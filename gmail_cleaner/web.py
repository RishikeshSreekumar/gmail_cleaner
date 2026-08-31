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
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import actions, db, stats, sync
from .config import Config, get_password
from .imapclient import GmailImap, ImapError

STATIC = Path(__file__).parent / "static"
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}

ATTENTION_WHERE = (
    "is_unread=1 AND attention IN "
    "('action required','potentially important','read later')"
)


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
        return "gm_msgid = ?", (str(value),)
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

    def start(self, config: Config, full: bool = False) -> dict:
        with self.lock:
            if self.state["running"]:
                return dict(self.state)
            self.state = {"running": True, "done": 0, "total": 0,
                          "message": "Connecting to Gmail...", "error": ""}
        self.thread = threading.Thread(
            target=self._run, args=(config, full), daemon=True
        )
        self.thread.start()
        return self.snapshot()

    def _run(self, config: Config, full: bool) -> None:
        try:
            pw = get_password(config.email)
            if not config.email or not pw:
                raise ImapError("Not configured. Run: gclean setup")
            conn = db.connect()
            with GmailImap(config.email, pw) as client:
                def progress(done, total):
                    self._set(done=done, total=total,
                              message=f"Fetching metadata {done}/{total}")
                n = sync.sync(conn, client, config.sync_days, full=full,
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
        except ImapError as e:
            return self._json({"error": str(e)}, 502)
        except Exception as e:
            return self._json({"error": str(e)}, 500)

    # --- API ------------------------------------------------------------

    def api_get(self, path: str, q: dict):
        conn = db.connect()
        try:
            one = lambda k, d=None: q.get(k, [d])[0]
            limit = min(int(one("limit", "100") or 100), 2000)

            if path == "/api/overview":
                cfg = self.server.config
                return {
                    "account": cfg.email,
                    "actions_enabled": cfg.actions_enabled,
                    "sync_days": cfg.sync_days,
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

    def api_post(self, path: str, body: dict):
        cfg = self.server.config

        if path == "/api/sync":
            return self.server.job.start(cfg, full=bool(body.get("full")))
        if path == "/api/sync/status":
            return self.server.job.snapshot()

        conn = db.connect()
        try:
            if path == "/api/preview":
                target = body.get("target") or {}
                where, params = build_where(target)
                info = actions.preview(conn, where, params)
                info["title"] = target_title(target)
                info["actions_enabled"] = cfg.actions_enabled
                return info

            if path == "/api/action":
                if not cfg.actions_enabled:
                    raise Denied(
                        "Actions are disabled. Run `gclean enable-actions` first."
                    )
                action = body.get("action")
                if action not in (actions.ARCHIVE, actions.TRASH,
                                  actions.MARK_READ, actions.STAR, actions.LABEL):
                    raise Denied(f"unknown action {action!r}")
                where, params = build_where(body.get("target") or {})
                label = body.get("label") or (
                    cfg.review_label if action == actions.LABEL else None
                )
                pw = get_password(cfg.email)
                if not pw:
                    raise Denied("No stored app password. Run: gclean setup")
                with GmailImap(cfg.email, pw) as client:
                    # protected mail is never included from the web UI
                    return actions.run(conn, client, action, where, params,
                                       label=label, include_protected=False)

            if path == "/api/undo":
                batch_id = body.get("batch_id")
                if not batch_id:
                    raise Denied("no batch id")
                pw = get_password(cfg.email)
                if not pw:
                    raise Denied("No stored app password. Run: gclean setup")
                with GmailImap(cfg.email, pw) as client:
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
        self.verbose = verbose

    @property
    def config(self) -> Config:
        """Re-read on every use so `gclean enable-actions` takes effect live."""
        return Config.load()


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
