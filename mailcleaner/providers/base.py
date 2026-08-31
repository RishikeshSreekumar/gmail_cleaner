"""IMAP plumbing shared by every provider, plus the generic folder-based backend.

Safety, unchanged from the Gmail-only version and now enforced for every
provider: no backend in this package issues EXPUNGE, sets the \\Deleted flag, or
has any code path that permanently deletes mail.

Providers differ in one structural way. Gmail is *label*-shaped: one message
lives in All Mail and carries labels, so archiving means dropping a label and
the UID never changes. Everything else is *folder*-shaped: a message lives in
exactly one mailbox, so archiving means moving it, and its UID changes with the
move. The generic backend below is the folder-shaped one; `gmail.py` overrides
the handful of methods where labels behave differently.

Moves use IMAP MOVE (RFC 6851). We refuse to fall back to the classic
COPY + STORE \\Deleted + EXPUNGE dance, because that is a delete.
"""

from __future__ import annotations

import email
import hashlib
import imaplib
import re
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header

# Some mailboxes return very long FETCH lines (large label sets, long headers).
imaplib._MAXLINE = 10_000_000

HEADER_FIELDS = (
    "FROM TO CC REPLY-TO SUBJECT DATE MESSAGE-ID LIST-ID LIST-UNSUBSCRIBE "
    "CONTENT-TYPE"
)
BASE_FETCH_ITEMS = (
    f"(UID FLAGS RFC822.SIZE INTERNALDATE "
    f"BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])"
)


class MailError(RuntimeError):
    """Anything the user should see rather than a traceback."""


# Kept under the old name so existing imports and error handling still read well.
ImapError = MailError


@dataclass
class RawMessage:
    """One message as it came off the wire, before normalisation."""

    uid: int
    msg_key: str
    thread_key: str
    folder: str
    size: int
    message_id: str = ""
    flags: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    internaldate: datetime | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class MsgRef:
    """Where a message currently is, as far as the local index knows."""

    key: str
    uid: int
    folder: str
    message_id: str = ""


# -- small parsing helpers -------------------------------------------------


def tokenize_list(s: str) -> list[str]:
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


def extract_paren(meta: str, key: str) -> str | None:
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


