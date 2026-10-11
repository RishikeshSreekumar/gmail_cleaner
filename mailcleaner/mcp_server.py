"""A read-only MCP server over stdio, so an agent can ask about your mailbox.

    mclean mcp            # register this command with your MCP client

It speaks JSON-RPC 2.0, one message per line, with the standard library only.
Every tool reads the local index - the same metadata the dashboard shows - and
none of them can change anything: there is no archive, trash, label, rule or
unsubscribe tool here. Acting stays in `mclean` / `mclean gui`, behind their
previews and confirmation.

Selections use the whitelisted filter language from `policies`, so a client can
describe "unread newsletters older than six months" but can never send SQL.

Whatever the tools return goes to the MCP client and, through it, to whichever
model that client uses. That is the user's choice when they register the
server; nothing is sent anywhere until a client asks.
"""

from __future__ import annotations

import json
import sys
import traceback

from . import __version__, db, policies, stats, unsubscribe
from .accounts import Store

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

ACCOUNT_PROP = {
    "account": {"type": "string",
                "description": "Account id, address or label. Defaults to the active one."},
}
FILTER_PROP = {
    "filter": {
        "type": "object",
        "description": "Conditions, all of which must hold. Fields: " + "; ".join(
            f"{k} ({v[0]}): {v[1]}" for k, v in policies.FIELDS.items()),
        "properties": {
            k: ({"type": "array", "items": {"type": "string"}} if v[0] == "strs"
                else {"type": "string"} if v[0] == "str"
                else {"type": "integer"} if v[0] == "int"
                else {"type": "boolean"})
            for k, v in policies.FIELDS.items()
        },
        "additionalProperties": False,
    },
}
LIMIT_PROP = {"limit": {"type": "integer", "minimum": 1, "maximum": 200}}

TOOLS = [
    ("list_accounts", "List the mailboxes mailcleaner tracks, with size and last sync.",
     {}),
    ("mailbox_overview",
     "Totals for one mailbox: messages, unread, size, senders, protected, "
     "cleanup candidates, mail wanting attention.", ACCOUNT_PROP),
    ("top_senders", "The loudest senders with volume, weekly rate, trend, unread and size.",
     {**ACCOUNT_PROP, **LIMIT_PROP,
      "domain": {"type": "string", "description": "Only senders in this domain."}}),
    ("top_domains", "Sending domains by volume.", {**ACCOUNT_PROP, **LIMIT_PROP}),
    ("categories", "How the classifier split the mailbox, per category.", ACCOUNT_PROP),
    ("needs_attention",
     "Unread mail the classifier thinks matters: action required first.",
     {**ACCOUNT_PROP, **LIMIT_PROP}),
    ("cleanup_suggestions",
     "Ready-made safe cleanup batches with message counts and bytes freed.", ACCOUNT_PROP),
    ("trend_alerts", "Senders whose volume recently jumped.", ACCOUNT_PROP),
    ("digest", "What changed in the last N days.",
     {**ACCOUNT_PROP, "days": {"type": "integer", "minimum": 1, "maximum": 365}}),
    ("search_messages",
     "Message metadata (sender, subject, date, category, flags) matching a filter. "
     "Never bodies.", {**ACCOUNT_PROP, **FILTER_PROP, **LIMIT_PROP}),
    ("preview_selection",
     "What a bulk action on a filter would touch: counts, protected exclusions, "
     "categories, date span and examples. Read-only; nothing is changed.",
     {**ACCOUNT_PROP, **FILTER_PROP}),
    ("list_rules", "The user's protect/ignore rules and compound rules (policies).",
     ACCOUNT_PROP),
    ("unsubscribe_candidates",
     "Senders that offer an unsubscribe, and how (one-click, link, mailto).",
     {**ACCOUNT_PROP, **LIMIT_PROP}),
]

MESSAGE_FIELDS = ("from_name", "from_email", "subject", "received_at", "category",
                  "subcategory", "attention", "retention", "protected", "is_unread",
                  "is_inbox", "is_starred", "size", "has_attachment", "reasons")


class ToolError(Exception):
    pass


def _account(args: dict):
    try:
        return Store.load().require(args.get("account") or None)
    except LookupError as e:
        raise ToolError(str(e))


