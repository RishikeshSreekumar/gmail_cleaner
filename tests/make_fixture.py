"""Generate a synthetic index so the UI can be exercised without a real mailbox."""

import json
import os
import random
import tempfile
import time


def sandbox() -> str:
    """Point both the current and the legacy home at throwaway directories, so a
    test run can never see - or migrate - the developer's real config."""
    tmp = tempfile.mkdtemp()
    os.environ["MAILCLEANER_HOME"] = os.path.join(tmp, "home")
    os.environ["GMAIL_CLEANER_HOME"] = os.path.join(tmp, "legacy")
    return tmp


SENDERS = [
    ("jobs-noreply@linkedin.com", "linkedin.com", ["You have 3 new job alerts", "People you may know"]),
    ("notifications@github.com", "github.com", ["[repo] Dependabot security alert", "PR #42 merged"]),
    ("billing@amazonaws.com", "amazonaws.com", ["Your AWS Invoice is available", "Payment received"]),
    ("alerts@icicibank.com", "icicibank.com", ["Credit card statement generated", "Rs.4,200 debited from A/c"]),
    ("newsletter@medium.com", "medium.com", ["Your Daily Digest", "This week in tech"]),
    ("offers@swiggy.in", "swiggy.in", ["FLAT 60% OFF today only", "Your order has been delivered"]),
    ("no-reply@accounts.google.com", "accounts.google.com", ["Security alert: new sign-in", "Your OTP is 449281"]),
    ("priya@example.com", "example.com", ["Re: dinner on Friday?", "the deck from yesterday"]),
    ("bookings@makemytrip.com", "makemytrip.com", ["Your flight PNR X8K2L is confirmed", "Hotel booking voucher"]),
    ("digest@quora.com", "quora.com", ["10 answers for you", "New questions in Python"]),
]


def build(conn, n=900, seed=7, folder="[Gmail]/All Mail"):
    from mailcleaner import db, sync

    rnd = random.Random(seed)
    now = int(time.time())
    rules = sync.load_rules(conn)
    rows = []
    for i in range(n):
        addr, domain, subjects = rnd.choice(SENDERS)
        age = rnd.choice([1, 3, 9, 25, 70, 150, 300, 500, 900, 1400])
        ts = now - age * 86400 - rnd.randint(0, 86400)
        labels = ["\\Inbox"] if rnd.random() < 0.4 else []
        if rnd.random() < 0.05:
            labels.append("\\Important")
        m = {
            "msg_key": str(10_000 + i), "uid": 100 + i,
            "thread_key": str(90_000 + i), "folder": folder,
            "message_id": f"<{10_000 + i}@{domain}>",
            "received_at": ts, "from_name": addr.split("@")[0], "from_email": addr,
            "from_domain": domain, "to_emails": json.dumps(["me@example.com"]),
            "subject": rnd.choice(subjects), "size": rnd.randint(3_000, 4_000_000),
            "labels": labels, "is_unread": int(rnd.random() < 0.6),
            "is_inbox": int("\\Inbox" in labels), "is_starred": int(rnd.random() < 0.03),
            "is_important": int("\\Important" in labels),
            "list_id": f"<list.{domain}>" if "newsletter" in addr or "digest" in addr else "",
            "unsubscribe": int("newsletter" in addr or "offers" in addr or "digest" in addr),
            "has_attachment": int(rnd.random() < 0.1),
            "age_days": age,
        }
        rows.append(sync.enrich(m, rules))
    sync.upsert(conn, rows)
    db.set_state(conn, "last_sync", now - 600)
    db.set_state(conn, "folders", [folder])
    return len(rows)


class FakeBackend:
    """Records backend calls instead of making them. Mirrors the label-shaped
    (Gmail) contract; `moves_on_action` flips it to the folder-shaped one."""

    supports_labels = True
    moves_on_action = False

    def __init__(self, moves=False):
        self.calls = []
        self.moves_on_action = moves
        self.supports_labels = not moves
        self.inbox = "INBOX"
        self.archive_box = "Archive"
        self.trash_box = "Trash"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def connect(self):
        pass

    def close(self):
        pass

    def _note(self, what, refs, extra=None):
        self.calls.append((what, len(refs), extra))

    def archive(self, refs):
        self._note("archive", refs)
        return self.archive_box if self.moves_on_action else "[Gmail]/All Mail"

    def unarchive(self, refs):
        self._note("unarchive", refs)

    def trash(self, refs):
        self._note("trash", refs)
        return self.trash_box

    def untrash(self, refs):
        self._note("untrash", refs)

    def mark_read(self, refs):
        self._note("mark_read", refs)

    def mark_unread(self, refs):
        self._note("mark_unread", refs)

    def star(self, refs):
        self._note("star", refs)

    def unstar(self, refs):
        self._note("unstar", refs)

    def apply_label(self, refs, name):
        self._note("apply_label", refs, name)
        return name if self.moves_on_action else "[Gmail]/All Mail"

    def remove_label(self, refs, name):
        self._note("remove_label", refs, name)
