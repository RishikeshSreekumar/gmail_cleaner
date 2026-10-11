"""Rules, unsubscribe, the LLM advisor, MCP, evaluation, scheduling, doctor,
the CLI surface and the new browser endpoints - all offline, against fakes."""

import io
import json
import os
import sqlite3
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)
from make_fixture import FakeBackend, build, sandbox  # noqa: E402

tmp = sandbox()

from typer.testing import CliRunner  # noqa: E402

from mailcleaner import (  # noqa: E402
    accounts as acct_mod, actions, classify, db, doctor, evaluate, llm, mcp_server,
    policies, schedule, stats, sync, unsubscribe, web,
)
from mailcleaner.accounts import Account, Store  # noqa: E402
from mailcleaner.cli import app  # noqa: E402

store = Store.load()
acct = store.add(Account.from_provider("gmail", "me@gmail.com"))
store.set_active(acct)
conn = db.connect(acct)
build(conn)
print("fixture:", stats.overview(conn)["total"], "messages")

# -- schema migration --------------------------------------------------------
old = os.path.join(tmp, "old.db")
c = sqlite3.connect(old)
v010 = db.SCHEMA.split("-- Compound rules")[0]          # the 0.1.0 tables...
v010 = "\n".join(l for l in v010.splitlines() if "unsub_" not in l)  # ...minus new columns
c.executescript(v010)
c.commit()
c.close()
c = db.connect(path=old)
cols = {r[1] for r in c.execute("PRAGMA table_info(emails)")}
assert {"unsub_url", "unsub_post", "unsub_mailto"} <= cols, cols
assert c.execute("SELECT name FROM sqlite_master WHERE name='policies'").fetchone()
c.close()
print("an older index gains the new columns and tables in place")

# -- classifier: locale packs and correspondents -----------------------------
de = {"subject": "Ihre Rechnung für Mai", "from_email": "x@shop.de", "from_domain": "shop.de"}
assert classify.classify(de).category == "finance"
assert classify.classify(de, locales=["en"]).category != "finance"
bounce = {"subject": "Delivery Status Notification", "from_email": "mailer-daemon@x.com",
          "from_domain": "x.com"}
assert classify.classify(bounce).category == "automated"
team = {"subject": "quick question", "from_email": "team@acme.io", "from_domain": "acme.io",
        "is_unread": 1, "is_inbox": 1}
v = classify.classify(team)
assert v.category == "automated" and not v.protected
v = classify.classify(team, correspondents={"team@acme.io"})
assert v.protected and v.category == "human" and v.attention == "potentially important", v
listy = dict(team, list_id="<list.acme.io>")
assert not classify.classify(listy, correspondents={"team@acme.io"}).protected, \
    "replying to a list once must not protect the whole list"
deadline = {"subject": "Your domain expires in 7 days", "from_email": "support@reg.com",
            "from_domain": "reg.com", "is_unread": 1}
assert classify.classify(deadline).protected
promo = {"subject": "Last day of the sale!", "from_email": "deals@shop.com",
         "from_domain": "shop.com", "is_unread": 1}
assert not classify.classify(promo).protected
print("locale packs, bounces, correspondents and unread deadlines classify as intended")

# correspondents come from mail the account owner sent
sent = {
    "msg_key": "sent-1", "uid": 1, "thread_key": "t", "folder": "[Gmail]/All Mail",
    "message_id": "<s1@gmail.com>", "received_at": 1_700_000_000, "from_name": "me",
    "from_email": "me@gmail.com", "from_domain": "gmail.com",
    "to_emails": json.dumps(["Friend@Startup.io"]), "subject": "hi", "size": 10,
    "labels": [], "is_unread": 0, "is_inbox": 0, "is_starred": 0, "is_important": 0,
    "list_id": "", "unsubscribe": 0, "unsub_url": "", "unsub_post": 0, "unsub_mailto": "",
    "has_attachment": 0, "age_days": 5,
}
sync.upsert(conn, [sync.enrich(sent, [])])
assert sync.correspondents(conn) == {"friend@startup.io"}
print("correspondents are read from Sent mail:", sync.correspondents(conn))

