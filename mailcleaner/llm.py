"""Optional LLM advisor. Off unless the user turns it on for an account.

What it does:

- **Rule suggestions.** For senders the deterministic classifier is unsure
  about, it gets a per-*sender* summary (address, volume, read rate, list
  headers, current category and - only if the account allows it - a few
  subject lines) and proposes a rule: protect, ignore, or set a category. One
  call covers many senders, so a 50k-message mailbox costs a handful of calls.
- **Plain-English search.** "old receipts from food delivery apps" becomes a
  filter in the whitelisted language of `policies.FIELDS`, never SQL.

What it cannot do:

- Act. Suggestions land in `llm_suggestions` as *pending*; only the user's
  accept turns one into an ordinary rule.
- Remove protection. A suggestion to ignore a sender that has any protected
  mail is dropped here, before the user ever sees it. The model may err only in
  the direction of keeping mail.
- Invent senders. Anything it names that was not in the batch it was shown is
  discarded.
- See bodies. Nothing beyond the metadata index is ever sent.

Providers: a local Ollama server (nothing leaves the machine), or Anthropic's
API, which needs an explicit opt-in because it sends sender addresses - and
subject lines, if allowed - to a third party.
"""

from __future__ import annotations

import json
import sqlite3
import time
import urllib.error
import urllib.request

from . import actions, policies, sync

PROVIDERS = ("off", "ollama", "anthropic")
DEFAULT_MODELS = {"ollama": "llama3.1", "anthropic": "claude-opus-5-5"}
OLLAMA_HOST = "http://127.0.0.1:11434"

#: Categories the model may assign; the same vocabulary the classifier uses.
CATEGORIES = (
    "finance", "security", "orders", "travel", "developer", "social", "work",
    "promotion", "newsletter", "human", "automated",
)
#: Categories the classifier protects. A sender with any of these is never
#: offered as "ignore", whatever the model says.
PROTECTED_CATEGORIES = ("finance", "security", "travel", "human")

BATCH = 20
SUBJECTS_PER_SENDER = 5


class LLMError(RuntimeError):
    """Anything the user should see rather than a traceback."""


# -- providers --------------------------------------------------------------


class OllamaClient:
    """Ollama's /api/chat with a JSON-schema `format`. Local by default."""

    name = "ollama"

    def __init__(self, model: str = "", host: str = ""):
        self.model = model or DEFAULT_MODELS["ollama"]
        self.host = (host or OLLAMA_HOST).rstrip("/")

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        body = json.dumps({
            "model": self.model, "stream": False, "format": schema,
            "options": {"temperature": 0},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }).encode()
        req = urllib.request.Request(
            self.host + "/api/chat", data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                data = json.loads(resp.read())
        except urllib.error.URLError as e:
            raise LLMError(
                f"Could not reach Ollama at {self.host} ({e}). Is it running? "
                f"Try: ollama pull {self.model} && ollama serve"
            ) from e
        try:
            return json.loads(data["message"]["content"])
        except (KeyError, TypeError, json.JSONDecodeError) as e:
            raise LLMError(f"Ollama returned something that is not JSON: {e}") from e


class AnthropicClient:
    """Claude via the official SDK, with structured output.

    Credentials come from the keychain entry `mclean llm setup` stores, or
    from whatever the SDK resolves on its own (ANTHROPIC_API_KEY, an
    `ant auth login` profile).
    """

    name = "anthropic"

    def __init__(self, model: str = "", api_key: str | None = None):
        try:
            import anthropic
        except ImportError as e:
            raise LLMError(
                "The Anthropic provider needs the SDK: "
                "pip install 'mailcleaner[claude]' (or: uv pip install anthropic)"
            ) from e
        self._anthropic = anthropic
        self.model = model or DEFAULT_MODELS["anthropic"]
        self.client = anthropic.Anthropic(api_key=api_key) if api_key \
            else anthropic.Anthropic()

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        a = self._anthropic
        try:
            # `fallbacks: "default"` re-runs a request a safety classifier
            # declined on Anthropic's recommended fallback model, server-side.
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except a.AuthenticationError as e:
            raise LLMError("Anthropic rejected the API key. Run: mclean llm setup") from e
        except a.RateLimitError as e:
            raise LLMError("Anthropic rate limit hit; try again shortly.") from e
        except a.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except a.APIConnectionError as e:
            raise LLMError(f"Could not reach the Anthropic API: {e}") from e
        if response.stop_reason == "refusal":
            raise LLMError("The model declined this request.")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"The model returned something that is not JSON: {e}") from e


def client_for(account) -> OllamaClient | AnthropicClient:
    provider = getattr(account, "llm_provider", "off") or "off"
    if provider == "off":
        raise LLMError(
            "The LLM advisor is off for this account. Turn it on with: "
            f"mclean llm setup -a {account.id}"
        )
    if provider == "ollama":
        return OllamaClient(account.llm_model, account.llm_host)
    if provider == "anthropic":
        from . import accounts as acct_mod

        return AnthropicClient(account.llm_model, acct_mod.get_llm_key(account))
    raise LLMError(f"Unknown LLM provider {provider!r}")


