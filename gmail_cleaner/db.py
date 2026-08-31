"""SQLite index. Gmail is the source of truth; deleting this file loses nothing."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    gm_msgid        TEXT PRIMARY KEY,
    uid             INTEGER,
    gm_thrid        TEXT,
    received_at     INTEGER,          -- unix seconds
    from_name       TEXT,
    from_email      TEXT,
    from_domain     TEXT,
    to_emails       TEXT,
    subject         TEXT,
    size            INTEGER,
    labels          TEXT,             -- json array of gmail labels
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
CREATE INDEX IF NOT EXISTS idx_emails_uid      ON emails(uid);

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
    gm_msgid    TEXT,
    uid         INTEGER,
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


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


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
