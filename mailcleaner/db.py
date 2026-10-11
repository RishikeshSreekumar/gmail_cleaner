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
    unsub_url       TEXT,             -- https List-Unsubscribe target, if any
    unsub_post      INTEGER,          -- RFC 8058 one-click POST supported
    unsub_mailto    TEXT,             -- mailto: List-Unsubscribe target, if any
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

-- Compound rules ("policies"): conditions over the index plus a mailbox
-- action. They suggest by default; `auto` ones run only when asked to.
CREATE TABLE IF NOT EXISTS policies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    conditions  TEXT NOT NULL,        -- json object, see policies.FIELDS
    action      TEXT NOT NULL,        -- archive | mark_read | label | star
    label       TEXT,
    auto        INTEGER DEFAULT 0,
    created_at  INTEGER,
    last_run    INTEGER
);

-- What an LLM proposed. Nothing here takes effect until the user accepts it,
-- at which point it becomes an ordinary row in `rules`.
CREATE TABLE IF NOT EXISTS llm_suggestions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    match_type  TEXT NOT NULL,        -- sender | domain
    match_value TEXT NOT NULL,
    action      TEXT NOT NULL,        -- protect | ignore | category
    category    TEXT,
    reason      TEXT,
    confidence  REAL,
    model       TEXT,
    status      TEXT DEFAULT 'pending', -- pending | accepted | rejected
    created_at  INTEGER,
    UNIQUE(match_type, match_value, action, category)
);

CREATE TABLE IF NOT EXISTS unsubscribes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sender      TEXT,
    method      TEXT,                 -- one-click | link | mailto
    target      TEXT,                 -- host only for one-click; never the full URL
    status      TEXT,                 -- ok | failed | manual
    detail      TEXT,
    ts          INTEGER
);
"""

#: Columns added after 0.1.0. An older index gets them in place; nothing else
#: about it changes, and the next sync fills them in.
ADDED_COLUMNS = {
    "emails": [("unsub_url", "TEXT"), ("unsub_post", "INTEGER"),
               ("unsub_mailto", "TEXT")],
}


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
    _add_columns(conn)
    conn.executescript(SCHEMA)
    if account is not None:
        # The classifier reads these from the index so a reclassify needs
        # nothing but the connection.
        set_state(conn, "self_emails", [account.email.lower()])
        set_state(conn, "locales", list(getattr(account, "locales", []) or []))
    return conn


def _add_columns(conn: sqlite3.Connection) -> None:
    for table, cols in ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not have:
            continue  # fresh index: the schema creates them
        for name, kind in cols:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
    conn.commit()


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
