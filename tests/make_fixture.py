"""Generate a synthetic index so the UI can be exercised without a real mailbox."""

import json
import random
import time

from gmail_cleaner import db, sync

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


def build(conn, n=900, seed=7):
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
            "gm_msgid": str(10_000 + i), "uid": 100 + i, "gm_thrid": str(90_000 + i),
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
    db.set_state(conn, "last_uid", 100 + n)
    return len(rows)
