"""Read-only queries that back every screen."""

from __future__ import annotations

import sqlite3
import time

DAY = 86400


def _cut(days: int) -> int:
    return int(time.time()) - days * DAY


def overview(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """SELECT COUNT(*) total,
                  SUM(is_unread) unread,
                  SUM(is_inbox) inbox,
                  SUM(size) bytes,
                  SUM(protected) protected,
                  MIN(received_at) oldest,
                  MAX(received_at) newest,
                  COUNT(DISTINCT from_domain) domains,
                  COUNT(DISTINCT from_email) senders
           FROM emails"""
    ).fetchone()
    d = dict(row)
    pool = conn.execute(
        "SELECT COUNT(*) c, SUM(size) b, COUNT(DISTINCT from_email) s FROM emails "
        "WHERE retention='cleanup candidate' AND protected=0"
    ).fetchone()
    d["cleanup"] = pool["c"]
    d["cleanup_bytes"] = pool["b"] or 0
    d["cleanup_senders"] = pool["s"]
    d["attention"] = conn.execute(
        "SELECT COUNT(*) c FROM emails "
        "WHERE attention IN ('action required','potentially important') AND is_unread=1"
    ).fetchone()["c"]
    return d


_GROUP_SELECT = """
SELECT {col} AS key,
       COUNT(*)                                              AS emails,
       SUM(is_unread)                                        AS unread,
       SUM(is_inbox)                                         AS inbox,
       SUM(CASE WHEN received_at > ? THEN 1 ELSE 0 END)      AS d7,
       SUM(CASE WHEN received_at > ? THEN 1 ELSE 0 END)      AS d30,
       SUM(CASE WHEN received_at > ? THEN 1 ELSE 0 END)      AS d90,
       SUM(size)                                             AS bytes,
       MAX(received_at)                                      AS last_seen,
       MIN(received_at)                                      AS first_seen,
       SUM(protected)                                        AS protected
FROM emails WHERE {col} != '' {extra}
GROUP BY {col} ORDER BY {order} DESC LIMIT ?
"""


def _grouped(conn, col, limit=200, order="emails", extra="", params=()) -> list[dict]:
    sql = _GROUP_SELECT.format(col=col, extra=extra, order=order)
    rows = conn.execute(
        sql, (_cut(7), _cut(30), _cut(90), *params, limit)
    ).fetchall()
    return [_with_trend(dict(r)) for r in rows]


