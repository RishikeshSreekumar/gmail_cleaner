"""Local web GUI.

A stdlib HTTP server on 127.0.0.1 plus one static page. No new dependencies and
no framework. This is the primary interface: everything a person needs -
connecting mailboxes, turning actions on, rules, unsubscribing, the LLM
advisor, background sync, exports - is reachable from the page, so nobody has
to touch a terminal. The CLI remains for scripts and power users.

Safety notes specific to serving over HTTP:

- Bound to the loopback interface only, never 0.0.0.0.
- Every request must carry the session token printed at startup (the browser
  gets it once in the launch URL and keeps it in sessionStorage). This stops
  any other page in the browser from driving the API.
- The Host header must be a loopback name, which blocks DNS rebinding.
- The client never sends SQL. It names a *target* ("sender", "domain",
  "category", "suggestion", "policy", ...) or a *filter* in the whitelisted
  language of `policies.FIELDS`, and the server builds the predicate with
  bound parameters, so the browser cannot reach a column or operator the UI
  does not offer.
- Turning on a cloud LLM provider requires an explicit consent flag in the
  request, which the page only sends after the user ticks the box that says
  what will leave the machine. An API key goes straight to the keychain and is
  never returned by any endpoint.
- Settings that change the machine (installing the background job) and the
  mailbox (actions on) are separate, explicit requests; turning actions *off*
  is always allowed.
- Unsubscribing is an outbound request on the user's behalf, so it needs
  actions enabled exactly like archive and trash do.
- Connecting a mailbox from the page means an app password crosses this
  loopback socket. It is written straight to the keychain, never stored by the
  page, never logged, and never echoed back by any endpoint - `/api/accounts`
  and `/api/overview` report only whether a credential exists. Providers that
  use OAuth never see a password at all: the browser gets a device code and the
  sign-in happens on the provider's own site.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import accounts as acct_mod
from . import actions, db, llm, policies, providers, stats, sync, unsubscribe
from .accounts import Account, Store
from .providers import MailError, oauth

STATIC = Path(__file__).parent / "static"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}

ATTENTION_WHERE = (
    "is_unread=1 AND attention IN "
    "('action required','potentially important','read later')"
)


#: Rich console markup, which the setup instructions are written in.
RICH_TAG = re.compile(r"\[/?[a-z][a-z ]*\]")


def plain(text: str) -> str:
    """Rich markup out, and the bold heading with it.

    Every provider's setup text opens with `[b]<service>[/b]`, which the dialog
    already shows as its title - repeating it inside the steps just looks like a
    mistake.
    """
    text = (text or "").strip()
    head, sep, rest = text.partition("\n")
    if head.startswith("[b]") and head.rstrip().endswith("[/b]"):
        text = rest.strip()
    return RICH_TAG.sub("", text).strip()


class Denied(Exception):
    """Request rejected for a reason worth showing the user."""


def build_where(target: dict) -> tuple[str, tuple]:
    """Turn a UI target into SQL. The browser never supplies a predicate."""
    kind = target.get("kind")
    value = target.get("value")
    if kind == "sender":
        return "from_email = ?", (str(value).lower(),)
    if kind == "senders":
        # Multi-select from the sender table. The list is still names, never SQL,
        # and it is capped so one request cannot build an unbounded predicate.
        values = [str(v).lower() for v in (value or []) if str(v).strip()]
        if not values:
            raise Denied("no senders selected")
        if len(values) > 200:
            raise Denied("too many senders in one selection")
        marks = ",".join("?" * len(values))
        return f"from_email IN ({marks})", tuple(values)
    if kind == "domain":
        return "from_domain = ?", (str(value).lower(),)
    if kind == "category":
        return "category = ?", (str(value),)
    if kind == "message":
        return "msg_key = ?", (str(value),)
    if kind == "attention":
        return ATTENTION_WHERE, ()
    if kind == "suggestion":
        try:
            name, _desc, where = stats.CLEANUP_SUGGESTIONS[int(value)]
        except (ValueError, TypeError, IndexError):
            raise Denied("unknown cleanup suggestion")
        return where, ()
    if kind == "filter":
        try:
            return policies.compile_filter(value or {})
        except policies.FilterError as e:
            raise Denied(str(e))
    if kind == "policy":
        raise Denied("policy targets need the index; use build_where_conn")
    if kind == "age":
        try:
            _name, lo, hi = stats.AGE_BUCKETS[int(value)]
        except (ValueError, TypeError, IndexError):
            raise Denied("unknown age bucket")
        now = int(time.time())
        return "received_at <= ? AND received_at > ?", (
            now - lo * stats.DAY, now - hi * stats.DAY,
        )
    raise Denied(f"unknown target {kind!r}")


def build_where_conn(conn, target: dict) -> tuple[str, tuple]:
    """build_where, plus targets that need the index to resolve (policies)."""
    if target.get("kind") == "policy":
        try:
            p = policies.get(conn, int(target.get("value")))
        except (TypeError, ValueError):
            p = None
        if not p:
            raise Denied("unknown rule")
        return policies.where_for(p)
    return build_where(target)


def target_title(target: dict) -> str:
    kind = target.get("kind")
    if kind == "filter":
        try:
            return policies.describe(target.get("value") or {})
        except policies.FilterError:
            return "filter"
    if kind == "policy":
        return f"rule {target.get('value')}"
    if kind == "suggestion":
        return stats.CLEANUP_SUGGESTIONS[int(target["value"])][0]
    if kind == "age":
        return stats.AGE_BUCKETS[int(target["value"])][0]
    if kind == "attention":
        return "Needs attention"
    if kind == "senders":
        n = len(target.get("value") or [])
        return f"{n} sender{'' if n == 1 else 's'}"
    return f"{kind}: {target.get('value')}"


def account_from_body(store: Store, body: dict) -> Account:
    """Turn the add-mailbox form into an Account, validating every field here.

    The browser picks a provider *key*; it never supplies a backend class, and
    for known providers it cannot override the host either - so a page cannot
    talk this tool into connecting somewhere the registry does not list.
    """
    key = str(body.get("provider") or "").strip().lower()
    if key not in providers.PROVIDERS:
        raise Denied("unknown provider")
    info = providers.get(key)

    email = str(body.get("email") or "").strip()
    if "@" not in email or len(email) > 254:
        raise Denied("that does not look like an email address")

    over = {"label": str(body.get("label") or "").strip()[:60]}
    if info.ask_host:
        host = str(body.get("host") or info.host).strip()
        if not host:
            raise Denied("this provider needs a hostname")
        security = str(body.get("security") or info.security).strip().lower()
        if security not in ("ssl", "starttls", "none"):
            raise Denied("security must be ssl, starttls or none")
        try:
            port = int(body.get("port") or info.port)
        except (TypeError, ValueError):
            raise Denied("port must be a number")
        if not 1 <= port <= 65535:
            raise Denied("port must be between 1 and 65535")
        over.update(host=host, port=port, security=security)

    account = Account.from_provider(key, email, **over)
    if store.get(account.id):
        raise Denied(
            f"{email} is already connected. To sign in again, open Settings."
        )
    return account


def verify_and_store(store: Store, account: Account) -> dict:
    """Connect once with the credential just saved, then keep the account.

    A mailbox that will not open is never added, and its half-written
    credential and directory are cleaned up rather than left behind.
    """
    try:
        with acct_mod.backend(account) as client:
            folders = client.sync_folders()
    except Exception:
        acct_mod.clear_secret(account)
        shutil.rmtree(account.dir, ignore_errors=True)
        raise
    store.add(account)
    store.set_active(account)
    db.connect(account).close()
    return {"id": account.id, "label": account.display,
            "provider": account.provider_info.name, "folders": folders}


def replace_credential(account: Account, write, suffix: str = "") -> None:
    """Swap in a new credential for an existing mailbox, keeping the old one if
    the new one does not work. Unlike adding, a failure never touches the
    account's directory or index."""
    old = acct_mod._read_secret(account, suffix)
    write()
    try:
        with acct_mod.backend(account):
            pass
    except Exception:
        if old:
            acct_mod._write_secret(account, suffix, old)
        else:
            acct_mod._drop_secret(account, suffix)
        raise