# -- unsubscribe header parsing ---------------------------------------------
url, one, mailto = sync.unsubscribe_targets(
    "<mailto:u@list.example.com?subject=unsub>, <https://list.example.com/u/abc>",
    "List-Unsubscribe=One-Click")
assert url == "https://list.example.com/u/abc" and one == 1 and mailto.startswith("mailto:")
url, one, _ = sync.unsubscribe_targets("<http://insecure.example.com/u>", "List-Unsubscribe=One-Click")
assert url == "" and one == 0, "plain http is never kept"
print("List-Unsubscribe parsing keeps https and mailto, drops http")

# -- filters ----------------------------------------------------------------
where, params = policies.compile_filter({"category": "newsletter", "older_than_days": 180,
                                         "unread": True})
n = stats.count(conn, where, params)
assert n > 0, "fixture has old unread newsletters"
for bad in ({}, {"nope": 1}, {"older_than_days": "soon"}, {"unread": "maybe"},
            {"sender": ["x"] * 500}):
    try:
        policies.compile_filter(bad)
        raise AssertionError(f"accepted {bad}")
    except policies.FilterError:
        pass
evil = "x' OR 1=1 --"
where, params = policies.compile_filter({"sender": evil})
assert stats.count(conn, where, params) == 0 and evil.lower() in params
where, params = policies.compile_filter({"subject_contains": "%"})
assert stats.count(conn, where, params) == stats.count(conn, "subject LIKE '%\\%%' ESCAPE '\\'")
where, params = policies.compile_filter({"domain": "google.com"})
assert stats.count(conn, where, params) == stats.count(
    conn, "from_domain='accounts.google.com'"), "domain matches subdomains"
print("filters validate, bind every value, escape LIKE and match subdomains")

# -- policies ---------------------------------------------------------------
try:
    policies.add(conn, "nope", {"category": "promotion"}, "trash")
    raise AssertionError("trash policy accepted")
except policies.FilterError:
    pass
pid = policies.add(conn, "Old newsletters", {"category": "newsletter",
                                             "older_than_days": 180}, "archive")
lbl = policies.add(conn, "", {"domain": "quora.com"}, "label", label="Later", auto=True)
rows = {p["id"]: p for p in policies.list_all(conn)}
assert rows[pid]["matches"] > 0 and not rows[pid]["auto"]
assert rows[lbl]["name"] and rows[lbl]["auto"]
fb = FakeBackend()
res = policies.apply(conn, fb, only_auto=True)
assert [r["id"] for r in res] == [lbl], "only auto policies run by default"
assert any(c[0] == "apply_label" for c in fb.calls)
assert stats.count(conn, "from_domain='quora.com' AND protected=0 AND labels NOT LIKE '%Later%'") == 0
res = policies.apply(conn, fb, only_auto=False, policy_id=pid)
assert res[0]["count"] == rows[pid]["matches"]
assert stats.count(conn, "category='newsletter' AND is_inbox=1 AND protected=0 AND "
                         "received_at < strftime('%s','now','-180 days')") == 0
assert actions.undo(conn, fb, res[0]["batch_id"]) == res[0]["count"]
print(f"policies: suggest by default, auto applied alone, {res[0]['count']} archived and undone")

# -- unsubscribe --------------------------------------------------------------
conn.execute("UPDATE emails SET unsub_url='https://lists.medium.com/u/TOKEN123', unsub_post=1 "
             "WHERE from_email='newsletter@medium.com'")
conn.execute("UPDATE emails SET unsub_url='https://quora.com/unsub?u=SECRET', unsub_post=0 "
             "WHERE from_email='digest@quora.com'")
conn.commit()
assert unsubscribe.options(conn, "newsletter@medium.com")["method"] == "one-click"
posted = []
r = unsubscribe.unsubscribe(conn, "newsletter@medium.com", post=lambda u: posted.append(u) or 200)
assert r["status"] == "ok" and posted == ["https://lists.medium.com/u/TOKEN123"]
r = unsubscribe.unsubscribe(conn, "digest@quora.com", post=lambda u: 1 / 0)
assert r["status"] == "manual" and r["open"].startswith("https://quora.com/")
log = unsubscribe.history(conn)
assert all("TOKEN" not in (x["target"] or "") and "SECRET" not in (x["target"] or "")
           for x in log), "only hosts are logged"