def _with_trend(d: dict) -> dict:
    """Per-week rate now vs the preceding two months, plus a trend arrow."""
    recent = (d["d30"] or 0) / 4.3
    older = ((d["d90"] or 0) - (d["d30"] or 0)) / 8.6
    d["per_week"] = round(recent, 1)
    if older == 0 and recent == 0:
        d["trend"] = "-"
    elif older == 0:
        d["trend"] = "new"
    elif recent > older * 1.3:
        d["trend"] = "up"
    elif recent < older * 0.7:
        d["trend"] = "down"
    else:
        d["trend"] = "flat"
    d["dormant_days"] = (
        int((time.time() - d["last_seen"]) // DAY) if d.get("last_seen") else 0
    )
    return d


def domains(conn, limit=200, order="emails") -> list[dict]:
    return _grouped(conn, "from_domain", limit, order)


def senders(conn, limit=200, order="emails", domain: str | None = None) -> list[dict]:
    extra = "AND from_domain = ?" if domain else ""
    return _grouped(conn, "from_email", limit, order, extra, (domain,) if domain else ())


def sender(conn, email: str) -> dict | None:
    """The same grouped row the sender table shows, for exactly one address."""
    rows = _grouped(conn, "from_email", 1, "emails",
                    "AND from_email = ?", (email.lower(),))
    return rows[0] if rows else None


def categories(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT category AS key, COUNT(*) emails, SUM(is_unread) unread,
                  SUM(size) bytes, SUM(protected) protected, MAX(received_at) last_seen
           FROM emails GROUP BY category ORDER BY emails DESC"""
    ).fetchall()
    return [dict(r) for r in rows]


def category_breakdown(conn, where: str, params: tuple) -> list[dict]:
    rows = conn.execute(
        f"""SELECT category AS key, COUNT(*) emails, SUM(protected) protected
            FROM emails WHERE {where} GROUP BY category ORDER BY emails DESC""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


AGE_BUCKETS = [
    ("< 30 days", 0, 30),
    ("1-6 months", 30, 180),
    ("6-12 months", 180, 365),
    ("1-3 years", 365, 1095),
    ("3+ years", 1095, 100000),
]


def ages(conn) -> list[dict]:
    out = []
    now = int(time.time())
    for name, lo, hi in AGE_BUCKETS:
        r = conn.execute(
            "SELECT COUNT(*) emails, SUM(is_unread) unread, SUM(size) bytes, "
            "SUM(protected) protected FROM emails "
            "WHERE received_at <= ? AND received_at > ?",
            (now - lo * DAY, now - hi * DAY),
        ).fetchone()
        out.append({"key": name, **dict(r)})
    return out


FREQ_BUCKETS = [
    ("50+/week", 50, 1e9),
    ("10-50/week", 10, 50),
    ("1-10/week", 1, 10),
    ("<1/week", 0.001, 1),
    ("dormant", -1, 0.001),
]


def frequency(conn) -> list[dict]:
    buckets = {name: {"key": name, "senders": 0, "emails": 0, "unread": 0}
               for name, _, _ in FREQ_BUCKETS}
    for s in senders(conn, limit=100000):
        for name, lo, hi in FREQ_BUCKETS:
            if lo <= s["per_week"] < hi:
                b = buckets[name]
                b["senders"] += 1
                b["emails"] += s["emails"]
                b["unread"] += s["unread"] or 0
                break
    return list(buckets.values())


ATTENTION_ORDER = (
    "CASE attention WHEN 'action required' THEN 0 "
    "WHEN 'potentially important' THEN 1 WHEN 'read later' THEN 2 "
    "WHEN 'informational' THEN 3 ELSE 4 END"
)


def attention(conn, limit=300) -> list[dict]:
    rows = conn.execute(
        f"""SELECT * FROM emails
            WHERE is_unread=1 AND attention IN
                  ('action required','potentially important','read later')
            ORDER BY {ATTENTION_ORDER}, received_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def messages(conn, where: str = "1=1", params: tuple = (), limit=1000,
             order="received_at DESC") -> list[dict]:
    rows = conn.execute(
        f"SELECT * FROM emails WHERE {where} ORDER BY {order} LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def count(conn, where: str, params: tuple = ()) -> int:
    return conn.execute(f"SELECT COUNT(*) c FROM emails WHERE {where}", params).fetchone()["c"]


CLEANUP_SUGGESTIONS = [
    (
        "Ignored newsletters",
        "Newsletter, unread, older than 6 months, never starred",
        "category='newsletter' AND is_unread=1 AND protected=0 AND is_starred=0 "
        "AND received_at < strftime('%s','now','-180 days')",
    ),
    (
        "Old promotions",
        "Marketing mail older than 90 days",
        "category='promotion' AND protected=0 AND is_starred=0 "
        "AND received_at < strftime('%s','now','-90 days')",
    ),
    (
        "Social notifications",
        "Social network noise older than 90 days",
        "category='social' AND protected=0 AND is_starred=0 "
        "AND received_at < strftime('%s','now','-90 days')",
    ),
    (
        "Ancient unread",
        "Unread, unstarred, unprotected, older than 2 years",
        "is_unread=1 AND protected=0 AND is_starred=0 "
        "AND received_at < strftime('%s','now','-730 days')",
    ),
    (
        "Automated system mail",
        "Machine-generated mail older than 60 days",
        "category='automated' AND protected=0 AND is_starred=0 "
        "AND received_at < strftime('%s','now','-60 days')",
    ),
    (
        "Large mail",
        "Anything over 2 MB that is not protected",
        "size > 2097152 AND protected=0",
    ),
    (
        "Still in inbox, no attention needed",
        "Inbox clutter the classifier rates as needing nothing",
        "is_inbox=1 AND attention='no attention' AND protected=0",
    ),
]


def suggestions(conn) -> list[dict]:
    out = []
    for name, desc, where in CLEANUP_SUGGESTIONS:
        r = conn.execute(
            f"SELECT COUNT(*) emails, SUM(size) bytes FROM emails WHERE {where}"
        ).fetchone()
        out.append({"key": name, "desc": desc, "where": where,
                    "emails": r["emails"], "bytes": r["bytes"] or 0})
    return out


def human_size(n: int | None) -> str:
    n = n or 0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def ago(ts: int | None) -> str:
    if not ts:
        return "-"
    d = (time.time() - ts) / DAY
    if d < 1:
        return "today"
    if d < 2:
        return "yesterday"
    if d < 30:
        return f"{int(d)}d"
    if d < 365:
        return f"{int(d / 30)}mo"
    return f"{d / 365:.1f}y"
