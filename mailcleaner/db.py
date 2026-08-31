"""SQLite index, one file per account.

The mailbox is the source of truth; deleting an index loses nothing. Each
account keeps its own database under ~/.mailcleaner/accounts/<id>/index.db, so
no query in this app can ever mix two mailboxes together.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    msg_key         TEXT PRIMARY KEY, -- provider message id, or a hash of Message-ID
    uid             INTEGER,
    folder          TEXT,             -- mailbox the message currently lives in
    message_id      TEXT,             -- RFC 5322 Message-ID, for re-locating it
    thread_key      TEXT,
    received_at     INTEGER,          -- unix seconds
    from_name       TEXT,
    from_email      TEXT,
    from_domain     TEXT,
    to_emails       TEXT,
    subject         TEXT,
    size            INTEGER,
    labels          TEXT,             -- json array of labels/folders
    is_unread       INTEGER,
    is_inbox        INTEGER,
    is_starred      INTEGER,
    is_important    INTEGER,
    list_id         TEXT,
    unsubscribe     INTEGER,
    has_attachment  INTEGER,
    category        TEXT,
    subcategory     TEXT,
    attention       TEXT,
    retention       TEXT,
    protected       INTEGER DEFAULT 0,
    confidence      REAL,
    reasons         TEXT,
    synced_at       INTEGER
);
CREATE INDEX IF NOT EXISTS idx_emails_domain   ON emails(from_domain);
CREATE INDEX IF NOT EXISTS idx_emails_sender   ON emails(from_email);
CREATE INDEX IF NOT EXISTS idx_emails_date     ON emails(received_at);
CREATE INDEX IF NOT EXISTS idx_emails_category ON emails(category);
CREATE INDEX IF NOT EXISTS idx_emails_folder   ON emails(folder, uid);

CREATE TABLE IF NOT EXISTS rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_type  TEXT NOT NULL,        -- sender | domain | list_id
    match_value TEXT NOT NULL,
    action      TEXT NOT NULL,        -- protect | ignore | category
    category    TEXT,
    created_at  INTEGER,
    UNIQUE(match_type, match_value, action)
);

CREATE TABLE IF NOT EXISTS action_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    TEXT,
    msg_key     TEXT,
    uid         INTEGER,
    folder      TEXT,                 -- where it was before the action
    message_id  TEXT,
    action      TEXT,
    prev_labels TEXT,
    detail      TEXT,
    ts          INTEGER,
    undone      INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_log_batch ON action_log(batch_id);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(account=None, path: Path | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the index for `account`, or an explicit path."""
    if path is None:
        if account is None:
            raise ValueError("db.connect needs an account or a path")
        path = account.db_path
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _drop_stale_schema(conn)
    conn.executescript(SCHEMA)
    return conn


def _drop_stale_schema(conn: sqlite3.Connection) -> None:
    """An index written by the Gmail-only version keyed messages on gm_msgid.

    The index is disposable and re-syncing rebuilds it in full, so the honest
    thing is to discard the old tables rather than half-migrate them.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(emails)")}
    if cols and "msg_key" not in cols:
        conn.executescript(
            "DROP TABLE IF EXISTS emails;"
            "DROP TABLE IF EXISTS action_log;"
            "DELETE FROM sync_state;"
        )
        conn.commit()


def get_state(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM sync_state WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_state(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO sync_state(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )
    conn.commit()