try:
    unsubscribe.unsubscribe(conn, "newsletter@medium.com", post=lambda u: 500)
    raise AssertionError("HTTP 500 counted as success")
except unsubscribe.UnsubscribeError:
    pass
for host in ("127.0.0.1", "10.0.0.8", "localhost"):
    try:
        unsubscribe.public_address(host)
        raise AssertionError(f"{host} allowed")
    except unsubscribe.UnsubscribeError:
        pass
for bad in ("http://example.com/u", "https://user:pw@example.com/u"):
    try:
        unsubscribe.one_click_post(bad, resolve=lambda h, p: "93.184.216.34")
        raise AssertionError(f"{bad} allowed")
    except unsubscribe.UnsubscribeError:
        pass


class FakeHTTP:
    def __init__(self):
        self.sent = None

    def request(self, method, path, body=None, headers=None):
        self.sent = (method, path, body, headers)

    def getresponse(self):
        class R:
            status = 200

            def read(self, n):
                return b""
        return R()

    def close(self):
        pass


fh = FakeHTTP()
assert unsubscribe.one_click_post("https://example.com/u?id=1",
                                  resolve=lambda h, p: "93.184.216.34", connection=fh) == 200
assert fh.sent[0] == "POST" and fh.sent[1] == "/u?id=1"
assert fh.sent[2] == b"List-Unsubscribe=One-Click" and "Cookie" not in fh.sent[3]
assert len(unsubscribe.senders(conn)) >= 2
print("unsubscribe: one-click POST, manual link, private hosts refused, hosts-only log")

# -- LLM advisor ----------------------------------------------------------------


class FakeLLM:
    name, model = "fake", "m1"

    def __init__(self, reply):
        self.reply = reply
        self.prompts = []

    def complete_json(self, system, user, schema):
        self.prompts.append(json.loads(user) if user.startswith("{") else user)
        return self.reply(self.prompts[-1]) if callable(self.reply) else self.reply


conn.execute("DELETE FROM rules")
conn.commit()
sync.reclassify_all(conn)
cands = llm.candidates(conn, 100)
names = {c["sender"] for c in cands}
assert "alerts@icicibank.com" in names
# A sender with no protected mail at all, which the model may suggest ignoring.
# (offers@swiggy.in would not do: its delivery notices are protected.)
QUIET = next(c["sender"] for c in cands if not (c["protected"] or c["sensitive"] or c["starred"]))


def reply(payload):
    seen = {s["sender"] for s in payload["senders"]}
    out = [
        {"sender": QUIET, "action": "ignore", "category": "",
         "reason": "never read", "confidence": 0.9},
        {"sender": "offers@swiggy.in", "action": "ignore", "category": "",
         "reason": "promotions", "confidence": 0.9},     # has protected orders: dropped
        {"sender": "alerts@icicibank.com", "action": "ignore", "category": "",
         "reason": "noise", "confidence": 0.9},          # protected: must be dropped
        {"sender": "alerts@icicibank.com", "action": "category", "category": "promotion",
         "reason": "x", "confidence": 0.9},              # demotes protected: dropped
        {"sender": "ghost@nowhere.com", "action": "protect", "category": "",
         "reason": "made up", "confidence": 0.9},        # not in the batch: dropped
        {"sender": "bookings@makemytrip.com", "action": "protect", "category": "",
         "reason": "travel bookings", "confidence": 0.8},
    ]
    return {"suggestions": [s for s in out if s["sender"] in seen or s["sender"] == "ghost@nowhere.com"]}


fake = FakeLLM(reply)
acct.llm_share_subjects = False
res = llm.suggest_rules(conn, acct, client=fake, limit=100)
assert all("recent_subjects" not in s for p in fake.prompts for s in p["senders"]), \
    "subjects withheld when the account says so"