class OAuthJob:
    """One browser sign-in at a time, with a state the page can poll.

    The device-code flow is a wait, not a request: the user has to finish on the
    provider's site. So the request that starts it returns the code immediately
    and the polling happens on this thread.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.state = self._idle()

    @staticmethod
    def _idle() -> dict:
        return {"running": False, "code": "", "url": "", "email": "",
                "message": "", "error": "", "account": None}

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)

    def _set(self, **kw) -> None:
        with self.lock:
            self.state.update(kw)

    def start(self, store: Store, account: Account, reauth: bool = False) -> dict:
        with self.lock:
            if self.state["running"]:
                return dict(self.state)
        info = account.provider_info
        flow = oauth.flow_for(info.oauth_flow, account.oauth_client_id or None)
        device = oauth.start_device_login(flow)
        with self.lock:
            self.state = self._idle()
            self.state.update(
                running=True, email=account.email,
                code=device.get("user_code", ""),
                url=device.get("verification_uri")
                    or device.get("verification_url", ""),
                message=f"Waiting for {account.email} to sign in...",
            )
        self.thread = threading.Thread(
            target=self._run, args=(store, account, flow, device, reauth), daemon=True
        )
        self.thread.start()
        return self.snapshot()

    def _run(self, store, account, flow, device, reauth=False) -> None:
        try:
            blob = oauth.poll_for_token(flow, device)
            if reauth:
                replace_credential(account, lambda: acct_mod.store_oauth(account, blob),
                                   ".oauth")
                self._set(running=False, account={"id": account.id,
                                                  "label": account.display},
                          code="", url="", message=f"Signed in again as {account.email}.")
                return
            acct_mod.store_oauth(account, blob)
            added = verify_and_store(store, account)
            self._set(running=False, account=added, code="", url="",
                      message=f"Connected {added['label']}.")
        except Exception as e:  # surfaced in the page, not swallowed
            self._set(running=False, error=str(e), code="", url="",
                      message="Sign-in failed.")


class SyncJob:
    """One background sync at a time, with progress the page can poll."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.state = {"running": False, "done": 0, "total": 0, "message": "", "error": ""}

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)

    def _set(self, **kw) -> None:
        with self.lock:
            self.state.update(kw)

    def start(self, account: Account, full: bool = False) -> dict:
        with self.lock:
            if self.state["running"]:
                return dict(self.state)
            self.state = {"running": True, "done": 0, "total": 0,
                          "message": f"Connecting to {account.host}...", "error": ""}
        self.thread = threading.Thread(
            target=self._run, args=(account, full), daemon=True
        )
        self.thread.start()
        return self.snapshot()

    def _run(self, account: Account, full: bool) -> None:
        try:
            conn = db.connect(account)
            with acct_mod.backend(account) as client:
                def progress(done, total):
                    self._set(done=done, total=total,
                              message=f"Fetching metadata {done}/{total}")
                n = sync.sync(conn, client, account.sync_days, full=full,
                              progress=progress)
            conn.close()
            self._set(running=False, message=f"Synced {n} messages.")
        except Exception as e:  # surfaced in the page, not swallowed
            self._set(running=False, error=str(e), message="Sync failed.")


