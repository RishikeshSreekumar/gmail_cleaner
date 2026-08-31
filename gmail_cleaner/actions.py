"""Mutating operations. Every one is reversible and every one is logged.

There is deliberately no permanent-delete operation anywhere in this file.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid

from .imapclient import GmailImap

ARCHIVE = "archive"
TRASH = "trash"
MARK_READ = "mark_read"
STAR = "star"
LABEL = "label"

INBOX_LABEL = "\\Inbox"
TRASH_LABEL = "\\Trash"


class ProtectedMessages(Exception):
    """Raised when a bulk action would touch protected mail without an override."""

    def __init__(self, n: int):
        super().__init__(f"{n} protected messages in selection")
        self.count = n


def preview(conn: sqlite3.Connection, where: str, params: tuple = ()) -> dict:
    """What a bulk action would actually do — shown before every confirmation."""
    from . import stats

    total = stats.count(conn, where, params)
    prot = stats.count(conn, f"({where}) AND protected=1", params)
    span = conn.execute(
        f"SELECT MIN(received_at) a, MAX(received_at) b, SUM(size) s "
        f"FROM emails WHERE {where}", params
    ).fetchone()
    return {
        "total": total,
        "protected": prot,
        "eligible": total - prot,
        "categories": stats.category_breakdown(conn, where, params),
        "oldest": span["a"],
        "newest": span["b"],
        "bytes": span["s"] or 0,
        "samples": stats.messages(
            conn, f"({where}) AND protected=0", params, limit=8
        ),
    }


def _select(conn, where, params, include_protected: bool):
    clause = where if include_protected else f"({where}) AND protected=0"
    rows = conn.execute(
        f"SELECT gm_msgid, uid, labels, is_unread, is_inbox, is_starred, protected "
        f"FROM emails WHERE {clause}", params
    ).fetchall()
    return [dict(r) for r in rows]


def _prev_state(r: dict) -> str:
    """Snapshot enough to reverse the action for exactly the messages it changed."""
    return json.dumps({
        "labels": json.loads(r["labels"] or "[]"),
        "unread": r["is_unread"],
        "inbox": r["is_inbox"],
        "starred": r.get("is_starred", 0),
    })


def _log(conn, batch_id, rows, action, detail):
    now = int(time.time())
    conn.executemany(
        "INSERT INTO action_log(batch_id,gm_msgid,uid,action,prev_labels,detail,ts) "
        "VALUES(?,?,?,?,?,?,?)",
        [(batch_id, r["gm_msgid"], r["uid"], action, _prev_state(r), detail, now)
         for r in rows],
    )
    conn.commit()


def run(
    conn: sqlite3.Connection,
    client: GmailImap,
    action: str,
    where: str,
    params: tuple = (),
    label: str | None = None,
    include_protected: bool = False,
    progress=None,
) -> dict:
    """Apply `action` to every message matching `where`. Returns a batch summary."""
    rows = _select(conn, where, params, include_protected)
    if not include_protected:
        blocked = conn.execute(
            f"SELECT COUNT(*) c FROM emails WHERE ({where}) AND protected=1", params
        ).fetchone()["c"]
    else:
        blocked = 0
    if not rows:
        return {"batch_id": None, "count": 0, "skipped": blocked}

    uids = [r["uid"] for r in rows]
    batch_id = uuid.uuid4().hex[:12]

    if action == ARCHIVE:
        client.remove_labels(uids, [INBOX_LABEL])
        conn.executemany(
            "UPDATE emails SET is_inbox=0 WHERE gm_msgid=?",
            [(r["gm_msgid"],) for r in rows],
        )
    elif action == TRASH:
        client.add_labels(uids, [TRASH_LABEL])
        conn.executemany(
            "DELETE FROM emails WHERE gm_msgid=?", [(r["gm_msgid"],) for r in rows]
        )
    elif action == MARK_READ:
        client.add_flags(uids, ["\\Seen"])
        conn.executemany(
            "UPDATE emails SET is_unread=0 WHERE gm_msgid=?",
            [(r["gm_msgid"],) for r in rows],
        )
    elif action == STAR:
        client.add_flags(uids, ["\\Flagged"])
        conn.executemany(
            "UPDATE emails SET is_starred=1, protected=1 WHERE gm_msgid=?",
            [(r["gm_msgid"],) for r in rows],
        )
    elif action == LABEL:
        if not label:
            raise ValueError("label action needs a label name")
        client.create_label(label)
        client.add_labels(uids, [label])
        for r in rows:
            labels = json.loads(r["labels"] or "[]")
            if label not in labels:
                labels.append(label)
            conn.execute(
                "UPDATE emails SET labels=? WHERE gm_msgid=?",
                (json.dumps(labels), r["gm_msgid"]),
            )
    else:
        raise ValueError(f"unknown action {action}")

    conn.commit()
    _log(conn, batch_id, rows, action, label or "")
    if progress:
        progress(len(rows), len(rows))
    return {"batch_id": batch_id, "count": len(rows), "skipped": blocked,
            "action": action, "label": label}


def undo(conn: sqlite3.Connection, client: GmailImap, batch_id: str) -> int:
    """Reverse a logged batch."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM action_log WHERE batch_id=? AND undone=0", (batch_id,)
    )]
    if not rows:
        return 0
    action = rows[0]["action"]
    uids = [r["uid"] for r in rows]
    for r in rows:
        try:
            r["prev"] = json.loads(r["prev_labels"] or "{}")
        except json.JSONDecodeError:
            r["prev"] = {}

    def was(field: str) -> list[dict]:
        """Only the messages the action actually changed."""
        return [r for r in rows if r["prev"].get(field)]

    if action == ARCHIVE:
        restore = was("inbox")
        if restore:
            client.add_labels([r["uid"] for r in restore], [INBOX_LABEL])
            conn.executemany("UPDATE emails SET is_inbox=1 WHERE gm_msgid=?",
                             [(r["gm_msgid"],) for r in restore])
    elif action == TRASH:
        # Trashed mail leaves All Mail, so find it again by permanent Gmail id.
        found = client.uids_for_msgids([r["gm_msgid"] for r in rows], client.trash)
        if found:
            client._select(client.trash, readonly=False)
            client._store(list(found.values()), "-X-GM-LABELS", f'("{TRASH_LABEL}")')
    elif action == MARK_READ:
        restore = was("unread")
        if restore:
            client.remove_flags([r["uid"] for r in restore], ["\\Seen"])
            conn.executemany("UPDATE emails SET is_unread=1 WHERE gm_msgid=?",
                             [(r["gm_msgid"],) for r in restore])
    elif action == STAR:
        restore = [r for r in rows if not r["prev"].get("starred")]
        if restore:
            client.remove_flags([r["uid"] for r in restore], ["\\Flagged"])
            conn.executemany("UPDATE emails SET is_starred=0 WHERE gm_msgid=?",
                             [(r["gm_msgid"],) for r in restore])
    elif action == LABEL:
        client.remove_labels(uids, [rows[0]["detail"]])

    conn.execute("UPDATE action_log SET undone=1 WHERE batch_id=?", (batch_id,))
    conn.commit()
    return len(rows)


def batches(conn: sqlite3.Connection, limit=50) -> list[dict]:
    rows = conn.execute(
        "SELECT batch_id, action, detail, COUNT(*) n, MAX(ts) ts, MAX(undone) undone "
        "FROM action_log GROUP BY batch_id ORDER BY ts DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def add_rule(conn, match_type: str, match_value: str, action: str, category=None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO rules(match_type,match_value,action,category,created_at) "
        "VALUES(?,?,?,?,?)",
        (match_type, match_value.lower(), action, category, int(time.time())),
    )
    conn.commit()


def delete_rule(conn, rule_id: int) -> None:
    conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))
    conn.commit()
