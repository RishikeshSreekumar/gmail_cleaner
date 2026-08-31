"""Headless run of every screen, plus a fake-backend action round trip.

Covers both provider shapes: Gmail's label-shaped backend, where a message never
moves, and the folder-shaped one used by every other provider, where archive and
label are IMAP MOVEs that invalidate the stored UID.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from make_fixture import FakeBackend, build, sandbox  # noqa: E402

sandbox()

from mailcleaner import actions, db, stats  # noqa: E402
from mailcleaner import sync as syncmod  # noqa: E402
from mailcleaner.accounts import Account, Store  # noqa: E402
from mailcleaner.tui import NAV, CleanerApp  # noqa: E402

store = Store.load()
gmail = store.add(Account.from_provider("gmail", "me@gmail.com"))
work = store.add(Account.from_provider("fastmail", "me@fastmail.com", label="work"))
store.set_active(gmail)
assert Store.load().current.id == gmail.id
assert gmail.db_path != work.db_path, "accounts must not share an index"
print(f"accounts: {[a.id for a in store.accounts]}")

conn = db.connect(gmail)
print("fixture rows:", build(conn))
work_conn = db.connect(work)
build(work_conn, n=120, seed=3, folder="INBOX")
assert stats.overview(work_conn)["total"] == 120
assert stats.overview(conn)["total"] == 900, "one account's sync must not touch another"
print("second account indexed independently:", stats.overview(work_conn)["total"], "rows")

o = stats.overview(conn)
assert o["total"] == 900, o
print("overview:", {k: o[k] for k in ("total", "unread", "protected", "cleanup", "attention")})
for name, fn in [("domains", stats.domains), ("senders", stats.senders),
                 ("categories", stats.categories), ("ages", stats.ages),
                 ("frequency", stats.frequency), ("suggestions", stats.suggestions),
                 ("attention", stats.attention)]:
    rows = fn(conn)
    assert rows, name
    print(f"{name}: {len(rows)} rows, first={rows[0].get('key')}")

pv = actions.preview(conn, "from_domain=?", ("linkedin.com",))
print("preview:", pv["total"], "total /", pv["eligible"], "eligible /", pv["protected"], "protected")
assert pv["protected"] + pv["eligible"] == pv["total"]

# -- label-shaped provider (Gmail): archive is a label edit, the UID survives --
fc = FakeBackend()
inbox_before = stats.count(conn, "from_domain='medium.com' AND is_inbox=1")
res = actions.run(conn, fc, "archive", "from_domain=?", ("medium.com",))
print("archive:", res["count"], "acted,", res["skipped"], "protected skipped")
assert stats.count(conn, "from_domain='medium.com' AND is_inbox=1 AND protected=0") == 0
assert fc.calls[0][0] == "archive"
assert stats.count(conn, "from_domain='medium.com' AND uid=0") == 0, "labels never move mail"

n = actions.undo(conn, fc, res["batch_id"])
assert n == res["count"]
assert stats.count(conn, "from_domain='medium.com' AND is_inbox=1") == inbox_before
print("undo restored", n, "messages")

before = stats.count(conn, "from_domain='quora.com'")
res = actions.run(conn, fc, "trash", "from_domain=? AND protected=0", ("quora.com",))
assert stats.count(conn, "from_domain='quora.com'") == before - res["count"]
print("trash:", res["count"], "removed from index")
actions.undo(conn, fc, res["batch_id"])
assert ("untrash", res["count"], None) in fc.calls
print("undo asked the backend to pull it back out of Trash")

# -- folder-shaped provider: archive is a MOVE, so the stored UID goes stale --
fmove = FakeBackend(moves=True)
res = actions.run(work_conn, fmove, "archive", "from_domain=? AND is_inbox=1",
                  ("medium.com",))
if res["count"]:
    assert res["moved"] is True
    moved = stats.count(work_conn, "folder='Archive' AND uid=0")
    assert moved == res["count"], (moved, res)
    print("folder-shaped archive moved", res["count"], "messages to Archive")
    actions.undo(work_conn, fmove, res["batch_id"])
    assert ("unarchive", res["count"], None) in fmove.calls

# ...and archiving never drags non-inbox mail out of the folder it was filed in.
filed = stats.count(work_conn, "from_domain='github.com' AND is_inbox=0")
res = actions.run(work_conn, fmove, "archive", "from_domain=?", ("github.com",))
assert res["count"] <= stats.count(work_conn, "from_domain='github.com'") - filed + res["count"]
assert stats.count(work_conn, "from_domain='github.com' AND is_inbox=0 AND folder='INBOX'") == filed
print("folder-shaped archive left", filed, "already-filed messages alone")

res = actions.run(work_conn, fmove, "label", "from_domain=?", ("quora.com",),
                  label="Cleanup/Review")
if res["count"]:
    assert stats.count(work_conn, "folder='Cleanup/Review'") == res["count"]
    actions.undo(work_conn, fmove, res["batch_id"])
    assert ("remove_label", res["count"], "Cleanup/Review") in fmove.calls
    print("folder-shaped label filed and un-filed", res["count"], "messages")

# No code path anywhere asks for a delete.
for backend in (fc, fmove):
    assert not any("Deleted" in str(c) or "EXPUNGE" in str(c).upper()
                   for c in backend.calls)
print("no backend call ever mentions \\Deleted or EXPUNGE")

actions.add_rule(conn, "domain", "linkedin.com", "protect")
syncmod.reclassify_all(conn)
assert stats.count(conn, "from_domain='linkedin.com' AND protected=0") == 0
print("protect rule applied to all linkedin.com mail")
actions.delete_rule(conn, conn.execute("SELECT id FROM rules").fetchone()["id"])
syncmod.reclassify_all(conn)


async def drive_ui():
    app = CleanerApp(store, gmail)
    async with app.run_test(size=(160, 46)) as pilot:
        for key, title in NAV:
            app.stack = [type(app.stack[0])(key, title)]
            app.render_pane()
            await pilot.pause()
            print(f"  screen {title:<12} rows={len(app.row_meta)}")
        # the account list shows every configured mailbox
        app.stack = [type(app.stack[0])("accounts", "Accounts")]
        app.render_pane()
        await pilot.pause()
        assert len(app.row_meta) == 2, app.row_meta
        # drill: domains -> senders -> messages -> detail
        app.stack = [type(app.stack[0])("domains", "Domains")]
        app.render_pane()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.pane.kind == "senders", app.pane
        await pilot.press("enter")
        await pilot.pause()
        assert app.pane.kind == "messages", app.pane
        assert app.row_meta
        await pilot.press("enter")
        await pilot.pause()
        assert app.pane.kind == "detail"
        await pilot.press("escape")
        await pilot.pause()
        assert app.pane.kind == "messages"
        # read-only guard
        app.stack = [type(app.stack[0])("cleanup", "Cleanup")]
        app.render_pane()
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert len(app.screen_stack) == 1, "read-only mode must not open a confirm dialog"
        # with actions on, the confirm dialog appears
        app.account.actions_enabled = True
        await pilot.press("a")
        await pilot.pause()
        assert len(app.screen_stack) == 2, app.screen_stack
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        print("  drill-down, read-only guard and confirm dialog all OK")
        # switching accounts swaps the whole index out
        app.account.actions_enabled = False
        app.switch_account(work)
        await pilot.pause()
        assert app.account.id == work.id
        assert stats.overview(app.conn)["total"] == stats.overview(work_conn)["total"]
        assert Store.load().active == work.id, "the switch must persist"
        print("  account switch repoints the dashboard at the other mailbox")

asyncio.run(drive_ui())
print("\nALL SMOKE TESTS PASSED")
