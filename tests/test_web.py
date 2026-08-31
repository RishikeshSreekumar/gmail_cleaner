"""Exercise the web GUI's HTTP surface against a synthetic index."""

import json
import os
import sys
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from make_fixture import build, sandbox  # noqa: E402

sandbox()

from mailcleaner import accounts as acct_mod  # noqa: E402
from mailcleaner import db, web  # noqa: E402
from mailcleaner.accounts import Store  # noqa: E402


class OkBackend:
    """Stands in for a real IMAP connection while adding accounts."""

    def __init__(self, account):
        self.account = account

    def __enter__(self):
        if "bad" in self.account.email:
            raise acct_mod.providers.MailError("server rejected the login")
        return self

    def __exit__(self, *a):
        pass

    def sync_folders(self):
        return ["INBOX", "Archive"]


acct_mod.backend = OkBackend   # web.py resolves this at call time

server = web.Server(("127.0.0.1", 0), token="tok", verbose=False)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{port}"


def call(path, body=None, token="tok", host=None):
    req = urllib.request.Request(
        BASE + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"X-Token": token, "Content-Type": "application/json"},
    )
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


status, page = urllib.request.urlopen(BASE + "/").status, None
assert status == 200
print("page served OK")

# -- connecting mailboxes from the page ------------------------------------
code, d = call("/api/overview")
assert code == 200 and d["needs_setup"] is True and d["accounts"] == [], d
print("with nothing configured, overview asks the page to onboard")

code, d = call("/api/providers")
keys = {r["key"]: r for r in d["rows"]}
assert keys["gmail"]["uses_oauth"] is False and keys["outlook"]["uses_oauth"] is True
assert keys["imap"]["ask_host"] is True and keys["gmail"]["ask_host"] is False
assert "[b]" not in keys["gmail"]["help"] and "apppasswords" in keys["gmail"]["help"]
print("providers endpoint lists", len(keys), "services with plain-text setup steps")

for bad, why in (
    ({"provider": "nope", "email": "a@b.com", "password": "x"}, "unknown provider"),
    ({"provider": "gmail", "email": "notanemail", "password": "x"}, "email address"),
    ({"provider": "gmail", "email": "a@b.com"}, "app password"),
    ({"provider": "outlook", "email": "a@b.com", "password": "x"}, "browser"),
    ({"provider": "imap", "email": "a@b.com", "password": "x", "host": "h",
      "port": "99999"}, "port"),
    ({"provider": "imap", "email": "a@b.com", "password": "x", "host": "h",
      "security": "plaintext"}, "security"),
):
    code, data = call("/api/account/add", bad)
    assert code == 400 and why in data["error"], (bad, code, data)
print("the add form validates provider, address, password, port and TLS mode")

code, data = call("/api/account/add", {"provider": "gmail", "email": "bad@gmail.com",
                                       "password": "secret"})
assert code == 502 and "rejected" in data["error"], data
assert Store.load().accounts == [], "a mailbox that will not open is never kept"
print("a failed connection adds nothing and leaves no credential behind")

code, d = call("/api/account/add", {"provider": "gmail", "email": "me@gmail.com",
                                    "password": "app pass word here"})
assert code == 200 and d["folders"] == ["INBOX", "Archive"], d
gmail = Store.load().require(d["id"])
assert acct_mod.get_secret(gmail).password == "apppasswordhere", "spaces are stripped"
code, d2 = call("/api/account/add", {"provider": "fastmail", "email": "me@fastmail.com",
                                     "label": "work", "password": "pw"})
other = Store.load().require(d2["id"])
assert Store.load().active == other.id, "a newly added mailbox becomes active"
print("added two mailboxes from the page:", gmail.id, "/", other.id)

code, data = call("/api/account/add", {"provider": "gmail", "email": "me@gmail.com",
                                       "password": "pw"})
assert code == 400 and "already connected" in data["error"], data

code, data = call("/api/account/oauth/start", {"provider": "gmail",
                                               "email": "second@gmail.com"})
assert code == 400 and "app password" in data["error"], data
print("duplicates and mismatched sign-in styles are rejected")

# No endpoint ever hands a stored credential back to the browser.
for path in ("/api/accounts", "/api/overview"):
    code, d = call(path)
    assert "apppasswordhere" not in json.dumps(d), path
