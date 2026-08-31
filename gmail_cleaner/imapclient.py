"""Thin Gmail-IMAP wrapper.

Safety: this module never issues EXPUNGE, never sets the \\Deleted flag, and
has no code path that permanently deletes mail. "Trash" adds Gmail's \\Trash
label, which is reversible from the Gmail UI or by this app's undo.
"""

from __future__ import annotations

import email
import imaplib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993

FETCH_ITEMS = (
    "(UID FLAGS RFC822.SIZE INTERNALDATE X-GM-MSGID X-GM-THRID X-GM-LABELS "
    "BODY.PEEK[HEADER.FIELDS (FROM TO CC REPLY-TO SUBJECT DATE LIST-ID "
    "LIST-UNSUBSCRIBE CONTENT-TYPE)])"
)

imaplib._MAXLINE = 10_000_000


class ImapError(RuntimeError):
    pass


@dataclass
class RawMessage:
    uid: int
    gm_msgid: str
    gm_thrid: str
    size: int
    flags: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    internaldate: datetime | None = None
    headers: dict[str, str] = field(default_factory=dict)


def _tokenize_list(s: str) -> list[str]:
    """Split an IMAP parenthesised list body into atoms/quoted strings."""
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c.isspace():
            i += 1
        elif c == '"':
            j = i + 1
            buf = []
            while j < n and s[j] != '"':
                if s[j] == "\\" and j + 1 < n:
                    buf.append(s[j + 1])
                    j += 2
                else:
                    buf.append(s[j])
                    j += 1
            out.append("".join(buf))
            i = j + 1
        else:
            j = i
            while j < n and not s[j].isspace():
                j += 1
            out.append(s[i:j])
            i = j
    return out


def _extract_paren(meta: str, key: str) -> str | None:
    """Return the raw text inside the parentheses following `key`."""
    m = re.search(re.escape(key) + r"\s*\(", meta)
    if not m:
        return None
    depth, i = 1, m.end()
    start = i
    while i < len(meta) and depth:
        if meta[i] == '"':
            i += 1
            while i < len(meta) and meta[i] != '"':
                i += 2 if meta[i] == "\\" else 1
        elif meta[i] == "(":
            depth += 1
        elif meta[i] == ")":
            depth -= 1
            if depth == 0:
                break
        i += 1
    return meta[start:i]


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return value.strip()