def decode_value(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return value.strip()


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _needs_quotes(mailbox: str) -> bool:
    return not (mailbox.startswith('"') and mailbox.endswith('"'))


def mailbox_atom(mailbox: str) -> str:
    """IMAP mailbox names with spaces must be quoted; already-quoted stays put."""
    return quote(mailbox) if _needs_quotes(mailbox) else mailbox


def unquote_mailbox(mailbox: str) -> str:
    if mailbox.startswith('"') and mailbox.endswith('"'):
        return mailbox[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return mailbox


# -- the generic, folder-shaped backend ------------------------------------


class ImapBackend:
    """Plain IMAP. Works with Fastmail, iCloud, Proton Bridge, Outlook, Zoho,
    self-hosted servers - anything that speaks IMAP4rev1 with MOVE."""

    #: Gmail-style labels (a message in several places at once).
    supports_labels = False
    #: Gmail exposes a stable per-message id; plain IMAP only has Message-ID.
    fetch_items = BASE_FETCH_ITEMS

    #: Mailboxes never worth indexing (drafts you are writing, junk, deleted).
    SKIP_FLAGS = {"\\Trash", "\\Junk", "\\Drafts"}
    SKIP_NAMES = {"trash", "deleted items", "deleted messages", "junk", "spam",
                  "bulk mail", "drafts", "outbox"}

    def __init__(self, account, secret):
        """`account` is a mailcleaner.accounts.Account; `secret` an AuthSecret."""
        self.account = account
        self.secret = secret
        self.conn: imaplib.IMAP4 | None = None
        self.inbox = "INBOX"
        self.archive_box = "Archive"
        self.trash_box = "Trash"
        self.all_mail: str | None = None
        self._specials: dict[str, str] = {}
        self._folders: list[str] = []
        self._selected: tuple[str, bool] | None = None
        self._capabilities: set[str] = set()

    # -- connection ---------------------------------------------------------
    def __enter__(self) -> "ImapBackend":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        if not self.account.verify_cert:
            # Proton Bridge and some self-hosted servers present a self-signed
            # certificate on loopback. Only ever relaxed when the account says so.
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx

    def connect(self) -> None:
        acct = self.account
        try:
            if acct.security == "ssl":
                self.conn = imaplib.IMAP4_SSL(
                    acct.host, acct.port, ssl_context=self._context()
                )
            else:
                self.conn = imaplib.IMAP4(acct.host, acct.port)
                if acct.security == "starttls":
                    self.conn.starttls(self._context())
        except (OSError, ssl.SSLError) as e:
            raise MailError(
                f"Could not reach {acct.host}:{acct.port} - {e}"
            ) from e

        self._login()
        self._capabilities = {
            c.decode() if isinstance(c, bytes) else c
            for c in (self.conn.capabilities or ())
        }
        if "MOVE" not in self._capabilities:
            # Everything this app does to move mail depends on MOVE, because the
            # alternative (COPY + \Deleted + EXPUNGE) is a delete.
            self.move_supported = False
        else:
            self.move_supported = True
        self.discover_folders()

    def _login(self) -> None:
        acct, secret = self.account, self.secret
        try:
            if secret.kind == "xoauth2":
                token = secret.access_token()
                auth = f"user={acct.email}\1auth=Bearer {token}\1\1"
                self.conn.authenticate("XOAUTH2", lambda _: auth.encode())
            else:
                self.conn.login(acct.email, secret.password)
        except imaplib.IMAP4.error as e:
            raise MailError(self.login_hint(str(e))) from e

    def login_hint(self, message: str) -> str:
        return (
            f"{self.account.host} rejected the login: {message}\n"
            "Check the address and the password/app password for this account."
        )

    def close(self) -> None:
        if self.conn is None:
            return
        try:
            self.conn.logout()
        except Exception:
            pass
        self.conn = None
        self._selected = None

    # -- folders ------------------------------------------------------------
    def discover_folders(self) -> None:
        """Learn this server's special-use mailboxes and its full folder list."""
        typ, data = self.conn.list()
        if typ != "OK":
            return
        self._folders = []
        specials: dict[str, str] = {}
        for line in data or []:
            text = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
            parsed = self._parse_list_line(text)
            if not parsed:
                continue
            flags, name = parsed
            if "\\Noselect" in flags or "\\NonExistent" in flags:
                continue
            self._folders.append(name)
            for special in ("\\Inbox", "\\All", "\\Archive", "\\Trash",
                            "\\Junk", "\\Drafts", "\\Sent"):
                if special in flags:
                    specials[special] = name
        if name_of := specials.get("\\Archive"):
            self.archive_box = name_of
        if name_of := specials.get("\\Trash"):
            self.trash_box = name_of
        if name_of := specials.get("\\All"):
            self.all_mail = name_of
        self._specials = specials
        for folder in self._folders:
            if folder.lower() == "inbox":
                self.inbox = folder

    @staticmethod
    def _parse_list_line(text: str) -> tuple[list[str], str] | None:
        """`(\\HasNoChildren \\Archive) "/" "Archive"` -> (flags, name)."""
        m = re.match(r'\s*\(([^)]*)\)\s+(?:"([^"]*)"|NIL)\s+(.*)$', text)
        if not m:
            return None
        flags = m.group(1).split()
        name = m.group(3).strip()
        return flags, unquote_mailbox(name)

    def sync_folders(self) -> list[str]:
        """Which mailboxes to index. Everything the user can file mail into,
        minus junk/drafts/trash, which are noise or already discarded."""
        if self.account.sync_folders:
            return list(self.account.sync_folders)
        skip_names = set(self.SKIP_NAMES)
        for flag in self.SKIP_FLAGS:
            if got := self._specials.get(flag):
                skip_names.add(got.lower())
        out = [f for f in self._folders if f.lower() not in skip_names]
        return out or [self.inbox]

    def _select(self, folder: str, readonly: bool) -> None:
        if self._selected == (folder, readonly):
            return
        typ, _ = self.conn.select(mailbox_atom(folder), readonly=readonly)
        if typ != "OK":
            raise MailError(f"Could not open mailbox {folder}")
        self._selected = (folder, readonly)

    def uidvalidity(self, folder: str | None = None) -> int:
        self._select(folder or self.inbox, readonly=True)
        typ, data = self.conn.response("UIDVALIDITY")
        return int(data[0]) if typ == "OK" and data and data[0] else 0

    def ensure_folder(self, name: str) -> None:
        if name in self._folders:
            return
        try:
            self.conn.create(mailbox_atom(name))
        except Exception:
            pass
        self._folders.append(name)

    # -- reading ------------------------------------------------------------
    def search_since(self, folder: str, days: int, min_uid: int = 1) -> list[int]:
        self._select(folder, readonly=True)
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
        criteria = f"UID {min_uid}:* SINCE {since}" if min_uid > 1 else f"SINCE {since}"
        typ, data = self.conn.uid("SEARCH", None, criteria)
        if typ != "OK":
            raise MailError(f"SEARCH failed in {folder}: {data}")
        uids = [int(x) for x in data[0].split()] if data and data[0] else []
        return [u for u in uids if u >= min_uid]

    def fetch_metadata(self, folder: str, uids: list[int]) -> list[RawMessage]:
        """Headers + flags only. No bodies, no attachments."""
        if not uids:
            return []
        self._select(folder, readonly=True)
        typ, data = self.conn.uid("FETCH", ",".join(map(str, uids)), self.fetch_items)
        if typ != "OK":
            raise MailError(f"FETCH failed in {folder}: {data}")
        return [m for m in (self._parse_one(item, folder) for item in data or [])
                if m is not None]

    def _parse_one(self, item, folder: str) -> RawMessage | None:
        if not isinstance(item, tuple) or len(item) < 2:
            return None
        meta = item[0].decode("utf-8", "replace")
        raw_headers = item[1] or b""

        uid = _num(meta, "UID")
        if not uid:
            return None
        flags_raw = extract_paren(meta, "FLAGS")
        msg = email.message_from_bytes(raw_headers)
        headers = {k.lower(): decode_value(v) for k, v in msg.items()}
        message_id = (headers.get("message-id") or "").strip()

        return RawMessage(
            uid=uid,
            msg_key=self.message_key(meta, folder, uid, message_id),
            thread_key=self.thread_key(meta, headers, message_id),
            folder=folder,
            size=_num(meta, "RFC822.SIZE"),
            message_id=message_id,
            flags=tokenize_list(flags_raw) if flags_raw else [],
            labels=self.message_labels(meta, folder),
            internaldate=_internaldate(meta),
            headers=headers,
        )

    def message_key(self, meta: str, folder: str, uid: int, message_id: str) -> str:
        """A stable id for the index. Message-ID survives moves between folders;
        when a message has none, fall back to something unique but folder-local."""
        if message_id:
            return "mid:" + hashlib.sha1(message_id.encode()).hexdigest()
        return f"loc:{folder}:{uid}"

    def thread_key(self, meta: str, headers: dict, message_id: str) -> str:
        """Plain IMAP has no server-side threading; group by the References root."""
        refs = (headers.get("references") or headers.get("in-reply-to") or "").split()
        root = refs[0] if refs else message_id
        return hashlib.sha1(root.encode()).hexdigest() if root else ""

    def message_labels(self, meta: str, folder: str) -> list[str]:
        """Folder-shaped servers have one "label": the mailbox the mail sits in."""
        return [folder]

    def is_inbox(self, raw: RawMessage) -> bool:
        """Folder-shaped servers: the message is in the inbox if it sits there."""
        return (raw.folder or "").lower() == self.inbox.lower()

    def fetch_preview(self, folder: str, uid: int, limit: int = 2000) -> str:
        """Lazily fetch a snippet of the body - only when the user opens a mail."""
        self._select(folder, readonly=True)
        typ, data = self.conn.uid("FETCH", str(uid), f"(BODY.PEEK[TEXT]<0.{limit}>)")
        if typ != "OK":
            return ""
        for item in data:
            if isinstance(item, tuple) and item[1]:
                text = item[1].decode("utf-8", "replace")
                text = re.sub(r"<[^>]+>", " ", text)
                return re.sub(r"\s+", " ", text).strip()[:limit]
        return ""

    # -- low-level mutation -------------------------------------------------
    def _by_folder(self, refs: list[MsgRef]) -> dict[str, list[MsgRef]]:
        out: dict[str, list[MsgRef]] = {}
        for r in refs:
            out.setdefault(r.folder or self.inbox, []).append(r)
        return out

    def _store(self, folder: str, uids: list[int], item: str, value: str) -> None:
        if not uids:
            return
        self._select(folder, readonly=False)
        for i in range(0, len(uids), 500):
            chunk = ",".join(str(u) for u in uids[i : i + 500])
            typ, data = self.conn.uid("STORE", chunk, item, value)
            if typ != "OK":
                raise MailError(f"STORE {item} failed: {data}")

    def _move(self, folder: str, uids: list[int], dest: str) -> None:
        """UID MOVE. Never COPY + \\Deleted + EXPUNGE - that would be a delete."""
        if not uids:
            return
        if folder == dest:
            return
        if not getattr(self, "move_supported", True):
            raise MailError(
                f"{self.account.host} does not advertise the IMAP MOVE extension. "
                "This tool will not fall back to copy-and-delete, so archive, "
                "trash and label are unavailable for this account. Marking read "
                "and starring still work."
            )
        self.ensure_folder(dest)
        self._select(folder, readonly=False)
        for i in range(0, len(uids), 500):
            chunk = ",".join(str(u) for u in uids[i : i + 500])
            typ, data = self.conn.uid("MOVE", chunk, mailbox_atom(dest))
            if typ != "OK":
                raise MailError(f"MOVE to {dest} failed: {data}")

    def _flag(self, refs: list[MsgRef], op: str, flags: list[str]) -> None:
        for folder, group in self._by_folder(refs).items():
            self._store(folder, [r.uid for r in group], f"{op}FLAGS",
                        "(" + " ".join(flags) + ")")

    # -- locating mail after it has moved -----------------------------------
    def locate(self, folder: str, refs: list[MsgRef]) -> dict[str, int]:
        """Find messages again in `folder` (after a move) by Message-ID."""
        self._select(folder, readonly=False)
        found: dict[str, int] = {}
        for r in refs:
            if not r.message_id:
                continue
            typ, data = self.conn.uid(
                "SEARCH", None, "HEADER", "Message-ID", quote(r.message_id)
            )
            if typ == "OK" and data and data[0]:
                found[r.key] = int(data[0].split()[-1])
        return found

    def _relocate(self, refs: list[MsgRef], folder: str) -> list[MsgRef]:
        uids = self.locate(folder, refs)
        return [MsgRef(r.key, uids[r.key], folder, r.message_id)
                for r in refs if r.key in uids]

    # -- the operations actions.py calls ------------------------------------
    def archive(self, refs: list[MsgRef]) -> str:
        """Take mail out of the inbox. Returns where the mail now lives."""
        for folder, group in self._by_folder(refs).items():
            self._move(folder, [r.uid for r in group], self.archive_box)
        return self.archive_box

    def unarchive(self, refs: list[MsgRef]) -> None:
        moved = self._relocate(refs, self.archive_box)
        self._move(self.archive_box, [r.uid for r in moved], self.inbox)

    def trash(self, refs: list[MsgRef]) -> str:
        """Move to the server's Trash. Recoverable; nothing is expunged."""
        for folder, group in self._by_folder(refs).items():
            self._move(folder, [r.uid for r in group], self.trash_box)
        return self.trash_box

    def untrash(self, refs: list[MsgRef]) -> None:
        found = self.locate(self.trash_box, refs)
        for r in refs:
            if r.key in found:
                self._move(self.trash_box, [found[r.key]], r.folder or self.inbox)

    def mark_read(self, refs: list[MsgRef]) -> None:
        self._flag(refs, "+", ["\\Seen"])

    def mark_unread(self, refs: list[MsgRef]) -> None:
        self._flag(refs, "-", ["\\Seen"])

    def star(self, refs: list[MsgRef]) -> None:
        self._flag(refs, "+", ["\\Flagged"])

    def unstar(self, refs: list[MsgRef]) -> None:
        self._flag(refs, "-", ["\\Flagged"])

    def apply_label(self, refs: list[MsgRef], name: str) -> str:
        """Folder-shaped servers cannot add a second label, so this files the
        mail into `name` instead. Undo moves it back where it came from."""
        self.ensure_folder(name)
        for folder, group in self._by_folder(refs).items():
            self._move(folder, [r.uid for r in group], name)
        return name

    def remove_label(self, refs: list[MsgRef], name: str) -> None:
        found = self.locate(name, refs)
        for r in refs:
            if r.key in found:
                self._move(name, [found[r.key]], r.folder or self.inbox)

    #: True when `archive`/`label` change a message's UID (folder-shaped servers).
    moves_on_action = True


def _num(meta: str, key: str, default: int = 0) -> int:
    m = re.search(re.escape(key) + r"\s+(\d+)", meta)
    return int(m.group(1)) if m else default


def _internaldate(meta: str) -> datetime | None:
    m = re.search(r'INTERNALDATE "([^"]+)"', meta)
    if not m:
        return None
    try:
        import time as _time

        parsed = imaplib.Internaldate2tuple(f'INTERNALDATE "{m.group(1)}"'.encode())
        return datetime.fromtimestamp(_time.mktime(parsed), tz=timezone.utc)
    except Exception:
        return None
