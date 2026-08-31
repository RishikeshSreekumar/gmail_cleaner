"""Exercise the web GUI's HTTP surface against a synthetic index."""

import json
import os
import sys
import tempfile
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
tmp = tempfile.mkdtemp()
os.environ["GMAIL_CLEANER_HOME"] = tmp

from gmail_cleaner import db, web  # noqa: E402
from gmail_cleaner.config import Config  # noqa: E402
from make_fixture import build  # noqa: E402

conn = db.connect()
build(conn)
Config(email="me@example.com").save()

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

cfg = Config.load()
cfg.actions_enabled = True
cfg.save()
code, data = call("/api/action", {"action": "delete_forever",
                                  "target": {"kind": "sender", "value": "x@y.com"}})
assert code == 400 and "unknown action" in data["error"], data
print("no permanent-delete action exists")

server.shutdown()
print("\nWEB SMOKE TESTS PASSED")