print("no endpoint echoes a stored credential")

build(db.connect(gmail))
build(db.connect(other), n=140, seed=11, folder="INBOX")
call("/api/account/use", {"id": gmail.id})

for path in ("/api/overview", "/api/senders?limit=5", "/api/domains?limit=5",
             "/api/categories", "/api/ages", "/api/frequency",
             "/api/attention?limit=5", "/api/suggestions", "/api/history",
             "/api/sender?value=digest@quora.com"):
    code, data = call(path)
    assert code == 200 and "error" not in data, (path, data)
    print(f"  GET {path:<28} ok")

code, data = call("/api/overview", token="wrong")
assert code == 403, (code, data)
code, data = call("/api/overview", host="evil.example.com")
assert code == 403, (code, data)
print("token and host guards reject bad requests")

code, info = call("/api/preview", {"target": {"kind": "sender",
                                              "value": "digest@quora.com"}})
assert code == 200 and info["eligible"] + info["protected"] == info["total"], info
print("preview:", info["total"], "total /", info["eligible"], "eligible")

for kind, value in (("suggestion", 1), ("age", 4), ("category", "promotion"),
                    ("domain", "quora.com"), ("attention", None)):
    code, info = call("/api/preview", {"target": {"kind": kind, "value": value}})
    assert code == 200, (kind, info)
print("every target kind builds a valid selection")

# The browser cannot smuggle SQL: only known target kinds are accepted.
for bad in ({"kind": "raw", "value": "1=1"}, {"kind": "suggestion", "value": 999},
            {"kind": "age", "value": "x"}, {}):
    code, data = call("/api/preview", {"target": bad})
    assert code == 400 and "error" in data, (bad, code, data)
print("unknown targets are rejected")

code, d = call("/api/sender?value=digest@quora.com")
assert d["row"]["key"] == "digest@quora.com" and "categories" in d and "samples" in d, d
print("sender detail returns a row, a category breakdown and examples")

code, d = call("/api/sender?value=nobody@nowhere.example")
assert code == 400, (code, d)
print("unknown sender is rejected")

# Multi-select: the browser names senders, the server builds the predicate.
code, info = call("/api/preview", {"target": {"kind": "senders",
                                              "value": ["digest@quora.com"]}})
assert code == 200 and info["title"] == "1 sender", info
one_total = info["total"]
code, info = call("/api/preview", {"target": {"kind": "senders", "value": []}})
assert code == 400, info
code, info = call("/api/preview", {"target": {"kind": "senders",
                                              "value": ["x@y.com"] * 201}})
assert code == 400 and "too many" in info["error"], info
print("multi-sender selections are bounded and never empty:", one_total, "matched")

# Read-only by default: no action can run.
code, data = call("/api/action", {"action": "trash",
                                  "target": {"kind": "sender", "value": "x@y.com"}})
assert code == 400 and "disabled" in data["error"], data
print("read-only mode blocks actions")

store = Store.load()
active = store.current
active.actions_enabled = True
store.update(active)
code, data = call("/api/action", {"action": "delete_forever",
                                  "target": {"kind": "sender", "value": "x@y.com"}})
assert code == 400 and "unknown action" in data["error"], data
print("no permanent-delete action exists")

# Every account is listed, and switching changes which index answers queries.
code, d = call("/api/overview")
assert {a["id"] for a in d["accounts"]} == {gmail.id, other.id}, d["accounts"]
assert d["account_id"] == gmail.id and d["stats"]["total"] == 900, d
code, d = call("/api/account/use", {"id": other.id})
assert code == 200 and d["active"] == other.id, d
code, d = call("/api/overview")
assert d["account_id"] == other.id and d["stats"]["total"] == 140, d
assert d["actions_enabled"] is False, "enabling actions is per account"
print("account switch: overview now reports the other mailbox,", d["stats"]["total"], "rows")

code, d = call("/api/account/use", {"id": "no-such-account"})
assert code == 400, (code, d)
call("/api/account/use", {"id": gmail.id})
print("unknown account ids are rejected")

server.shutdown()
print("\nWEB SMOKE TESTS PASSED")