class LLMJob:
    """One rule-suggestion run at a time; a model can take a while."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state = {"running": False, "done": 0, "total": 0, "message": "",
                      "error": "", "result": None}

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.state)

    def _set(self, **kw) -> None:
        with self.lock:
            self.state.update(kw)

    def start(self, account: Account, limit: int) -> dict:
        with self.lock:
            if self.state["running"]:
                return dict(self.state)
            self.state = {"running": True, "done": 0, "total": 0, "error": "",
                          "message": "Asking the model...", "result": None}
        threading.Thread(target=self._run, args=(account, limit), daemon=True).start()
        return self.snapshot()

    def _run(self, account: Account, limit: int) -> None:
        conn = db.connect(account)
        try:
            res = llm.suggest_rules(
                conn, account, limit=limit,
                progress=lambda d, t: self._set(done=d, total=t,
                                                message=f"Asked about {d}/{t} senders"))
            self._set(running=False, result=res,
                      message=f"{res['stored']} suggestions, {res['dropped']} dropped "
                              "by the safety checks.")
        except Exception as e:  # surfaced in the page, not swallowed
            self._set(running=False, error=str(e), message="The model run failed.")
        finally:
            conn.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "mailcleaner"
    protocol_version = "HTTP/1.1"

    # --- plumbing -------------------------------------------------------

    def log_message(self, fmt, *args) -> None:  # quiet by default
        if self.server.verbose:
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        return host in ALLOWED_HOSTS

    def _token_ok(self, query: dict) -> bool:
        given = self.headers.get("X-Token") or (query.get("t", [""])[0])
        return secrets.compare_digest(given, self.server.token)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            raise Denied("malformed request body")

    # --- routing --------------------------------------------------------

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not self._host_ok():
            return self._json({"error": "bad host"}, 403)

        if url.path in ("/", "/index.html"):
            page = (STATIC / "index.html").read_bytes()
            return self._send(200, page, "text/html; charset=utf-8")

        if not url.path.startswith("/api/"):
            return self._json({"error": "not found"}, 404)
        if not self._token_ok(query):
            return self._json({"error": "bad or missing token"}, 403)
        if url.path == "/api/export":
            try:
                name, ctype, data = self.export(query)
            except Denied as e:
                return self._json({"error": str(e)}, 400)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
            return

        try:
            return self._json(self.api_get(url.path, query))
        except Denied as e:
            return self._json({"error": str(e)}, 400)
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    def do_POST(self) -> None:
        url = urlparse(self.path)
        query = parse_qs(url.query)
        if not self._host_ok():
            return self._json({"error": "bad host"}, 403)
        if not self._token_ok(query):
            return self._json({"error": "bad or missing token"}, 403)
        try:
            return self._json(self.api_post(url.path, self._body()))
        except Denied as e:
            return self._json({"error": str(e)}, 400)
        except MailError as e:
            return self._json({"error": str(e)}, 502)
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    # --- API ------------------------------------------------------------

    EXPORT_COLUMNS = ("received_at from_name from_email from_domain subject size "
                      "category subcategory attention retention protected is_unread "
                      "is_inbox is_starred list_id folder").split()

    def export(self, q: dict) -> tuple[str, str, bytes]:
        """Senders, domains, categories or a selection of messages, as CSV/JSON."""
        import csv
        import io

        one = lambda k, d=None: q.get(k, [d])[0]
        what = one("what", "senders")
        fmt = one("format", "csv")
        if fmt not in ("csv", "json"):
            raise Denied("format must be csv or json")
        account = self.server.account
        conn = db.connect(account)
        try:
            if what == "senders":
                rows = stats.senders(conn, 100000)
            elif what == "domains":
                rows = stats.domains(conn, 100000)
            elif what == "categories":
                rows = stats.categories(conn)
            elif what == "messages":
                target = {"kind": one("kind"), "value": one("value")}
                if target["kind"] == "filter":
                    try:
                        target["value"] = json.loads(target["value"] or "{}")
                    except json.JSONDecodeError:
                        raise Denied("bad filter")
                where, params = (build_where_conn(conn, target) if target["kind"]
                                 else ("1=1", ()))
                rows = [{c: r[c] for c in self.EXPORT_COLUMNS}
                        for r in stats.messages(conn, where, params, limit=1_000_000)]
            else:
                raise Denied("export senders, domains, categories or messages")
        finally:
            conn.close()
        stamp = time.strftime("%Y%m%d")
        name = f"mailcleaner-{account.id}-{what}-{stamp}.{fmt}"
        if fmt == "json":
            return name, "application/json", json.dumps(rows, default=str, indent=2).encode()
        buf = io.StringIO()
        if rows:
            w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        return name, "text/csv; charset=utf-8", buf.getvalue().encode()

    def settings(self, account: Account, conn) -> dict:
        from . import schedule
        from .classify import LOCALES

        return {
            "version": __import__("mailcleaner").__version__,
            "account": {
                "id": account.id, "email": account.email, "label": account.display,
                "provider": account.provider, "provider_name": account.provider_info.name,
                "uses_oauth": account.provider_info.uses_oauth,
                "host": account.host, "port": account.port, "security": account.security,
                "credential": acct_mod.get_secret(account) is not None,
                "actions_enabled": account.actions_enabled,
                "sync_days": account.sync_days, "review_label": account.review_label,
                "locales": account.locales,
                "setup_url": account.provider_info.setup_url,
                "messages": stats.overview(conn)["total"] or 0,
                "last_sync": db.get_state(conn, "last_sync"),
            },
            "locales": {k: sorted({w for fam in v.values() for w in fam})[:6]
                        for k, v in LOCALES.items()},
            "llm": {
                "provider": account.llm_provider, "model": account.llm_model,
                "host": account.llm_host or llm.OLLAMA_HOST,
                "share_subjects": account.llm_share_subjects,
                "has_key": bool(acct_mod.get_llm_key(account)),
                "defaults": llm.DEFAULT_MODELS,
            },
            "schedule": schedule.status(),
            "mcp": {"command": schedule.mclean_command() + ["mcp"]},
        }


    def api_get(self, path: str, q: dict):
        if path == "/api/providers":
            return {"rows": [{
                "key": p.key, "name": p.name, "host": p.host, "port": p.port,
                "security": p.security, "ask_host": p.ask_host,
                "uses_oauth": p.uses_oauth, "notes": p.notes,
                "help": plain(p.setup_help),
            } for p in providers.choices()]}

        store = self.server.store
        if path == "/api/accounts":
            return {"rows": self.server.account_rows(), "active": store.active}
        if path == "/api/overview" and not store.accounts:
            # Nothing configured yet: the page renders its onboarding state
            # rather than an error.
            return {"needs_setup": True, "accounts": [], "account": None}

        account = self.server.account
        conn = db.connect(account)
        try:
            one = lambda k, d=None: q.get(k, [d])[0]
            limit = min(int(one("limit", "100") or 100), 2000)

            if path == "/api/overview":
                return {
                    "account": account.display,
                    "account_id": account.id,
                    "provider": account.provider_info.name,
                    "accounts": self.server.account_rows(),
                    "actions_enabled": account.actions_enabled,
                    "sync_days": account.sync_days,
                    "last_sync": db.get_state(conn, "last_sync"),
                    "stats": stats.overview(conn),
                    "categories": stats.categories(conn),
                    "top_senders": stats.senders(conn, 10),
                    "top_domains": stats.domains(conn, 10),
                    "alerts": stats.alerts(conn, 5),
                    "llm_provider": account.llm_provider,
                    "pending_suggestions": len(llm.suggestions(conn)),
                }
            if path == "/api/senders":
                return {"rows": stats.senders(
                    conn, limit, one("order", "emails"), one("domain"))}
            if path == "/api/domains":
                return {"rows": stats.domains(conn, limit, one("order", "emails"))}
            if path == "/api/sender":
                email = (one("value") or "").lower()
                if not email:
                    raise Denied("no sender")
                row = stats.sender(conn, email)
                if not row:
                    raise Denied("unknown sender")
                where, params = build_where({"kind": "sender", "value": email})
                unsub = unsubscribe.options(conn, email)
                return {
                    "row": row,
                    "categories": stats.category_breakdown(conn, where, params),
                    "samples": stats.messages(conn, where, params, limit=8),
                    # How, and to which host - never the URL, which usually
                    # carries a token identifying the recipient.
                    "unsubscribe": ({"method": unsub["method"], "host": unsub["host"]}
                                    if unsub else None),
                }
            if path == "/api/categories":
                return {"rows": stats.categories(conn)}
            if path == "/api/ages":
                return {"rows": stats.ages(conn)}
            if path == "/api/frequency":
                return {"rows": stats.frequency(conn)}
            if path == "/api/attention":
                return {"rows": stats.attention(conn, limit)}
            if path == "/api/suggestions":
                rows = stats.suggestions(conn)
                for i, r in enumerate(rows):
                    r.pop("where", None)
                    r["index"] = i
                return {"rows": rows}
            if path == "/api/history":
                return {"rows": actions.batches(conn, limit)}
            if path == "/api/alerts":
                return {"rows": stats.alerts(conn, 20)}
            if path == "/api/settings":
                return self.settings(account, conn)
            if path == "/api/doctor":
                from . import doctor

                checks = doctor.diagnose(account) + doctor.index_checks(account)
                return {"checks": [c.__dict__ for c in checks]}
            if path == "/api/eval/sample":
                n = max(5, min(int(one("n", "30") or 30), 200))
                rows = conn.execute(
                    "SELECT msg_key, from_email, subject, category, protected "
                    "FROM emails ORDER BY RANDOM() LIMIT ?", (n,)).fetchall()
                return {"rows": [dict(r) for r in rows],
                        "categories": list(llm.CATEGORIES) + ["unknown"]}
            if path == "/api/llm/models":
                return {"models": ollama_models(one("host") or account.llm_host
                                                or llm.OLLAMA_HOST)}
            if path == "/api/digest":
                return stats.digest(conn, max(1, min(int(one("days", "7") or 7), 365)))
            if path == "/api/rules":
                return {
                    "rules": [dict(r) for r in conn.execute(
                        "SELECT * FROM rules ORDER BY id DESC")],
                    "policies": policies.list_all(conn),
                    "fields": {k: {"kind": v[0], "help": v[1]}
                               for k, v in policies.FIELDS.items()},
                    "policy_actions": list(policies.POLICY_ACTIONS),
                }
            if path == "/api/unsubscribe":
                return {"rows": unsubscribe.senders(conn, limit),
                        "history": unsubscribe.history(conn, 50)}
            if path == "/api/llm":
                return {"provider": account.llm_provider, "model": account.llm_model,
                        "share_subjects": account.llm_share_subjects,
                        "pending": llm.suggestions(conn),
                        "job": self.server.llm_job.snapshot()}
            if path == "/api/messages":
                where, params = build_where_conn(conn,
                    {"kind": one("kind"), "value": one("value")})
                return {
                    "title": target_title({"kind": one("kind"), "value": one("value")}),
                    "rows": stats.messages(conn, where, params, limit=limit),
                    "total": stats.count(conn, where, params),
                }
            raise Denied("unknown endpoint")
        finally:
            conn.close()

    def api_account(self, path: str, body: dict):
        """Add, switch between and sign in to mailboxes."""
        store = self.server.store

        if path == "/api/account/use":
            # The same operation the CLI performs: it only changes which index
            # the next request reads.
            chosen = store.get(str(body.get("id") or ""))
            if not chosen:
                raise Denied("unknown account")
            store.set_active(chosen)
            return {"active": chosen.id, "rows": self.server.account_rows()}

        if path == "/api/account/add":
            account = account_from_body(store, body)
            if account.provider_info.uses_oauth:
                raise Denied(
                    f"{account.provider_info.name} signs in through the browser. "
                    "Use Start sign-in instead of a password."
                )
            password = str(body.get("password") or "").replace(" ", "").strip()
            if not password:
                raise Denied("this provider needs an app password")
            acct_mod.set_password(account, password)
            try:
                added = verify_and_store(store, account)
            except MailError as e:
                from . import doctor

                raise Denied(f"{e} {doctor.login_fix(account.provider, str(e))}")
            return {**added, "rows": self.server.account_rows()}

        if path == "/api/account/oauth/start":
            account = account_from_body(store, body)
            if not account.provider_info.uses_oauth:
                raise Denied("this provider uses an app password, not a sign-in")
            return self.server.oauth_job.start(store, account)

        if path == "/api/account/oauth/status":
            state = self.server.oauth_job.snapshot()
            if state.get("account"):
                state["rows"] = self.server.account_rows()
            return state

        raise Denied("unknown endpoint")

    def api_post(self, path: str, body: dict):
        if path.startswith("/api/account/"):
            # Managing accounts has to work before any mailbox is configured.
            return self.api_account(path, body)

        account = self.server.account
        if path == "/api/sync":
            return self.server.job.start(account, full=bool(body.get("full")))
        if path == "/api/sync/status":
            return self.server.job.snapshot()

        conn = db.connect(account)
        try:
            if path == "/api/preview":
                target = body.get("target") or {}
                where, params = build_where_conn(conn, target)
                info = actions.preview(conn, where, params)
                info["title"] = target_title(target)
                info["actions_enabled"] = account.actions_enabled
                return info

            if path == "/api/action":
                if not account.actions_enabled:
                    raise Denied("This mailbox is read-only. Turn on actions in Settings.")
                action = body.get("action")
                if action not in actions.ALL_ACTIONS:
                    raise Denied(f"unknown action {action!r}")
                where, params = build_where_conn(conn, body.get("target") or {})
                label = body.get("label") or (
                    account.review_label if action == actions.LABEL else None
                )
                with acct_mod.backend(account) as client:
                    # protected mail is never included from the web UI
                    return actions.run(conn, client, action, where, params,
                                       label=label, include_protected=False)

            if path == "/api/undo":
                batch_id = body.get("batch_id")
                if not batch_id:
                    raise Denied("no batch id")
                with acct_mod.backend(account) as client:
                    return {"undone": actions.undo(conn, client, str(batch_id))}

            if path == "/api/reclassify":
                return {"reclassified": sync.reclassify_all(conn)}

            if path == "/api/settings/actions":
                store = self.server.store
                acct = store.get(account.id)
                acct.actions_enabled = bool(body.get("enabled"))
                store.update(acct)
                return {"actions_enabled": acct.actions_enabled}
            if path == "/api/settings/account":
                store = self.server.store
                acct = store.get(account.id)
                if "sync_days" in body:
                    try:
                        days = int(body["sync_days"])
                    except (TypeError, ValueError):
                        raise Denied("sync window must be a number of days")
                    if not 7 <= days <= 36500:
                        raise Denied("sync window must be between 7 days and 100 years")
                    acct.sync_days = days
                if "label" in body:
                    acct.label = str(body["label"] or "").strip()[:60] or acct.email
                if "review_label" in body:
                    acct.review_label = str(body["review_label"] or "").strip()[:80] \
                        or "Cleanup/Review"
                reclassified = None
                if "locales" in body:
                    from .classify import LOCALES

                    chosen = [str(x) for x in (body["locales"] or [])]
                    bad = [x for x in chosen if x not in LOCALES]
                    if bad:
                        raise Denied(f"unknown keyword pack(s): {', '.join(bad)}")
                    acct.locales = [] if set(chosen) == set(LOCALES) else chosen
                store.update(acct)
                if "locales" in body:
                    conn.close()
                    conn = db.connect(acct)   # mirrors the new packs into the index
                    reclassified = sync.reclassify_all(conn)
                return {"ok": True, "reclassified": reclassified}
            if path == "/api/settings/password":
                if account.provider_info.uses_oauth:
                    raise Denied("this mailbox signs in through the browser")
                password = str(body.get("password") or "").replace(" ", "").strip()
                if not password:
                    raise Denied("enter the new app password")
                replace_credential(account,
                                   lambda: acct_mod.set_password(account, password))
                return {"ok": True}
            if path == "/api/settings/oauth":
                if not account.provider_info.uses_oauth:
                    raise Denied("this mailbox uses an app password")
                return self.server.oauth_job.start(self.server.store, account, reauth=True)
            if path == "/api/settings/remove":
                if str(body.get("confirm") or "") != account.email:
                    raise Denied("type the mailbox address to confirm")
                conn.close()
                store = self.server.store
                store.remove(store.get(account.id), purge=True)
                conn = None
                return {"removed": account.id, "rows": self.server.account_rows()}
            if path == "/api/settings/llm":
                store = self.server.store
                acct = store.get(account.id)
                provider = str(body.get("provider") or "off")
                if provider not in llm.PROVIDERS:
                    raise Denied("unknown LLM provider")
                if provider == "anthropic" and not body.get("cloud_consent"):
                    raise Denied("tick the box to confirm sending sender details to Anthropic")
                acct.llm_provider = provider
                acct.llm_model = (str(body.get("model") or "").strip()[:100]
                                  or llm.DEFAULT_MODELS.get(provider, ""))
                host = str(body.get("host") or "").strip()
                if host and not re.match(r"^https?://[^\s/]+(:\d+)?/?$", host):
                    raise Denied("the Ollama address looks like http://127.0.0.1:11434")
                acct.llm_host = host
                acct.llm_share_subjects = bool(body.get("share_subjects", True))
                key = str(body.get("api_key") or "").strip()
                if provider == "anthropic" and key:
                    acct_mod.set_llm_key(acct, key)
                if provider != "anthropic" or body.get("forget_key"):
                    acct_mod.clear_llm_key(acct)
                store.update(acct)
                return {"ok": True, "provider": provider, "model": acct.llm_model}
            if path == "/api/settings/schedule":
                from . import schedule

                if body.get("remove"):
                    return {"message": schedule.remove(), "status": schedule.status()}
                try:
                    msg = schedule.install(int(body.get("every") or 6),
                                           bool(body.get("apply_rules")),
                                           bool(body.get("notify", True)))
                except (ValueError, RuntimeError) as e:
                    raise Denied(str(e))
                return {"message": msg, "status": schedule.status()}
            if path == "/api/rules/apply":
                if not account.actions_enabled:
                    raise Denied("Turn on actions in Settings to apply rules.")
                pid = body.get("id")
                with acct_mod.backend(account) as client:
                    res = policies.apply(conn, client, only_auto=pid is None,
                                         policy_id=int(pid) if pid else None)
                return {"results": res}
            if path == "/api/eval/score":
                from . import evaluate

                rows = []
                for r in (body.get("rows") or [])[:500]:
                    key = str(r.get("msg_key") or "")
                    hit = conn.execute("SELECT from_email, subject FROM emails "
                                       "WHERE msg_key=?", (key,)).fetchone()
                    if not hit:
                        continue
                    rows.append({"msg_key": key, "from_email": hit["from_email"],
                                 "subject": hit["subject"],
                                 "true_category": str(r.get("true_category") or ""),
                                 "true_protected": "1" if r.get("true_protected") else "0"})
                return evaluate.score(evaluate.predict_rows(rows, conn))

            if path == "/api/rules/simple/add":
                mtype = body.get("match_type")
                if mtype not in ("sender", "domain"):
                    raise Denied("rules match a sender or a domain")
                act = body.get("action")
                if act not in ("protect", "ignore"):
                    raise Denied("rule action must be protect or ignore")
                value = str(body.get("value") or "").strip().lower()
                if not value or len(value) > 254:
                    raise Denied("no sender or domain given")
                actions.add_rule(conn, mtype, value, act)
                return {"reclassified": sync.reclassify_all(conn)}
            if path == "/api/rules/simple/remove":
                actions.delete_rule(conn, int(body.get("id") or 0))
                return {"reclassified": sync.reclassify_all(conn)}
            if path == "/api/rules/add":
                try:
                    pid = policies.add(conn, str(body.get("name") or ""),
                                       body.get("conditions") or {},
                                       str(body.get("action") or ""),
                                       body.get("label"), bool(body.get("auto")))
                except policies.FilterError as e:
                    raise Denied(str(e))
                return {"id": pid}
            if path == "/api/rules/remove":
                return {"removed": policies.remove(conn, int(body.get("id") or 0))}
            if path == "/api/rules/auto":
                return {"ok": policies.set_auto(conn, int(body.get("id") or 0),
                                                bool(body.get("auto")))}

            if path == "/api/unsubscribe":
                if not account.actions_enabled:
                    raise Denied("Unsubscribing sends a request on your behalf, so it "
                                 "needs actions on. Turn them on in Settings.")
                try:
                    return unsubscribe.unsubscribe(conn, str(body.get("sender") or ""))
                except unsubscribe.UnsubscribeError as e:
                    raise Denied(str(e))

            if path == "/api/llm/suggest":
                if account.llm_provider == "off":
                    raise Denied("The assistant is off. Turn it on in Settings.")
                limit = max(1, min(int(body.get("limit") or 40), 200))
                return self.server.llm_job.start(account, limit)
            if path == "/api/llm/status":
                return self.server.llm_job.snapshot()
            if path == "/api/llm/decide":
                try:
                    return llm.decide(conn, int(body.get("id") or 0),
                                      bool(body.get("accept")))
                except llm.LLMError as e:
                    raise Denied(str(e))
            if path == "/api/ask":
                if account.llm_provider == "off":
                    raise Denied("Plain-English search needs the assistant. "
                                 "Turn it on in Settings.")
                doms = [d["key"] for d in stats.domains(conn, 60)]
                try:
                    q = llm.parse_query(str(body.get("text") or ""), account,
                                        known_domains=doms)
                except llm.LLMError as e:
                    raise Denied(str(e))
                where, params = policies.compile_filter(q["filter"])
                return {**q, "total": stats.count(conn, where, params)}

            raise Denied("unknown endpoint")
        finally:
            if conn is not None:
                conn.close()


def ollama_models(host: str) -> list[str]:
    """Models an Ollama server has pulled; empty when it is not reachable."""
    import urllib.request

    if not re.match(r"^https?://[^\s/]+(:\d+)?/?$", host or ""):
        return []
    try:
        with urllib.request.urlopen(host.rstrip("/") + "/api/tags", timeout=2) as r:
            return sorted(m.get("name", "") for m in json.loads(r.read()).get("models", []))
    except Exception:
        return []


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, token: str, verbose: bool):
        super().__init__(addr, Handler)
        self.token = token
        self.job = SyncJob()
        self.oauth_job = OAuthJob()
        self.llm_job = LLMJob()
        self.verbose = verbose

    @property
    def store(self) -> Store:
        """Re-read on every use so a change made elsewhere (another tab, the
        CLI, the background job) shows up in an already-open page."""
        return Store.load()

    @property
    def account(self) -> Account:
        store = self.store
        account = store.current
        if account is None:
            raise Denied("No mailbox connected yet.")
        return account

    def account_rows(self) -> list[dict]:
        """Just enough for the switcher; no mailbox is opened to build it."""
        store = self.store
        return [{
            "id": a.id, "label": a.display, "email": a.email,
            "provider": a.provider_info.name,
            "actions_enabled": a.actions_enabled,
            "active": a.id == store.active,
        } for a in store.accounts]


def bind(host: str, port: int, token: str, verbose: bool, tries: int = 20) -> Server:
    """The first free port from `port` up: a second copy of the app, or anything
    else on 8765, should not stop this one from starting."""
    last = None
    for p in range(port, port + tries):
        try:
            return Server((host, p), token, verbose)
        except OSError as e:
            last = e
    raise OSError(f"no free port between {port} and {port + tries - 1}: {last}")


def serve(host: str = "127.0.0.1", port: int = 8765,
          open_browser: bool = True, verbose: bool = False) -> None:
    """Run the GUI until Ctrl-C."""
    token = secrets.token_urlsafe(24)
    server = bind(host, port, token, verbose)
    url = f"http://{host}:{server.server_address[1]}/?t={token}"
    print(f"mailcleaner is running at {url}")
    print("Only this machine can reach it. Keep this window open; Ctrl-C stops it.",
          flush=True)
    if open_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