pending = {(s["match_value"], s["action"]) for s in llm.suggestions(conn)}
assert (QUIET, "ignore") in pending
assert ("offers@swiggy.in", "ignore") not in pending
assert ("bookings@makemytrip.com", "protect") in pending
assert not any(v == "alerts@icicibank.com" for v, _ in pending), "never unprotects"
assert not any(v == "ghost@nowhere.com" for v, _ in pending), "no invented senders"
assert conn.execute("SELECT COUNT(*) c FROM rules").fetchone()["c"] == 0, "nothing applied yet"
sid = [s["id"] for s in llm.suggestions(conn) if s["match_value"] == QUIET][0]
llm.decide(conn, sid, True)
assert conn.execute("SELECT COUNT(*) c FROM rules WHERE match_value=? AND action='ignore'",
                    (QUIET,)).fetchone()["c"] == 1
try:
    llm.decide(conn, sid, True)
    raise AssertionError("decided twice")
except llm.LLMError:
    pass
again = llm.candidates(conn, 100)
assert QUIET not in {c["sender"] for c in again}, "not asked about twice"
print(f"llm suggestions: {res['stored']} kept, {res['dropped']} dropped by safety checks")

q = llm.parse_query("old unread newsletters", acct, client=FakeLLM(
    {"filter": {"category": ["newsletter"], "older_than_days": 180, "unread": True,
                "sender": []}, "explanation": "unread newsletters over 6 months"}))
assert q["filter"] == {"category": ["newsletter"], "older_than_days": 180, "unread": True}
try:
    llm.parse_query("x", acct, client=FakeLLM({"filter": {"drop_table": 1}, "explanation": ""}))
    raise AssertionError("unknown field accepted")
except llm.LLMError:
    pass
try:
    llm.client_for(acct)
    raise AssertionError("advisor should be off by default")
except llm.LLMError:
    pass
print("plain-English search yields only validated filters; the advisor is off by default")

# -- digest and alerts -----------------------------------------------------------
d = stats.digest(conn, 7)
assert d["new"] > 0 and isinstance(d["alerts"], list)
assert "new in 7 days" in stats.digest_text(d, "x")
print("digest:", stats.digest_text(d).splitlines()[0])

# -- MCP ---------------------------------------------------------------------------
init = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                          "params": {"protocolVersion": "2025-06-18"}})
assert init["result"]["protocolVersion"] == "2025-06-18"
assert mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
tools = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
names = {t["name"] for t in tools}
assert not names & {"archive", "trash", "label", "unsubscribe", "add_rule"}, "read-only"
assert all(t["annotations"]["readOnlyHint"] for t in tools)


def mcp_call(name, args):
    r = mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                           "params": {"name": name, "arguments": args}})["result"]
    return r["isError"], (json.loads(r["content"][0]["text"]) if not r["isError"]
                          else r["content"][0]["text"])


err, out = mcp_call("top_senders", {"limit": 3})
assert not err and len(out) == 3
err, out = mcp_call("search_messages", {"filter": {"category": ["finance"]}, "limit": 5})
assert not err and out["total"] > 0 and "unsub_url" not in out["rows"][0]
err, out = mcp_call("search_messages", {"filter": {"evil": "1"}})
assert err and "unknown filter field" in out
err, out = mcp_call("preview_selection", {"filter": {"domain": ["linkedin.com"]}})
assert not err and out["total"] == out["eligible"] + out["protected"]
assert mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"})["error"]["code"] == -32601
buf = io.StringIO()
mcp_server.serve(io.StringIO('{"jsonrpc":"2.0","id":9,"method":"ping"}\nnot json\n'), buf)
lines = [json.loads(x) for x in buf.getvalue().splitlines()]
assert lines[0]["result"] == {} and lines[1]["error"]["code"] == -32700
print(f"mcp: {len(tools)} read-only tools over stdio JSON-RPC")

# -- evaluation ------------------------------------------------------------------
r = evaluate.score_file(os.path.join(HERE, "classifier_cases.csv"))
assert r["missed_protection"] == 0, r["missed_examples"]
assert r["accuracy"] >= 0.9, r
out = os.path.join(tmp, "sample.csv")
assert evaluate.export_sample(conn, out, 20, seed=1) == 20
print(f"eval: {r['accuracy']:.1%} on {r['labelled']} labelled cases, 0 missed protections")

