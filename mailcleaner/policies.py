"""Compound rules ("policies") and the filter language behind them.

A *filter* is a small JSON object over a fixed set of fields:

    {"category": "newsletter", "older_than_days": 180, "unread": true}

and `compile_filter` is the only thing that turns one into SQL. Everything that
lets a person - or a model - describe a selection goes through it: policies,
the plain-English search, the MCP server and the browser's "filter" target. So
a filter can never name a column that is not listed here, and every value is a
bound parameter.

A *policy* is a named filter plus a mailbox action. Policies suggest by default:
`mclean rules run` previews what each one matches. Only policies marked `auto`
are ever applied, only by an explicit `mclean rules apply` (or a scheduled run
the user installed with `--apply-rules`), and only on an account with actions
enabled. Protected mail is excluded exactly as it is for every bulk action, and
trash is not a policy action at all.
"""

from __future__ import annotations

import json
import sqlite3
import time

from . import actions

#: Mailbox actions a policy may take. Trash is deliberately absent: nothing in
#: this tool trashes mail without a person looking at the preview first.
POLICY_ACTIONS = (actions.ARCHIVE, actions.MARK_READ, actions.LABEL, actions.STAR)

DAY = 86400

#: field -> (kind, help). Kinds: str, strs (one or a list), int, bool.
FIELDS: dict[str, tuple[str, str]] = {
    "sender": ("strs", "exact sender address(es)"),
    "domain": ("strs", "sender domain(s); also matches their subdomains"),
    "list_id": ("str", "text contained in the List-Id header"),
    "category": ("strs", "classifier category, e.g. newsletter, promotion"),
    "attention": ("strs", "action required | potentially important | "
                          "read later | informational | no attention"),
    "retention": ("strs", "protected | keep | archive | review | cleanup candidate"),
    "subject_contains": ("str", "text contained in the subject"),
    "older_than_days": ("int", "received more than N days ago"),
    "newer_than_days": ("int", "received within the last N days"),
    "larger_than_kb": ("int", "message size above N kilobytes"),
    "unread": ("bool", "unread (true) or read (false)"),
    "in_inbox": ("bool", "still in the inbox"),
    "starred": ("bool", "starred / flagged"),
    "protected": ("bool", "protected by the classifier or a rule"),
    "has_attachment": ("bool", "has an attachment"),
    "has_unsubscribe": ("bool", "carries a List-Unsubscribe header"),
}

#: Bounds that keep a filter sane whoever wrote it.
MAX_LIST = 200
MAX_TEXT = 200


class FilterError(ValueError):
    """A filter that names an unknown field or carries a bad value."""


def _strs(field: str, value) -> list[str]:
    vals = value if isinstance(value, list) else [value]
    out = [str(v).strip().lower() for v in vals if str(v).strip()]
    if not out:
        raise FilterError(f"{field} needs a value")
    if len(out) > MAX_LIST:
        raise FilterError(f"{field} lists more than {MAX_LIST} values")
    if any(len(v) > MAX_TEXT for v in out):
        raise FilterError(f"{field} value too long")
    return out


def _int(field: str, value) -> int:
    if isinstance(value, bool):
        raise FilterError(f"{field} must be a number")
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise FilterError(f"{field} must be a number")
    if not 0 <= n <= 100_000_000:
        raise FilterError(f"{field} out of range")
    return n


def _bool(field: str, value) -> bool:
    if isinstance(value, bool):
        return value
    if str(value).lower() in ("true", "1", "yes"):
        return True
    if str(value).lower() in ("false", "0", "no"):
        return False
    raise FilterError(f"{field} must be true or false")


def _like(text: str) -> str:
    """A LIKE pattern matching `text` literally anywhere."""
    esc = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def normalize_filter(f: dict) -> dict:
    """Validate a filter and return it with canonical value types."""
    if not isinstance(f, dict):
        raise FilterError("a filter is a JSON object")
    unknown = sorted(set(f) - set(FIELDS))
    if unknown:
        raise FilterError(f"unknown filter field(s): {', '.join(unknown)}")
    out: dict = {}
    for field, value in f.items():
        if value is None or value == "" or value == []:
            continue
        kind = FIELDS[field][0]
        if kind == "strs":
            out[field] = _strs(field, value)
        elif kind == "str":
            out[field] = _strs(field, value)[0]
        elif kind == "int":
            out[field] = _int(field, value)
        else:
            out[field] = _bool(field, value)
    if not out:
        raise FilterError("a filter needs at least one condition")
    return out