class GmailImap:
    def __init__(self, email_addr: str, app_password: str):
        self.email = email_addr
        self._password = app_password
        self.conn: imaplib.IMAP4_SSL | None = None
        self.all_mail = '"[Gmail]/All Mail"'
        self.trash = '"[Gmail]/Trash"'
        self._selected: tuple[str, bool] | None = None

    # -- connection ---------------------------------------------------------
    def __enter__(self) -> "GmailImap":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def connect(self) -> None:
        try:
            self.conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
            self.conn.login(self.email, self._password)
        except imaplib.IMAP4.error as e:
            msg = str(e)
            if "Invalid credentials" in msg or "AUTHENTICATIONFAILED" in msg:
                raise ImapError(
                    "Gmail rejected the login. Check the address and that the "
                    "16-character app password was pasted without spaces."
                ) from e
            raise ImapError(f"IMAP login failed: {msg}") from e
        self._discover_folders()

    def close(self) -> None:
        if self.conn is None:
            return
        try:
            self.conn.logout()
        except Exception:
            pass
        self.conn = None

    def _discover_folders(self) -> None:
        """Find the All Mail / Trash mailboxes via SPECIAL-USE flags."""
        typ, data = self.conn.list()
        if typ != "OK":
            return
        for line in data:
            text = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
            name = text.rsplit('"/"', 1)[-1].strip()
            if not name.startswith('"'):
                name = f'"{name}"'
            if "\\All" in text:
                self.all_mail = name
            elif "\\Trash" in text:
                self.trash = name

    def _select(self, folder: str, readonly: bool) -> None:
        if self._selected == (folder, readonly):
            return
        typ, _ = self.conn.select(folder, readonly=readonly)
        if typ != "OK":
            raise ImapError(f"Could not open mailbox {folder}")
        self._selected = (folder, readonly)

    def uidvalidity(self, folder: str | None = None) -> int:
        self._select(folder or self.all_mail, readonly=True)
        typ, data = self.conn.response("UIDVALIDITY")
        return int(data[0]) if typ == "OK" and data and data[0] else 0

    # -- reading ------------------------------------------------------------
    def search_since(self, days: int, min_uid: int = 1) -> list[int]:
        """UIDs in All Mail newer than `days` days, at or above `min_uid`."""
        self._select(self.all_mail, readonly=True)
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
        criteria = f"UID {min_uid}:* SINCE {since}" if min_uid > 1 else f"SINCE {since}"
        typ, data = self.conn.uid("SEARCH", None, criteria)
        if typ != "OK":
            raise ImapError(f"SEARCH failed: {data}")
        uids = [int(x) for x in data[0].split()] if data and data[0] else []
        return [u for u in uids if u >= min_uid]

    def fetch_metadata(self, uids: list[int]) -> list[RawMessage]:
        """Headers + flags + labels only. No bodies, no attachments."""
        if not uids:
            return []
        self._select(self.all_mail, readonly=True)
        typ, data = self.conn.uid("FETCH", ",".join(map(str, uids)), FETCH_ITEMS)
        if typ != "OK":
            raise ImapError(f"FETCH failed: {data}")
        return self._parse_fetch(data)

    @staticmethod
    def _parse_fetch(data) -> list[RawMessage]:
        out: list[RawMessage] = []
        for item in data:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            meta = item[0].decode("utf-8", "replace")
            raw_headers = item[1] or b""

            def num(key, default=0):
                m = re.search(re.escape(key) + r"\s+(\d+)", meta)
                return int(m.group(1)) if m else default

            uid = num("UID")
            msgid = str(num("X-GM-MSGID"))
            thrid = str(num("X-GM-THRID"))
            if not uid or msgid == "0":
                continue

            flags_raw = _extract_paren(meta, "FLAGS")
            labels_raw = _extract_paren(meta, "X-GM-LABELS")
            idate = None
            m = re.search(r'INTERNALDATE "([^"]+)"', meta)
            if m:
                try:
                    idate = imaplib.Internaldate2tuple(
                        f'INTERNALDATE "{m.group(1)}"'.encode()
                    )
                    idate = datetime.fromtimestamp(
                        __import__("time").mktime(idate), tz=timezone.utc
                    )
                except Exception:
                    idate = None

            msg = email.message_from_bytes(raw_headers)
            headers = {k.lower(): _decode(v) for k, v in msg.items()}

            out.append(
                RawMessage(
                    uid=uid,
                    gm_msgid=msgid,
                    gm_thrid=thrid,
                    size=num("RFC822.SIZE"),
                    flags=_tokenize_list(flags_raw) if flags_raw else [],
                    labels=_tokenize_list(labels_raw) if labels_raw else [],
                    internaldate=idate,
                    headers=headers,
                )
            )
        return out

    def fetch_preview(self, uid: int, limit: int = 2000) -> str:
        """Lazily fetch a snippet of the body — only when the user opens a mail."""
        self._select(self.all_mail, readonly=True)
        typ, data = self.conn.uid("FETCH", str(uid), f"(BODY.PEEK[TEXT]<0.{limit}>)")
        if typ != "OK":
            return ""
        for item in data:
            if isinstance(item, tuple) and item[1]:
                text = item[1].decode("utf-8", "replace")
                text = re.sub(r"<[^>]+>", " ", text)
                return re.sub(r"\s+", " ", text).strip()[:limit]
        return ""

    # -- mutation -----------------------------------------------------------
    def _store(self, uids: list[int], item: str, value: str) -> None:
        if not uids:
            return
        self._select(self.all_mail, readonly=False)
        for i in range(0, len(uids), 500):
            chunk = ",".join(str(u) for u in uids[i : i + 500])
            typ, data = self.conn.uid("STORE", chunk, item, value)
            if typ != "OK":
                raise ImapError(f"STORE {item} failed: {data}")

    def add_labels(self, uids: list[int], labels: list[str]) -> None:
        self._store(uids, "+X-GM-LABELS", "(" + " ".join(_q(l) for l in labels) + ")")

    def remove_labels(self, uids: list[int], labels: list[str]) -> None:
        self._store(uids, "-X-GM-LABELS", "(" + " ".join(_q(l) for l in labels) + ")")

    def add_flags(self, uids: list[int], flags: list[str]) -> None:
        self._store(uids, "+FLAGS", "(" + " ".join(flags) + ")")

    def remove_flags(self, uids: list[int], flags: list[str]) -> None:
        self._store(uids, "-FLAGS", "(" + " ".join(flags) + ")")

    def create_label(self, name: str) -> None:
        try:
            self.conn.create(_q(name))
        except Exception:
            pass

    def uids_for_msgids(self, msgids: list[str], folder: str) -> dict[str, int]:
        """Re-locate messages by permanent Gmail id (used to undo a trash)."""
        self._select(folder, readonly=False)
        found: dict[str, int] = {}
        for mid in msgids:
            typ, data = self.conn.uid("SEARCH", None, f"X-GM-MSGID {mid}")
            if typ == "OK" and data and data[0]:
                found[mid] = int(data[0].split()[0])
        return found


def _q(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
