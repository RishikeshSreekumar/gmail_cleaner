"""Fetch Gmail metadata into the local index."""

from __future__ import annotations

import json
import sqlite3
import time
from email.utils import getaddresses, parsedate_to_datetime

from . import db
from .classify import apply_rules, classify
from .imapclient import GmailImap, RawMessage

BATCH = 200


def _addr_parts(header: str) -> tuple[str, str, str]:
    pairs = getaddresses([header or ""])
    name, addr = (pairs[0] if pairs else ("", ""))
    addr = addr.strip().lower()
    domain = addr.split("@")[-1] if "@" in addr else ""
    return name.strip(), addr, domain


def normalize(raw: RawMessage) -> dict:
    h = raw.headers
    name, addr, domain = _addr_parts(h.get("from", ""))
    ts = None
    if h.get("date"):
        try:
            ts = int(parsedate_to_datetime(h["date"]).timestamp())
        except Exception:
            ts = None
    if ts is None and raw.internaldate:
        ts = int(raw.internaldate.timestamp())
    ts = ts or int(time.time())

    labels = raw.labels
    lower = [l.lower() for l in labels]
    ctype = (h.get("content-type") or "").lower()
    to_all = [a for _, a in getaddresses([h.get("to", ""), h.get("cc", "")]) if a]

    return {
        "gm_msgid": raw.gm_msgid,
        "uid": raw.uid,
        "gm_thrid": raw.gm_thrid,
        "received_at": ts,
        "from_name": name,
        "from_email": addr,
        "from_domain": domain,
        "to_emails": json.dumps(to_all[:20]),
        "subject": h.get("subject", ""),
        "size": raw.size,
        "labels": labels,
        "is_unread": int("\\Seen" not in raw.flags),
        "is_inbox": int(any(l == "\\inbox" for l in lower)),
        "is_starred": int("\\Flagged" in raw.flags or "\\starred" in lower),
        "is_important": int("\\important" in lower),
        "list_id": h.get("list-id", ""),
        "unsubscribe": int(bool(h.get("list-unsubscribe"))),
        "has_attachment": int("multipart/mixed" in ctype),
        "age_days": max(0, (int(time.time()) - ts) // 86400),
    }


COLUMNS = (
    "gm_msgid uid gm_thrid received_at from_name from_email from_domain to_emails "
    "subject size labels is_unread is_inbox is_starred is_important list_id "
    "unsubscribe has_attachment category subcategory attention retention protected "
    "confidence reasons synced_at"
).split()


def _row_tuple(m: dict) -> tuple:
    m = dict(m)
    m["labels"] = json.dumps(m["labels"])
    m["synced_at"] = int(time.time())
    return tuple(m.get(c) for c in COLUMNS)


def upsert(conn: sqlite3.Connection, rows: list[dict]) -> None:
    placeholders = ",".join("?" * len(COLUMNS))
    updates = ",".join(f"{c}=excluded.{c}" for c in COLUMNS if c != "gm_msgid")
    conn.executemany(
        f"INSERT INTO emails ({','.join(COLUMNS)}) VALUES ({placeholders}) "
        f"ON CONFLICT(gm_msgid) DO UPDATE SET {updates}",
        [_row_tuple(r) for r in rows],
    )
    conn.commit()


def load_rules(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM rules")]


def enrich(m: dict, rules: list[dict]) -> dict:
    v = apply_rules(classify(m), rules, m)
    m = dict(m)
    m.update(
        category=v.category,
        subcategory=v.subcategory,
        attention=v.attention,
        retention=v.retention,
        protected=int(v.protected),
        confidence=v.confidence,
        reasons="; ".join(v.reasons),
    )
    return m


def sync(
    conn: sqlite3.Connection,
    client: GmailImap,
    days: int,
    full: bool = False,
    progress=None,
) -> int:
    """Pull metadata for the last `days` days. Incremental unless `full`."""
    uidvalidity = client.uidvalidity()
    stored_validity = db.get_state(conn, "uidvalidity")
    last_uid = db.get_state(conn, "last_uid", 0)
    if full or stored_validity != uidvalidity:
        last_uid = 0
    db.set_state(conn, "uidvalidity", uidvalidity)

    uids = client.search_since(days, min_uid=max(1, last_uid))
    # Re-check the newest 500 known messages so flag/label changes land too.
    if last_uid and not full:
        known = [
            r["uid"]
            for r in conn.execute(
                "SELECT uid FROM emails ORDER BY uid DESC LIMIT 500"
            )
        ]
        uids = sorted(set(uids) | set(known))

    rules = load_rules(conn)
    total = len(uids)
    done = 0
    highest = last_uid
    for i in range(0, total, BATCH):
        chunk = uids[i : i + BATCH]
        raws = client.fetch_metadata(chunk)
        rows = [enrich(normalize(r), rules) for r in raws]
        if rows:
            upsert(conn, rows)
            highest = max(highest, max(r["uid"] for r in rows))
        done += len(chunk)
        if progress:
            progress(done, total)
    db.set_state(conn, "last_uid", highest)
    db.set_state(conn, "last_sync", int(time.time()))
    db.set_state(conn, "sync_days", days)
    return total


def reclassify_all(conn: sqlite3.Connection, progress=None) -> int:
    """Re-run the classifier over the existing index (after rule changes)."""
    rules = load_rules(conn)
    rows = conn.execute("SELECT * FROM emails").fetchall()
    now = int(time.time())
    updates = []
    for r in rows:
        m = dict(r)
        m["labels"] = json.loads(m["labels"] or "[]")
        m["age_days"] = max(0, (now - (m["received_at"] or now)) // 86400)
        e = enrich(m, rules)
        updates.append(
            (e["category"], e["subcategory"], e["attention"], e["retention"],
             e["protected"], e["confidence"], e["reasons"], e["gm_msgid"])
        )
    conn.executemany(
        "UPDATE emails SET category=?,subcategory=?,attention=?,retention=?,"
        "protected=?,confidence=?,reasons=? WHERE gm_msgid=?",
        updates,
    )
    conn.commit()
    return len(updates)