def compile_filter(f: dict, now: int | None = None) -> tuple[str, tuple]:
    """Filter -> (WHERE clause, params). The only filter-to-SQL path there is."""
    f = normalize_filter(f)
    now = now or int(time.time())
    parts: list[str] = []
    params: list = []

    def any_of(col: str, vals: list[str]) -> None:
        parts.append(f"{col} IN ({','.join('?' * len(vals))})")
        params.extend(vals)

    for field, v in f.items():
        if field == "sender":
            any_of("from_email", v)
        elif field == "domain":
            ors = []
            for d in v:
                ors.append("(from_domain = ? OR from_domain LIKE ? ESCAPE '\\')")
                params.extend([d, "%." + d.replace("%", "\\%").replace("_", "\\_")])
            parts.append("(" + " OR ".join(ors) + ")")
        elif field == "list_id":
            parts.append("LOWER(list_id) LIKE ? ESCAPE '\\'")
            params.append(_like(v))
        elif field in ("category", "attention", "retention"):
            any_of(field, v)
        elif field == "subject_contains":
            parts.append("LOWER(subject) LIKE ? ESCAPE '\\'")
            params.append(_like(v))
        elif field == "older_than_days":
            parts.append("received_at < ?")
            params.append(now - v * DAY)
        elif field == "newer_than_days":
            parts.append("received_at >= ?")
            params.append(now - v * DAY)
        elif field == "larger_than_kb":
            parts.append("size > ?")
            params.append(v * 1024)
        else:
            col = {
                "unread": "is_unread", "in_inbox": "is_inbox",
                "starred": "is_starred", "protected": "protected",
                "has_attachment": "has_attachment", "has_unsubscribe": "unsubscribe",
            }[field]
            parts.append(f"{col} = ?")
            params.append(1 if v else 0)
    return " AND ".join(parts), tuple(params)


def describe(f: dict) -> str:
    """One line a person can read back: what this filter selects."""
    f = normalize_filter(f)
    bits = []
    for field, v in f.items():
        if isinstance(v, bool):
            bits.append(field.replace("_", " ") if v else "not " + field.replace("_", " "))
        elif isinstance(v, list):
            bits.append(f"{field.replace('_', ' ')} {' / '.join(v)}")
        elif field.endswith("_days"):
            bits.append(f"{field.split('_')[0]} than {v} days")
        elif field == "larger_than_kb":
            bits.append(f"larger than {v} KB")
        else:
            bits.append(f"{field.replace('_', ' ')} \"{v}\"")
    return ", ".join(bits)


# -- policies ---------------------------------------------------------------


def add(conn: sqlite3.Connection, name: str, conditions: dict, action: str,
        label: str | None = None, auto: bool = False) -> int:
    if action not in POLICY_ACTIONS:
        raise FilterError(
            f"policy action must be one of {', '.join(POLICY_ACTIONS)} "
            "(trash is never automatic)"
        )
    if action == actions.LABEL and not (label or "").strip():
        raise FilterError("a label policy needs a label name")
    conditions = normalize_filter(conditions)
    name = (name or "").strip()[:80] or describe(conditions)[:80]
    cur = conn.execute(
        "INSERT INTO policies(name,conditions,action,label,auto,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (name, json.dumps(conditions), action, (label or "").strip() or None,
         int(bool(auto)), int(time.time())),
    )
    conn.commit()
    return cur.lastrowid


def remove(conn: sqlite3.Connection, policy_id: int) -> bool:
    cur = conn.execute("DELETE FROM policies WHERE id=?", (policy_id,))
    conn.commit()
    return cur.rowcount > 0


def set_auto(conn: sqlite3.Connection, policy_id: int, auto: bool) -> bool:
    cur = conn.execute("UPDATE policies SET auto=? WHERE id=?",
                       (int(bool(auto)), policy_id))
    conn.commit()
    return cur.rowcount > 0


def get(conn: sqlite3.Connection, policy_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM policies WHERE id=?", (policy_id,)).fetchone()
    return _row(r) if r else None


def _row(r) -> dict:
    d = dict(r)
    d["conditions"] = json.loads(d["conditions"] or "{}")
    d["auto"] = bool(d["auto"])
    d["summary"] = describe(d["conditions"])
    return d


def where_for(policy: dict) -> tuple[str, tuple]:
    """A policy's selection, as an action would see it."""
    where, params = compile_filter(policy["conditions"])
    if policy["action"] == actions.MARK_READ:
        where += " AND is_unread=1"
    elif policy["action"] == actions.ARCHIVE:
        where += " AND is_inbox=1"
    elif policy["action"] == actions.STAR:
        where += " AND is_starred=0"
    return where, params


def list_all(conn: sqlite3.Connection, with_counts: bool = True) -> list[dict]:
    out = []
    for r in conn.execute("SELECT * FROM policies ORDER BY id"):
        d = _row(r)
        if with_counts:
            where, params = where_for(d)
            c = conn.execute(
                f"SELECT COUNT(*) n, SUM(size) b, SUM(protected) p FROM emails "
                f"WHERE {where}", params
            ).fetchone()
            d["matches"] = (c["n"] or 0) - (c["p"] or 0)
            d["protected"] = c["p"] or 0
            d["bytes"] = c["b"] or 0
        out.append(d)
    return out


def apply(conn: sqlite3.Connection, client, only_auto: bool = True,
          policy_id: int | None = None) -> list[dict]:
    """Run policies against the mailbox. Each one is its own undoable batch."""
    results = []
    for p in list_all(conn, with_counts=False):
        if policy_id is not None and p["id"] != policy_id:
            continue
        if only_auto and not p["auto"]:
            continue
        where, params = where_for(p)
        res = actions.run(conn, client, p["action"], where, params,
                          label=p["label"], include_protected=False)
        conn.execute("UPDATE policies SET last_run=? WHERE id=?",
                     (int(time.time()), p["id"]))
        conn.commit()
        results.append({"policy": p["name"], "id": p["id"], **res})
    return results