# -- rule suggestions -------------------------------------------------------


SUGGEST_SYSTEM = f"""You help a person tidy their own mailbox. You see one summary
per sender: address, how much they send, how much of it was read, whether it is
a mailing list, how a rule-based classifier labelled it, and sometimes recent
subject lines. For each sender, propose at most one rule:

- "protect": mail the person would regret losing (banks, government, health,
  employers, schools, real people, receipts, legal or account notices).
- "ignore": bulk mail they clearly do not read (marketing, digests nobody opens,
  social notifications). Only when you are confident.
- "category": the classifier picked the wrong category; give the right one.
- "none": the current handling is fine, or you are unsure.

Be conservative. When in doubt, choose "protect" or "none", never "ignore".
Use only these categories: {", ".join(CATEGORIES)}.
Give a short reason a person can check, and a confidence between 0 and 1.
Only mention senders from the list you were given."""

SUGGEST_SCHEMA = {
    "type": "object",
    "properties": {
        "suggestions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sender": {"type": "string"},
                    "action": {"type": "string",
                               "enum": ["protect", "ignore", "category", "none"]},
                    "category": {"type": "string"},
                    "reason": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["sender", "action", "category", "reason", "confidence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["suggestions"],
    "additionalProperties": False,
}


def candidates(conn: sqlite3.Connection, limit: int = 40) -> list[dict]:
    """Senders worth asking about: unsure classifications and loud unprotected
    ones, skipping anything a rule or an earlier suggestion already covers."""
    rows = conn.execute(
        """SELECT from_email AS sender, from_domain AS domain,
                  COUNT(*) emails, SUM(is_unread) unread, SUM(protected) protected,
                  SUM(is_starred) starred, MAX(list_id != '' OR unsubscribe=1) list,
                  AVG(confidence) confidence, MIN(received_at) first_seen,
                  MAX(received_at) last_seen,
                  SUM(CASE WHEN category IN ('finance','security','travel','human')
                      THEN 1 ELSE 0 END) sensitive
           FROM emails WHERE from_email != ''
           GROUP BY from_email
           HAVING AVG(confidence) < 0.7 OR emails >= 10
           ORDER BY (AVG(confidence) < 0.7) DESC, emails DESC"""
    ).fetchall()
    ruled_senders = {r["match_value"] for r in conn.execute(
        "SELECT match_value FROM rules WHERE match_type='sender'")}
    ruled_domains = [r["match_value"] for r in conn.execute(
        "SELECT match_value FROM rules WHERE match_type='domain'")]
    asked = {r["match_value"] for r in conn.execute(
        "SELECT match_value FROM llm_suggestions WHERE match_type='sender'")}
    out = []
    for r in rows:
        d = dict(r)
        s = d["sender"]
        if s in ruled_senders or s in asked:
            continue
        if any(d["domain"] == v or d["domain"].endswith("." + v) for v in ruled_domains):
            continue
        cats = conn.execute(
            "SELECT category, COUNT(*) n FROM emails WHERE from_email=? "
            "GROUP BY category ORDER BY n DESC LIMIT 1", (s,)
        ).fetchone()
        d["category"] = cats["category"] if cats else "unknown"
        out.append(d)
        if len(out) >= limit:
            break
    return out


def _summary(conn, c: dict, share_subjects: bool) -> dict:
    weeks = max(1.0, ((c["last_seen"] or 0) - (c["first_seen"] or 0)) / 604800)
    d = {
        "sender": c["sender"],
        "emails": c["emails"],
        "per_week": round(c["emails"] / weeks, 1),
        "read_fraction": round(1 - (c["unread"] or 0) / max(1, c["emails"]), 2),
        "mailing_list": bool(c["list"]),
        "starred": c["starred"] or 0,
        "classifier_category": c["category"],
    }
    if share_subjects:
        d["recent_subjects"] = [r["subject"] for r in conn.execute(
            "SELECT DISTINCT subject FROM emails WHERE from_email=? "
            "ORDER BY received_at DESC LIMIT ?", (c["sender"], SUBJECTS_PER_SENDER))]
    return d


def suggest_rules(conn: sqlite3.Connection, account, client=None,
                  limit: int = 40, progress=None) -> dict:
    """Ask the model about up to `limit` senders. Stores pending suggestions."""
    client = client or client_for(account)
    share = bool(getattr(account, "llm_share_subjects", True))
    cands = candidates(conn, limit)
    by_sender = {c["sender"]: c for c in cands}
    stored = dropped = 0
    for i in range(0, len(cands), BATCH):
        chunk = cands[i:i + BATCH]
        payload = [_summary(conn, c, share) for c in chunk]
        result = client.complete_json(
            SUGGEST_SYSTEM, json.dumps({"senders": payload}, ensure_ascii=False),
            SUGGEST_SCHEMA,
        )
        allowed = {c["sender"] for c in chunk}
        for s in (result or {}).get("suggestions", []) or []:
            kept = _store_one(conn, s, allowed, by_sender, client)
            stored += kept
            dropped += not kept and s.get("action") not in ("none", None)
        if progress:
            progress(min(i + BATCH, len(cands)), len(cands))
    conn.commit()
    return {"asked": len(cands), "stored": stored, "dropped": dropped,
            "model": f"{client.name}:{client.model}"}


def _store_one(conn, s: dict, allowed: set, by_sender: dict, client) -> bool:
    sender = str(s.get("sender") or "").strip().lower()
    action = s.get("action")
    if sender not in allowed or action not in ("protect", "ignore", "category"):
        return False
    category = str(s.get("category") or "").strip().lower() or None
    if action == "category":
        if category not in CATEGORIES:
            return False
    else:
        category = None
    c = by_sender[sender]
    touchy = c["protected"] or c["sensitive"] or c["starred"]
    if touchy and (action == "ignore" or
                   (action == "category" and category not in PROTECTED_CATEGORIES)):
        # The asymmetry: a model may add protection, never take it away - not
        # even indirectly, by moving protected mail into a bulk category.
        return False
    try:
        conf = max(0.0, min(1.0, float(s.get("confidence") or 0)))
    except (TypeError, ValueError):
        conf = 0.0
    cur = conn.execute(
        "INSERT OR IGNORE INTO llm_suggestions(match_type,match_value,action,"
        "category,reason,confidence,model,status,created_at) "
        "VALUES('sender',?,?,?,?,?,?,'pending',?)",
        (sender, action, category, str(s.get("reason") or "")[:300], conf,
         f"{client.name}:{client.model}", int(time.time())),
    )
    return cur.rowcount > 0


def suggestions(conn: sqlite3.Connection, status: str = "pending") -> list[dict]:
    rows = conn.execute(
        """SELECT s.*, (SELECT COUNT(*) FROM emails e WHERE e.from_email=s.match_value) emails
           FROM llm_suggestions s WHERE status=? ORDER BY confidence DESC, id""",
        (status,),
    ).fetchall()
    return [dict(r) for r in rows]


def decide(conn: sqlite3.Connection, suggestion_id: int, accept: bool) -> dict:
    """Accept (turn into a rule and reclassify) or reject one suggestion."""
    r = conn.execute("SELECT * FROM llm_suggestions WHERE id=?",
                     (suggestion_id,)).fetchone()
    if not r:
        raise LLMError("unknown suggestion")
    if r["status"] != "pending":
        raise LLMError(f"already {r['status']}")
    if accept:
        actions.add_rule(conn, r["match_type"], r["match_value"], r["action"],
                         r["category"])
    conn.execute("UPDATE llm_suggestions SET status=? WHERE id=?",
                 ("accepted" if accept else "rejected", suggestion_id))
    conn.commit()
    if accept:
        sync.reclassify_all(conn)
    return dict(r) | {"status": "accepted" if accept else "rejected"}


# -- plain-English search ---------------------------------------------------


def _query_schema() -> dict:
    props = {}
    for field, (kind, help_) in policies.FIELDS.items():
        if kind == "strs":
            props[field] = {"type": "array", "items": {"type": "string"},
                            "description": help_}
        elif kind == "str":
            props[field] = {"type": "string", "description": help_}
        elif kind == "int":
            props[field] = {"type": "integer", "description": help_}
        else:
            props[field] = {"type": "boolean", "description": help_}
    return {
        "type": "object",
        "properties": {
            "filter": {"type": "object", "properties": props,
                       "additionalProperties": False},
            "explanation": {"type": "string"},
        },
        "required": ["filter", "explanation"],
        "additionalProperties": False,
    }


QUERY_SYSTEM = """Turn a person's description of some of their email into a filter.
Use only the filter fields in the schema; leave out anything not implied.
Dates are relative: "old" with no number means older_than_days 180, "recent"
means newer_than_days 30. Categories: """ + ", ".join(CATEGORIES) + """.
Use domain for companies ("from amazon" -> ["amazon.com"]) when you know the
domain, otherwise subject_contains. The explanation says, in one sentence,
what the filter selects."""


def parse_query(text: str, account, client=None, known_domains=()) -> dict:
    """Plain English -> a validated filter. Raises on anything unusable."""
    text = (text or "").strip()
    if not text:
        raise LLMError("say what you are looking for")
    if len(text) > 500:
        raise LLMError("keep the description under 500 characters")
    client = client or client_for(account)
    hint = ""
    if known_domains:
        hint = "\nSome sending domains in this mailbox: " + ", ".join(known_domains[:60])
    out = client.complete_json(QUERY_SYSTEM + hint, text, _query_schema())
    raw = {k: v for k, v in ((out or {}).get("filter") or {}).items()
           if v not in (None, "", [])}
    try:
        f = policies.normalize_filter(raw)
    except policies.FilterError as e:
        raise LLMError(f"could not turn that into a filter: {e}") from e
    return {"filter": f, "explanation": str((out or {}).get("explanation") or ""),
            "summary": policies.describe(f)}
