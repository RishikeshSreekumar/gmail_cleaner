"""Gmail over IMAP.

Gmail is the one label-shaped provider here: every message lives in All Mail
and carries labels, so archiving and trashing are label edits and a message's
UID never changes underneath us. That makes Gmail strictly safer than the
folder-shaped providers, and it is why the original tool was Gmail-only.

Access is still an app password, deliberately not OAuth.
"""

from __future__ import annotations

import re

from .base import (
    HEADER_FIELDS,
    ImapBackend,
    MsgRef,
    extract_paren,
    quote,
    tokenize_list,
)

INBOX_LABEL = "\\Inbox"
TRASH_LABEL = "\\Trash"

GMAIL_FETCH_ITEMS = (
    f"(UID FLAGS RFC822.SIZE INTERNALDATE X-GM-MSGID X-GM-THRID X-GM-LABELS "
    f"BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])"
)


class GmailBackend(ImapBackend):
    supports_labels = True
    moves_on_action = False
    fetch_items = GMAIL_FETCH_ITEMS

    def __init__(self, account, secret):
        super().__init__(account, secret)
        self.all_mail = "[Gmail]/All Mail"
        self.trash_box = "[Gmail]/Trash"

    def login_hint(self, message: str) -> str:
        if "Invalid credentials" in message or "AUTHENTICATIONFAILED" in message:
            return (
                "Gmail rejected the login. Check the address and that the "
                "16-character app password was pasted without spaces, and that "
                "IMAP is enabled in Gmail's settings."
            )
        return f"Gmail IMAP login failed: {message}"

    # -- folders ------------------------------------------------------------
    def sync_folders(self) -> list[str]:
        """One virtual folder holds everything; labels do the rest."""
        return [self.all_mail]

    # -- parsing ------------------------------------------------------------
    def message_key(self, meta: str, folder: str, uid: int, message_id: str) -> str:
        return _digits(meta, "X-GM-MSGID") or super().message_key(
            meta, folder, uid, message_id
        )

    def thread_key(self, meta: str, headers: dict, message_id: str) -> str:
        return _digits(meta, "X-GM-THRID") or ""

    def message_labels(self, meta: str, folder: str) -> list[str]:
        raw = extract_paren(meta, "X-GM-LABELS")
        return tokenize_list(raw) if raw else []

    def is_inbox(self, raw) -> bool:
        return any(l.lower() == "\\inbox" for l in raw.labels)

    # -- label plumbing -----------------------------------------------------
    def _labels(self, refs: list[MsgRef], op: str, labels: list[str]) -> None:
        value = "(" + " ".join(quote(l) for l in labels) + ")"
        self._store(self.all_mail, [r.uid for r in refs], f"{op}X-GM-LABELS", value)

    def locate(self, folder: str, refs: list[MsgRef]) -> dict[str, int]:
        """Gmail message ids are permanent, so mail can be found after a trash."""
        self._select(folder, readonly=False)
        found: dict[str, int] = {}
        for r in refs:
            if not str(r.key).isdigit():
                continue
            typ, data = self.conn.uid("SEARCH", None, f"X-GM-MSGID {r.key}")
            if typ == "OK" and data and data[0]:
                found[r.key] = int(data[0].split()[0])
        return found

    # -- operations ---------------------------------------------------------
    def archive(self, refs: list[MsgRef]) -> str:
        self._labels(refs, "-", [INBOX_LABEL])
        return self.all_mail

    def unarchive(self, refs: list[MsgRef]) -> None:
        self._labels(refs, "+", [INBOX_LABEL])

    def trash(self, refs: list[MsgRef]) -> str:
        """Adds Gmail's Trash label. The mail sits in Trash for 30 days and undo
        pulls it straight back. No EXPUNGE, no \\Deleted, ever."""
        self._labels(refs, "+", [TRASH_LABEL])
        return self.trash_box

    def untrash(self, refs: list[MsgRef]) -> None:
        found = self.locate(self.trash_box, refs)
        if not found:
            return
        self._store(self.trash_box, list(found.values()), "-X-GM-LABELS",
                    f'("{TRASH_LABEL}")')

    def apply_label(self, refs: list[MsgRef], name: str) -> str:
        self.ensure_folder(name)
        self._labels(refs, "+", [name])
        return self.all_mail

    def remove_label(self, refs: list[MsgRef], name: str) -> None:
        self._labels(refs, "-", [name])


def _digits(meta: str, key: str) -> str:
    m = re.search(re.escape(key) + r"\s+(\d+)", meta)
    return m.group(1) if m else ""
