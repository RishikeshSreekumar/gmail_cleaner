"""Unsubscribing from mailing lists, from the List-Unsubscribe header.

Three shapes exist in the wild, and this module handles each as far as it can
honestly go:

- **one-click** (RFC 8058): an https URL plus `List-Unsubscribe-Post:
  List-Unsubscribe=One-Click`. The sender has promised that a bare POST to that
  URL unsubscribes you, so this tool sends exactly that and nothing else.
- **link**: an https URL without the one-click promise. Fetching it usually
  lands on a page that asks for confirmation, so the URL is handed back for the
  user to open in their own browser.
- **mailto**: an address to write to. This tool never sends mail; the mailto
  link is handed back for the user's mail client.

The URL comes from the message, which means from the sender, so the one-click
request is treated as hostile input: https only, the host must resolve to
public addresses and the connection is pinned to the address that was checked
(no DNS rebinding onto your LAN), no cookies or credentials, no redirects
followed, and only the host is logged - never the full URL, which often embeds
a token identifying you.

Unsubscribing does not touch any message. Archiving the backlog afterwards is a
separate, ordinary bulk action with its own preview and undo.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import sqlite3
import ssl
import time
from urllib.parse import urlsplit

TIMEOUT = 15
USER_AGENT = "mailcleaner (RFC 8058 one-click unsubscribe)"


class UnsubscribeError(RuntimeError):
    """Anything the user should see rather than a traceback."""


def options(conn: sqlite3.Connection, sender: str) -> dict | None:
    """The newest unsubscribe target this sender has offered, if any."""
    r = conn.execute(
        """SELECT unsub_url, unsub_post, unsub_mailto, received_at
           FROM emails
           WHERE from_email = ? AND (COALESCE(unsub_url,'') != ''
                                     OR COALESCE(unsub_mailto,'') != '')
           ORDER BY unsub_post DESC, received_at DESC LIMIT 1""",
        (sender.lower(),),
    ).fetchone()
    if not r:
        return None
    method = ("one-click" if r["unsub_post"] and r["unsub_url"]
              else "link" if r["unsub_url"] else "mailto")
    return {"sender": sender.lower(), "method": method,
            "url": r["unsub_url"] or "", "mailto": r["unsub_mailto"] or "",
            "host": urlsplit(r["unsub_url"] or "").hostname or ""}


def senders(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    """Senders that offer an unsubscribe, loudest first, with what was done."""
    rows = conn.execute(
        """SELECT from_email AS key, COUNT(*) emails, SUM(is_unread) unread,
                  SUM(size) bytes, MAX(received_at) last_seen,
                  SUM(is_inbox) inbox, SUM(protected) protected,
                  MAX(CASE WHEN unsub_post=1 AND COALESCE(unsub_url,'')!=''
                           THEN 1 ELSE 0 END) one_click,
                  MAX(CASE WHEN COALESCE(unsub_url,'')!='' THEN 1 ELSE 0 END) link,
                  MAX(CASE WHEN COALESCE(unsub_mailto,'')!='' THEN 1 ELSE 0 END) mailto
           FROM emails
           WHERE COALESCE(unsub_url,'') != '' OR COALESCE(unsub_mailto,'') != ''
           GROUP BY from_email ORDER BY emails DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    done = {
        r["sender"]: r["ts"] for r in conn.execute(
            "SELECT sender, MAX(ts) ts FROM unsubscribes "
            "WHERE status IN ('ok','manual') GROUP BY sender")
    }
    out = []
    for r in rows:
        d = dict(r)
        d["method"] = ("one-click" if d["one_click"] else
                       "link" if d["link"] else "mailto")
        d["unsubscribed_at"] = done.get(d["key"])
        out.append(d)
    return out


def history(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM unsubscribes ORDER BY ts DESC LIMIT ?", (limit,))]


def _log(conn, sender, method, target, status, detail="") -> None:
    conn.execute(
        "INSERT INTO unsubscribes(sender,method,target,status,detail,ts) "
        "VALUES(?,?,?,?,?,?)",
        (sender, method, target, status, detail[:300], int(time.time())),
    )
    conn.commit()


def public_address(host: str, port: int = 443) -> str:
    """Resolve `host` and return an address only if every answer is public."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise UnsubscribeError(f"cannot resolve {host}: {e}") from e
    addrs = []
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global:
            raise UnsubscribeError(
                f"{host} resolves to a non-public address ({ip}); refusing to "
                "send it a request from your machine"
            )
        addrs.append(str(ip))
    if not addrs:
        raise UnsubscribeError(f"{host} did not resolve")
    return addrs[0]


class _PinnedHTTPS(http.client.HTTPSConnection):
    """HTTPS to an address that was already checked, with the real host's SNI
    and certificate verification."""

    def __init__(self, host: str, address: str, **kw):
        super().__init__(host, **kw)
        self._address = address

    def connect(self) -> None:
        sock = socket.create_connection((self._address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def one_click_post(url: str, resolve=public_address, connection=None) -> int:
    """Send the RFC 8058 POST. Returns the HTTP status. Never follows redirects."""
    parts = urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise UnsubscribeError("one-click unsubscribe needs an https URL")
    if parts.username or parts.password:
        raise UnsubscribeError("refusing an unsubscribe URL with embedded credentials")
    port = parts.port or 443
    address = resolve(parts.hostname, port)
    conn = connection or _PinnedHTTPS(
        parts.hostname, address, port=port, timeout=TIMEOUT,
        context=ssl.create_default_context(),
    )
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    body = b"List-Unsubscribe=One-Click"
    try:
        conn.request("POST", path, body=body, headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(body)),
            "User-Agent": USER_AGENT,
        })
        resp = conn.getresponse()
        resp.read(4096)
        return resp.status
    except (OSError, ssl.SSLError, http.client.HTTPException) as e:
        raise UnsubscribeError(f"{parts.hostname}: {e}") from e
    finally:
        conn.close()


def unsubscribe(conn: sqlite3.Connection, sender: str, post=one_click_post) -> dict:
    """Unsubscribe from one sender, as far as the header allows.

    Returns {"method", "status", ...}. For "link" and "mailto" the caller must
    open the returned target itself; nothing was sent.
    """
    opt = options(conn, sender)
    if not opt:
        raise UnsubscribeError(f"{sender} has not offered an unsubscribe link")
    sender = opt["sender"]
    if opt["method"] == "one-click":
        try:
            status = post(opt["url"])
        except UnsubscribeError as e:
            _log(conn, sender, "one-click", opt["host"], "failed", str(e))
            raise
        ok = 200 <= status < 400
        _log(conn, sender, "one-click", opt["host"], "ok" if ok else "failed",
             f"HTTP {status}")
        if not ok:
            raise UnsubscribeError(f"{opt['host']} answered HTTP {status}")
        return {"sender": sender, "method": "one-click", "status": "ok",
                "host": opt["host"], "http_status": status}
    target = opt["url"] if opt["method"] == "link" else opt["mailto"]
    where = (opt["host"] if opt["method"] == "link"
             else target.split("?")[0].rsplit("@", 1)[-1])
    _log(conn, sender, opt["method"], where, "manual", "opened for the user")
    return {"sender": sender, "method": opt["method"], "status": "manual",
            "open": target}
