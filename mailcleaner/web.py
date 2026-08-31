"""Local web GUI.

A stdlib HTTP server on 127.0.0.1 plus one static page. No new dependencies and
no framework: the browser is only a nicer renderer for the same read-only
queries and the same guarded actions the terminal dashboard uses.

Safety notes specific to serving over HTTP:

- Bound to the loopback interface only, never 0.0.0.0.
- Every request must carry the session token printed at startup (the browser
  gets it once in the launch URL and keeps it in sessionStorage). This stops
  any other page in the browser from driving the API.
- The Host header must be a loopback name, which blocks DNS rebinding.
- The client never sends SQL. It names a *target* ("sender", "domain",
  "category", "suggestion", ...) and the server builds the predicate, so the
  browser cannot widen a selection beyond what the UI offers.
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
from . import actions, db, providers, stats, sync
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


def target_title(target: dict) -> str:
    kind = target.get("kind")
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
            f"{email} is already connected as {account.id}. "
            "Remove it first with: mclean remove " + account.id
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

    def start(self, store: Store, account: Account) -> dict:
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
            target=self._run, args=(store, account, flow, device), daemon=True
        )
        self.thread.start()
        return self.snapshot()

    def _run(self, store, account, flow, device) -> None:
        try:
            blob = oauth.poll_for_token(flow, device)
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


class Handler(BaseHTTPRequestHandler):
    server_version = "gmail-cleaner"
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
                return {
                    "row": row,
                    "categories": stats.category_breakdown(conn, where, params),
                    "samples": stats.messages(conn, where, params, limit=8),
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
            if path == "/api/messages":
                where, params = build_where(
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
            added = verify_and_store(store, account)
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
                where, params = build_where(target)
                info = actions.preview(conn, where, params)
                info["title"] = target_title(target)
                info["actions_enabled"] = account.actions_enabled
                return info

            if path == "/api/action":
                if not account.actions_enabled:
                    raise Denied(
                        "Actions are disabled for this account. Run "
                        f"`mclean enable-actions -a {account.id}` first."
                    )
                action = body.get("action")
                if action not in actions.ALL_ACTIONS:
                    raise Denied(f"unknown action {action!r}")
                where, params = build_where(body.get("target") or {})
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

            raise Denied("unknown endpoint")
        finally:
            conn.close()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, token: str, verbose: bool):
        super().__init__(addr, Handler)
        self.token = token
        self.job = SyncJob()
        self.oauth_job = OAuthJob()
        self.verbose = verbose

    @property
    def store(self) -> Store:
        """Re-read on every use so CLI changes (enable-actions, use, add) take
        effect in an already-open browser tab."""
        return Store.load()

    @property
    def account(self) -> Account:
        store = self.store
        account = store.current
        if account is None:
            raise Denied("No accounts configured. Run: mclean add")
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


def serve(host: str = "127.0.0.1", port: int = 8765,
          open_browser: bool = True, verbose: bool = False) -> None:
    """Run the GUI until Ctrl-C."""
    token = secrets.token_urlsafe(24)
    server = Server((host, port), token, verbose)
    url = f"http://{host}:{server.server_address[1]}/?t={token}"
    print(f"gmail-cleaner GUI: {url}")
    print("Only this machine can reach it. Ctrl-C to stop.", flush=True)
    if open_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
