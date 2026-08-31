"""Mutating operations. Every one is reversible and every one is logged.

There is deliberately no permanent-delete operation anywhere in this file, for
any provider. "Trash" means the provider's own Trash folder or label, which the
user can empty themselves whenever they choose.

The provider-specific mechanics live in `providers/`: on Gmail these are label
edits and a message never moves, while on folder-shaped servers archive, trash
and label are IMAP MOVEs that change the message's UID. This module papers over
that difference by recording, for every affected message, where it was and how
to find it again (its Message-ID), so undo works either way.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid

from .providers import MsgRef

ARCHIVE = "archive"
TRASH = "trash"
MARK_READ = "mark_read"
STAR = "star"
LABEL = "label"

ALL_ACTIONS = (ARCHIVE, TRASH, MARK_READ, STAR, LABEL)


class ProtectedMessages(Exception):
    """Raised when a bulk action would touch protected mail without an override."""

    def __init__(self, n: int):
        super().__init__(f"{n} protected messages in selection")
        self.count = n


def preview(conn: sqlite3.Connection, where: str, params: tuple = ()) -> dict:
    """What a bulk action would actually do - shown before every confirmation."""
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
        f"SELECT msg_key, uid, folder, message_id, labels, is_unread, is_inbox, "
        f"is_starred, protected FROM emails WHERE {clause}", params
    ).fetchall()
    return [dict(r) for r in rows]


def _refs(rows: list[dict]) -> list[MsgRef]:
    return [MsgRef(r["msg_key"], r["uid"] or 0, r.get("folder") or "",
                   r.get("message_id") or "") for r in rows]


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
        "INSERT INTO action_log(batch_id,msg_key,uid,folder,message_id,action,"
        "prev_labels,detail,ts) VALUES(?,?,?,?,?,?,?,?,?)",
        [(batch_id, r["msg_key"], r["uid"], r.get("folder") or "",
          r.get("message_id") or "", action, _prev_state(r), detail, now)
         for r in rows],
    )
    conn.commit()


def _mark_moved(conn, rows, dest: str) -> None:
    """The message is in a new mailbox and its old UID is void until next sync."""
    conn.executemany(
        "UPDATE emails SET folder=?, uid=0 WHERE msg_key=?",
        [(dest, r["msg_key"]) for r in rows],
    )


def run(
    conn: sqlite3.Connection,
    client,
    action: str,
    where: str,
    params: tuple = (),
    label: str | None = None,
    include_protected: bool = False,
    progress=None,
) -> dict:
    """Apply `action` to every message matching `where`. Returns a batch summary."""
    rows = _select(conn, where, params, include_protected)
    if action == ARCHIVE and getattr(client, "moves_on_action", False):
        # On folder-shaped servers archiving is an IMAP MOVE, so only inbox mail
        # is eligible: moving anything else would drag it out of the folder the
        # user deliberately filed it in. On Gmail this is a no-op either way.
        rows = [r for r in rows if r["is_inbox"]]
    if not include_protected:
        blocked = conn.execute(
            f"SELECT COUNT(*) c FROM emails WHERE ({where}) AND protected=1", params
        ).fetchone()["c"]
    else:
        blocked = 0
    if not rows:
        return {"batch_id": None, "count": 0, "skipped": blocked}

    refs = _refs(rows)
    keys = [(r["msg_key"],) for r in rows]
    batch_id = uuid.uuid4().hex[:12]
    moves = getattr(client, "moves_on_action", False)

    if action == ARCHIVE:
        dest = client.archive(refs)
        conn.executemany("UPDATE emails SET is_inbox=0 WHERE msg_key=?", keys)
        if moves:
            _mark_moved(conn, rows, dest)
    elif action == TRASH:
        client.trash(refs)
        # Trashed mail leaves the indexed set entirely; the log still holds
        # everything undo needs to fetch it back.
        conn.executemany("DELETE FROM emails WHERE msg_key=?", keys)
    elif action == MARK_READ:
        client.mark_read(refs)
        conn.executemany("UPDATE emails SET is_unread=0 WHERE msg_key=?", keys)
    elif action == STAR:
        client.star(refs)
        conn.executemany(
            "UPDATE emails SET is_starred=1, protected=1 WHERE msg_key=?", keys
        )
    elif action == LABEL:
        if not label:
            raise ValueError("label action needs a label name")
        dest = client.apply_label(refs, label)
        for r in rows:
            labels = json.loads(r["labels"] or "[]")
            if label not in labels:
                labels.append(label)
            conn.execute(
                "UPDATE emails SET labels=? WHERE msg_key=?",
                (json.dumps(labels), r["msg_key"]),
            )
        if moves:
            _mark_moved(conn, rows, dest)
    else:
        raise ValueError(f"unknown action {action}")

    conn.commit()
    _log(conn, batch_id, rows, action, label or "")
    if progress:
        progress(len(rows), len(rows))
    return {"batch_id": batch_id, "count": len(rows), "skipped": blocked,
            "action": action, "label": label, "moved": bool(moves)}


def undo(conn: sqlite3.Connection, client, batch_id: str) -> int:
    """Reverse a logged batch."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM action_log WHERE batch_id=? AND undone=0", (batch_id,)
    )]
    if not rows:
        return 0
    action = rows[0]["action"]
    for r in rows:
        try:
            r["prev"] = json.loads(r["prev_labels"] or "{}")
        except json.JSONDecodeError:
            r["prev"] = {}

    def was(field: str) -> list[dict]:
        """Only the messages the action actually changed."""
        return [r for r in rows if r["prev"].get(field)]

    def restore_index(subset, sql):
        conn.executemany(sql, [(r["msg_key"],) for r in subset])

    if action == ARCHIVE:
        subset = was("inbox")
        if subset:
            client.unarchive(_refs(subset))
            restore_index(subset, "UPDATE emails SET is_inbox=1 WHERE msg_key=?")
    elif action == TRASH:
        client.untrash(_refs(rows))
    elif action == MARK_READ:
        subset = was("unread")
        if subset:
            client.mark_unread(_refs(subset))
            restore_index(subset, "UPDATE emails SET is_unread=1 WHERE msg_key=?")
    elif action == STAR:
        subset = [r for r in rows if not r["prev"].get("starred")]
        if subset:
            client.unstar(_refs(subset))
            restore_index(subset, "UPDATE emails SET is_starred=0 WHERE msg_key=?")
    elif action == LABEL:
        client.remove_label(_refs(rows), rows[0]["detail"])

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
