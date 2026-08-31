"""Provider layer: folder discovery, message identity, and the legacy migration.

None of this touches the network - the pieces exercised here are the ones that
parse what a server said, or decide what to ask it next.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from make_fixture import sandbox  # noqa: E402

tmp = sandbox()

from mailcleaner import providers  # noqa: E402
from mailcleaner.accounts import Account, Store  # noqa: E402
from mailcleaner.providers.base import ImapBackend, mailbox_atom  # noqa: E402
from mailcleaner.providers.gmail import GmailBackend  # noqa: E402

# -- provider presets ------------------------------------------------------
assert providers.guess("someone@hotmail.com") == "outlook"
assert providers.guess("someone@proton.me") == "protonmail"
assert providers.guess("someone@my-company.example") == "imap"
assert providers.get("outlook").uses_oauth and not providers.get("gmail").uses_oauth
assert providers.get("protonmail").verify_cert is False, "Bridge is self-signed"
print("provider presets and address guessing OK")

# -- LIST parsing / which folders get indexed ------------------------------
LIST_LINES = [
    r'(\HasNoChildren) "/" "INBOX"',
    r'(\HasNoChildren \Archive) "/" "Archive"',
    r'(\HasNoChildren \Trash) "/" "Deleted Items"',
    r'(\HasNoChildren \Junk) "/" "Junk Email"',
    r'(\HasNoChildren \Drafts) "/" "Drafts"',
    r'(\HasNoChildren \Sent) "/" "Sent Items"',
    r'(\HasNoChildren) "/" "Receipts/2024"',
    r'(\Noselect \HasChildren) "/" "Containers"',
]


class FakeConn:
    def list(self):
        return "OK", [line.encode() for line in LIST_LINES]


account = Account.from_provider("imap", "me@example.test", host="mail.example.test")
backend = ImapBackend(account, secret=None)
backend.conn = FakeConn()
backend.discover_folders()

assert backend.archive_box == "Archive", backend.archive_box
assert backend.trash_box == "Deleted Items", backend.trash_box
folders = backend.sync_folders()
assert "INBOX" in folders and "Receipts/2024" in folders and "Sent Items" in folders
for junk in ("Deleted Items", "Junk Email", "Drafts", "Containers"):
    assert junk not in folders, (junk, folders)
print("folder discovery:", folders)

account.sync_folders = ["INBOX"]
assert backend.sync_folders() == ["INBOX"], "an explicit folder list wins"
account.sync_folders = []

assert mailbox_atom("Deleted Items") == '"Deleted Items"'
assert mailbox_atom('"[Gmail]/All Mail"') == '"[Gmail]/All Mail"'

# -- message identity ------------------------------------------------------
META = (
    "1 (UID 5 RFC822.SIZE 4021 X-GM-MSGID 1799 X-GM-THRID 1800 "
    'X-GM-LABELS (\\Inbox "Cleanup/Review") FLAGS (\\Seen) '
    'INTERNALDATE "01-Mar-2026 10:00:00 +0000" BODY[HEADER] {10}'
)
HEADERS = (b"From: Someone <a@b.example>\r\nSubject: hi\r\n"
           b"Message-ID: <abc@b.example>\r\n\r\n")

gmail_backend = GmailBackend(Account.from_provider("gmail", "me@gmail.com"), None)
raw = gmail_backend._parse_one((META.encode(), HEADERS), "[Gmail]/All Mail")
assert raw.msg_key == "1799" and raw.thread_key == "1800", raw
assert raw.labels == ["\\Inbox", "Cleanup/Review"], raw.labels
assert gmail_backend.is_inbox(raw) is True
assert raw.message_id == "<abc@b.example>"
print("gmail parse:", raw.msg_key, raw.labels)

generic = backend._parse_one((META.encode(), HEADERS), "INBOX")
assert generic.msg_key.startswith("mid:"), generic.msg_key
assert generic.labels == ["INBOX"], generic.labels
assert backend.is_inbox(generic) is True
assert backend.is_inbox(backend._parse_one((META.encode(), HEADERS), "Archive")) is False
# The same message keeps one identity wherever it is filed, which is what makes
# undo work after a MOVE.
moved = backend._parse_one((META.encode(), HEADERS), "Archive")
assert moved.msg_key == generic.msg_key
# ...and a message with no Message-ID still gets a unique, folder-local key.
no_id = backend._parse_one((META.encode(), b"Subject: nothing\r\n\r\n"), "INBOX")
assert no_id.msg_key == "loc:INBOX:5", no_id.msg_key
print("generic parse:", generic.msg_key, "stable across folders")

# -- MOVE is required; copy-then-delete is never a fallback ----------------
backend.move_supported = False
try:
    backend._move("INBOX", [1], "Archive")
    raise AssertionError("a server without MOVE must refuse, not fall back")
except providers.MailError as e:
    assert "MOVE" in str(e)
print("servers without MOVE are refused rather than copy-and-deleted")

# -- per-account isolation and ids ----------------------------------------
store = Store.load()
a1 = store.add(Account.from_provider("gmail", "same@example.com"))
a2 = store.add(Account.from_provider("fastmail", "same@example.com"))
assert a1.id != a2.id, "the same address on two services is two accounts"
assert a1.db_path != a2.db_path
assert store.get("same@example.com") is None, "an ambiguous address must not resolve"
assert store.get(a2.id).provider == "fastmail"
print("account ids:", a1.id, "/", a2.id)

# -- migration from the Gmail-only layout ---------------------------------
legacy = os.environ["GMAIL_CLEANER_HOME"]
os.makedirs(legacy, exist_ok=True)
with open(os.path.join(legacy, "config.json"), "w") as f:
    json.dump({"email": "old@gmail.com", "actions_enabled": True,
               "sync_days": 90, "review_label": "Old/Review"}, f)
with open(os.path.join(legacy, "app_password"), "w") as f:
    f.write("abcdabcdabcdabcd")

fresh_home = os.path.join(tmp, "home2")
os.environ["MAILCLEANER_HOME"] = fresh_home
import importlib  # noqa: E402

from mailcleaner import accounts as acct_mod  # noqa: E402

importlib.reload(acct_mod)
migrated = acct_mod.Store.load()
assert len(migrated.accounts) == 1, migrated.accounts
moved_acct = migrated.accounts[0]
assert moved_acct.email == "old@gmail.com" and moved_acct.provider == "gmail"
assert moved_acct.actions_enabled is True and moved_acct.sync_days == 90
assert moved_acct.review_label == "Old/Review"
secret = acct_mod.get_secret(moved_acct)
assert secret and secret.password == "abcdabcdabcdabcd"
print("legacy single-account install migrated to", moved_acct.id)

print("\nPROVIDER TESTS PASSED")
