"""Headless run of every screen, plus a fake-IMAP action round trip."""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
tmp = tempfile.mkdtemp()
os.environ["GMAIL_CLEANER_HOME"] = tmp

from gmail_cleaner import actions, db, stats  # noqa: E402
from gmail_cleaner.config import Config  # noqa: E402
from gmail_cleaner.tui import NAV, CleanerApp  # noqa: E402
from make_fixture import build  # noqa: E402

conn = db.connect()
print("fixture rows:", build(conn))

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


class FakeClient:
    """Records IMAP calls instead of making them."""
    trash = '"[Gmail]/Trash"'
    def __init__(self): self.calls = []
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def remove_labels(self, u, l): self.calls.append(("-labels", len(u), l))
    def add_labels(self, u, l): self.calls.append(("+labels", len(u), l))
    def add_flags(self, u, f): self.calls.append(("+flags", len(u), f))
    def remove_flags(self, u, f): self.calls.append(("-flags", len(u), f))
    def create_label(self, n): self.calls.append(("create", n))
    def uids_for_msgids(self, ids, folder): return {i: 1 for i in ids}
    def _select(self, *a, **k): pass
    def _store(self, *a): self.calls.append(("store", a))


fc = FakeClient()
inbox_before = stats.count(conn, "from_domain='medium.com' AND is_inbox=1")
res = actions.run(conn, fc, "archive", "from_domain=?", ("medium.com",))
print("archive:", res["count"], "acted,", res["skipped"], "protected skipped")
assert stats.count(conn, "from_domain='medium.com' AND is_inbox=1 AND protected=0") == 0
assert fc.calls[0][0] == "-labels" and fc.calls[0][2] == ["\\Inbox"]

n = actions.undo(conn, fc, res["batch_id"])
assert n == res["count"]
assert stats.count(conn, "from_domain='medium.com' AND is_inbox=1") == inbox_before
print("undo restored", n, "messages")

before = stats.count(conn, "from_domain='quora.com'")
res = actions.run(conn, fc, "trash", "from_domain=? AND protected=0", ("quora.com",))
assert stats.count(conn, "from_domain='quora.com'") == before - res["count"]
print("trash:", res["count"], "removed from index")
assert not any("Deleted" in str(c) or "EXPUNGE" in str(c).upper() for c in fc.calls)

actions.add_rule(conn, "domain", "linkedin.com", "protect")
from gmail_cleaner import sync as syncmod
syncmod.reclassify_all(conn)
assert stats.count(conn, "from_domain='linkedin.com' AND protected=0") == 0
print("protect rule applied to all linkedin.com mail")
actions.delete_rule(conn, conn.execute("SELECT id FROM rules").fetchone()["id"])
syncmod.reclassify_all(conn)


async def drive_ui():
    cfg = Config(email="me@example.com", actions_enabled=False)
    app = CleanerApp(db.connect(), cfg)
    async with app.run_test(size=(160, 46)) as pilot:
        for key, title in NAV:
            app.stack = [type(app.stack[0])(key, title)]
            app.render_pane()
            await pilot.pause()
            print(f"  screen {title:<12} rows={len(app.row_meta)}")
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
        app.config.actions_enabled = True
        await pilot.press("a")
        await pilot.pause()
        assert len(app.screen_stack) == 2, app.screen_stack
        await pilot.press("escape")
        await pilot.pause()
        assert len(app.screen_stack) == 1
        print("  drill-down, read-only guard and confirm dialog all OK")

asyncio.run(drive_ui())
print("\nALL SMOKE TESTS PASSED")