# -- scheduling -------------------------------------------------------------------
args = schedule.job_args(apply_rules=True, notify=True)
assert args[-2:] == ["--apply-rules", "--notify"] and "background-run" in args
plist = schedule.launchd_plist(args, 6)
assert "<integer>21600</integer>" in plist and "background-run" in plist
units = schedule.systemd_units(args, 4)
assert "OnUnitActiveSec=4h" in units["mailcleaner-sync.timer"]
assert "background-run" in schedule.cron_line(args, 3)
ran = []
os.environ["XDG_CONFIG_HOME"] = os.path.join(tmp, "xdg")
schedule.shutil.which = lambda name: "/bin/systemctl" if name == "systemctl" else None
msg = schedule.install(2, system="Linux", run=lambda *a, **k: ran.append(a[0]))
assert "systemd" in msg and any("enable" in c for c in ran)
assert schedule.status(system="Linux")["installed"] is True
schedule.remove(system="Linux", run=lambda *a, **k: ran.append(a[0]))
assert schedule.status(system="Linux")["installed"] is False
try:
    schedule.install(0, system="Linux", run=lambda *a, **k: None)
    raise AssertionError("bad interval accepted")
except ValueError:
    pass
acct.actions_enabled = True
store.update(acct)
print("schedule: launchd / systemd / cron definitions, install and remove")


class SyncFake(FakeBackend):
    def sync_folders(self):
        return ["[Gmail]/All Mail"]

    def uidvalidity(self, folder):
        return 1

    def search_since(self, folder, days, min_uid=1):
        return []

    def fetch_metadata(self, folder, uids):
        return []

    def is_inbox(self, raw):
        return False


notes = []
results = schedule.background_run(apply_rules=True, notify=True,
                                  backend=lambda a: SyncFake(),
                                  notifier=lambda t, b: notes.append(b))
assert results[0]["ok"], results
assert "policies" in results[0] and (acct.dir / "digest.json").exists()
assert notes and acct.id in notes[0]
print("background run: synced, applied auto rules, wrote digest, notified")

# -- doctor -----------------------------------------------------------------------
assert "apppasswords" in doctor.login_fix("gmail", "[ALERT] Application-specific password required")
assert "IMAP" in doctor.login_fix("gmail", "IMAP access is disabled for your domain")
ghost = Account.from_provider("imap", "x@nowhere.invalid", host="mail.nowhere.invalid")
checks = doctor.diagnose(ghost)
assert checks[0].ok is False and checks[1].name == "Server name resolves" and checks[1].ok is False
assert all(c.ok is None for c in checks[2:])
print("doctor: maps login errors to setup steps; stops at the first broken step")

# -- CLI ----------------------------------------------------------------------------
cli = CliRunner()
r = cli.invoke(app, ["rules", "add", "Big promos", "--category", "promotion",
                     "--older-than", "30", "--action", "archive"])
assert r.exit_code == 0, r.output
r = cli.invoke(app, ["rules", "list", "--json"])
data = json.loads(r.output)
assert any(p["name"] == "Big promos" for p in data["policies"])
r = cli.invoke(app, ["rules", "add", "x", "--category", "promotion", "--action", "trash"])
assert r.exit_code == 1 and "trash" in r.output
r = cli.invoke(app, ["digest", "--json"])
assert acct.id in json.loads(r.output)
r = cli.invoke(app, ["top", "--by", "sender", "-n", "3", "--json"])
assert len(json.loads(r.output)) == 3
r = cli.invoke(app, ["export", "messages", "--format", "json", "--where",
                     '{"category": ["finance"]}', "-n", "5"])
assert r.exit_code == 0 and len(json.loads(r.output)) == 5
r = cli.invoke(app, ["export", "senders", "-n", "2"])
assert r.output.splitlines()[0].startswith("key,")
r = cli.invoke(app, ["eval", "score", os.path.join(HERE, "classifier_cases.csv"), "--json"])
assert json.loads(r.output)["missed_protection"] == 0
r = cli.invoke(app, ["locale", "--set", "en,in"])
assert r.exit_code == 0 and Store.load().current.locales == ["en", "in"]
r = cli.invoke(app, ["locale", "--set", "klingon"])
assert r.exit_code == 1
r = cli.invoke(app, ["llm", "suggest"])
assert r.exit_code == 1 and "off" in r.output
r = cli.invoke(app, ["status", "--json"])
assert json.loads(r.output)["locales"] == ["en", "in"]
print("cli: rules, digest, top, export, eval, locale, llm and status behave")