def _limit(args: dict, default: int = 25) -> int:
    try:
        return max(1, min(int(args.get("limit") or default), 200))
    except (TypeError, ValueError):
        return default


def _filter(args: dict) -> tuple[str, tuple]:
    try:
        return policies.compile_filter(args.get("filter") or {})
    except policies.FilterError as e:
        raise ToolError(str(e))


def call_tool(name: str, args: dict):
    if name == "list_accounts":
        store = Store.load()
        out = []
        for a in store.accounts:
            conn = db.connect(a)
            try:
                o = stats.overview(conn)
                last = db.get_state(conn, "last_sync")
            finally:
                conn.close()
            out.append({"id": a.id, "address": a.email, "label": a.display,
                        "provider": a.provider_info.name, "active": a.id == store.active,
                        "messages": o["total"] or 0, "last_sync": last})
        return out

    account = _account(args)
    conn = db.connect(account)
    try:
        if name == "mailbox_overview":
            return {"account": account.id, **stats.overview(conn),
                    "last_sync": db.get_state(conn, "last_sync")}
        if name == "top_senders":
            return stats.senders(conn, _limit(args), domain=args.get("domain") or None)
        if name == "top_domains":
            return stats.domains(conn, _limit(args))
        if name == "categories":
            return stats.categories(conn)
        if name == "needs_attention":
            rows = stats.attention(conn, _limit(args))
            return [{k: r[k] for k in MESSAGE_FIELDS} for r in rows]
        if name == "cleanup_suggestions":
            return [{k: v for k, v in s.items() if k != "where"}
                    for s in stats.suggestions(conn)]
        if name == "trend_alerts":
            return stats.alerts(conn, 20)
        if name == "digest":
            return stats.digest(conn, int(args.get("days") or 7))
        if name == "search_messages":
            where, params = _filter(args)
            rows = stats.messages(conn, where, params, limit=_limit(args))
            return {"total": stats.count(conn, where, params),
                    "rows": [{k: r[k] for k in MESSAGE_FIELDS} for r in rows]}
        if name == "preview_selection":
            from . import actions

            where, params = _filter(args)
            info = actions.preview(conn, where, params)
            info["samples"] = [{k: r[k] for k in MESSAGE_FIELDS}
                               for r in info["samples"]]
            info["note"] = ("Read-only preview. Acting on it happens in mclean or "
                            "mclean gui, where the user confirms.")
            return info
        if name == "list_rules":
            return {"rules": [dict(r) for r in conn.execute("SELECT * FROM rules")],
                    "policies": policies.list_all(conn)}
        if name == "unsubscribe_candidates":
            return unsubscribe.senders(conn, _limit(args))
    finally:
        conn.close()
    raise ToolError(f"unknown tool {name!r}")


def tool_list() -> list[dict]:
    return [{
        "name": n, "description": d,
        "inputSchema": {"type": "object", "properties": props,
                        "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "openWorldHint": False},
    } for n, d, props in TOOLS]


def handle(msg: dict) -> dict | None:
    """One JSON-RPC message in, at most one out (none for notifications)."""
    method = msg.get("method")
    mid = msg.get("id")
    if mid is None:
        return None  # a notification, e.g. notifications/initialized
    if method == "initialize":
        asked = (msg.get("params") or {}).get("protocolVersion")
        result = {
            "protocolVersion": asked if asked in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "mailcleaner", "version": __version__},
            "instructions": (
                "Read-only access to the user's local mailbox index (metadata "
                "only). Use filters to describe selections; nothing here can "
                "change mail."),
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": tool_list()}
    elif method == "tools/call":
        params = msg.get("params") or {}
        try:
            data = call_tool(params.get("name", ""), params.get("arguments") or {})
            result = {"content": [{"type": "text",
                                   "text": json.dumps(data, default=str)}],
                      "isError": False}
        except ToolError as e:
            result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None,
                     "error": {"code": -32700, "message": "parse error"}}
        else:
            try:
                reply = handle(msg)
            except Exception as e:  # keep the server up; report on stderr
                traceback.print_exc(file=sys.stderr)
                reply = {"jsonrpc": "2.0", "id": msg.get("id"),
                         "error": {"code": -32603, "message": str(e)}}
        if reply is not None:
            stdout.write(json.dumps(reply, default=str) + "\n")
            stdout.flush()