# -- browser endpoints ---------------------------------------------------------------
server = web.Server(("127.0.0.1", 0), token="tok", verbose=False)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_address[1]}"


def call(path, body=None):
    req = urllib.request.Request(
        BASE + path, data=None if body is None else json.dumps(body).encode(),
        headers={"X-Token": "tok", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


code, d = call("/api/overview")
assert code == 200 and "alerts" in d and d["llm_provider"] == "off"
code, d = call("/api/rules")
assert code == 200 and d["policies"] and "older_than_days" in d["fields"]
code, d = call("/api/rules/add", {"name": "w", "conditions": {"category": ["social"]},
                                  "action": "archive"})
assert code == 200
new_id = d["id"]
code, d = call("/api/rules/add", {"name": "w", "conditions": {"x": 1}, "action": "archive"})
assert code == 400
code, d = call("/api/rules/add", {"name": "w", "conditions": {"category": ["social"]},
                                  "action": "trash"})
assert code == 400
code, d = call("/api/preview", {"target": {"kind": "policy", "value": new_id}})
assert code == 200 and d["total"] > 0
code, d = call("/api/preview", {"target": {"kind": "filter",
                                           "value": {"category": ["finance"]}}})
assert code == 200 and d["eligible"] == 0, "finance is protected"
code, d = call("/api/preview", {"target": {"kind": "filter", "value": {"sql": "1=1"}}})
assert code == 400
code, d = call("/api/sender?value=newsletter@medium.com")
assert d["unsubscribe"] == {"method": "one-click", "host": "lists.medium.com"}
assert "TOKEN" not in json.dumps(d["unsubscribe"])
code, d = call("/api/rules/simple/add", {"match_type": "sender",
                                         "value": "digest@quora.com", "action": "protect"})
assert code == 200
code, d = call("/api/rules/simple/add", {"match_type": "sql", "value": "x", "action": "protect"})
assert code == 400
code, d = call("/api/llm/suggest", {})
assert code == 400 and "Settings" in d["error"]
code, d = call("/api/ask", {"text": "old stuff"})
assert code == 400
acct = Store.load().current
acct.actions_enabled = False
Store.load().update(acct)
code, d = call("/api/unsubscribe", {"sender": "newsletter@medium.com"})
assert code == 400 and "Settings" in d["error"]
code, d = call("/api/unsubscribe")
assert code == 200 and d["rows"]
code, d = call("/api/llm")
assert code == 200 and d["provider"] == "off"
print("web: rules, filter and policy targets, sender unsubscribe info, guards")

# -- everything the CLI did, from the page -------------------------------------
code, d = call("/api/settings")
assert code == 200 and d["account"]["email"] == "me@gmail.com"
assert "en" in d["locales"] and d["llm"]["has_key"] is False and d["mcp"]["command"][-1] == "mcp"

code, d = call("/api/settings/actions", {"enabled": True})
assert code == 200 and Store.load().current.actions_enabled is True
code, d = call("/api/settings/actions", {"enabled": False})
assert Store.load().current.actions_enabled is False

code, d = call("/api/settings/account", {"sync_days": 3})
assert code == 400
code, d = call("/api/settings/account", {"sync_days": 730, "label": "Personal",
                                         "locales": ["en", "de"]})
assert code == 200 and d["reclassified"] > 0
cur = Store.load().current
assert cur.sync_days == 730 and cur.label == "Personal" and cur.locales == ["en", "de"]
code, d = call("/api/settings/account", {"locales": ["xx"]})
assert code == 400

code, d = call("/api/settings/llm", {"provider": "anthropic", "api_key": "sk-test"})
assert code == 400 and "confirm" in d["error"], "cloud needs explicit consent"
code, d = call("/api/settings/llm", {"provider": "anthropic", "api_key": "sk-test",
                                     "cloud_consent": True, "share_subjects": False})
assert code == 200 and d["model"] == "claude-opus-5-5"
code, d = call("/api/settings")
assert d["llm"]["has_key"] is True and "sk-test" not in json.dumps(d), "key never echoed"
assert acct_mod.get_llm_key(Store.load().current) == "sk-test"
code, d = call("/api/settings/llm", {"provider": "ollama", "host": "not a url"})
assert code == 400
code, d = call("/api/settings/llm", {"provider": "off"})
assert code == 200 and acct_mod.get_llm_key(Store.load().current) is None
code, d = call("/api/llm/models?host=http://127.0.0.1:9")
assert code == 200 and d["models"] == []


class PwBackend(FakeBackend):
    """Accepts any password except 'wrong'."""

    def __init__(self, account):
        super().__init__()
        self.account = account

    def __enter__(self):
        if (acct_mod._read_secret(self.account, "") or "") == "wrong":
            raise acct_mod.providers.MailError("server rejected the login")
        return self


real_backend = acct_mod.backend
acct_mod.backend = PwBackend
cur = Store.load().current
acct_mod.set_password(cur, "good-one")
code, d = call("/api/settings/password", {"password": "wrong"})
assert code in (400, 502) and acct_mod._read_secret(cur, "") == "good-one", "old password kept"
code, d = call("/api/settings/password", {"password": "new one"})
assert code == 200 and acct_mod._read_secret(cur, "") == "newone"

code, d = call("/api/rules/apply", {})
assert code == 400 and "Settings" in d["error"]
call("/api/settings/actions", {"enabled": True})
code, d = call("/api/rules/apply", {})
assert code == 200 and isinstance(d["results"], list)
call("/api/settings/actions", {"enabled": False})

installed = []
schedule.install = lambda every, apply_rules, notify: installed.append((every, apply_rules)) or "ok"
schedule.remove = lambda: "removed"
code, d = call("/api/settings/schedule", {"every": 3, "apply_rules": True})
assert code == 200 and installed == [(3, True)]
code, d = call("/api/settings/schedule", {"remove": True})
assert d["message"] == "removed"

code, d = call("/api/eval/sample?n=10")
assert code == 200 and len(d["rows"]) == 10
rows = [{"msg_key": r["msg_key"], "true_category": r["category"],
         "true_protected": bool(r["protected"])} for r in d["rows"]]
rows[0]["true_protected"] = True
code, d = call("/api/eval/score", {"rows": rows})
assert code == 200 and d["labelled"] == 10 and d["accuracy"] == 1.0

req = urllib.request.Request(BASE + "/api/export?what=senders", headers={"X-Token": "tok"})
with urllib.request.urlopen(req) as resp:
    assert "attachment" in resp.headers["Content-Disposition"]
    assert resp.read().decode().splitlines()[0].startswith("key,")
req = urllib.request.Request(
    BASE + "/api/export?" + urllib.parse.urlencode({"what": "messages", "kind": "filter",
                                                   "value": json.dumps({"category": ["finance"]})}),
    headers={"X-Token": "tok"})
with urllib.request.urlopen(req) as resp:
    lines = resp.read().decode().splitlines()
    assert len(lines) > 1 and all(",finance," in l for l in lines[1:3])
try:
    urllib.request.urlopen(BASE + "/api/export?what=senders")
    raise AssertionError("export without a token")
except urllib.error.HTTPError as e:
    assert e.code == 403

code, d = call("/api/settings/remove", {"confirm": "nope"})
assert code == 400 and Store.load().current is not None
code, d = call("/api/settings/remove", {"confirm": "me@gmail.com"})
assert code == 200 and Store.load().accounts == [] and not acct.dir.exists()
code, d = call("/api/overview")
assert d["needs_setup"] is True
acct_mod.backend = real_backend
server.shutdown()

taken = web.Server(("127.0.0.1", 0), token="x", verbose=False)
second = web.bind("127.0.0.1", taken.server_address[1], "y", False)
assert second.server_address[1] != taken.server_address[1], "falls through to a free port"
taken.server_close()
second.server_close()
print("web settings: actions, sync window, packs, password swap, assistant, schedule, "
      "apply, eval, export, remove")

print("\nall feature tests passed")
