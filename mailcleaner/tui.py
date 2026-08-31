"""Terminal dashboard.

One table, one breadcrumb, one status bar. Every screen is a "pane" pushed on a
stack; Enter drills down, Escape comes back. Rows carry the SQL predicate they
represent, so the same action code works on a domain, a sender, a saved cleanup
suggestion or a single message.
"""

from __future__ import annotations

import json
import time
import webbrowser
from dataclasses import dataclass, field

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button, DataTable, Footer, Input, Label, ListItem, ListView, Static,
)

from . import accounts as acct_mod
from . import actions, db, providers, stats, sync
from .accounts import Account, Store
from .providers import MailError

NAV = [
    ("dashboard", "Dashboard"),
    ("accounts", "Accounts"),
    ("attention", "Attention"),
    ("domains", "Domains"),
    ("senders", "Senders"),
    ("categories", "Categories"),
    ("frequency", "Frequency"),
    ("ages", "Age"),
    ("storage", "Storage"),
    ("cleanup", "Cleanup"),
    ("rules", "Rules"),
    ("history", "History"),
]

TREND = {"up": "^", "down": "v", "flat": "=", "new": "*", "-": " "}


@dataclass
class Pane:
    kind: str
    title: str
    where: str = "1=1"
    params: tuple = ()
    extra: dict = field(default_factory=dict)


class Confirm(ModalScreen[str]):
    """Sample-first confirmation. Nothing bulk happens without passing through here."""

    BINDINGS = [
        Binding("escape", "dismiss_none", "Cancel"),
        Binding("a", "pick('archive')", "Archive"),
        Binding("t", "pick('trash')", "Trash"),
        Binding("m", "pick('mark_read')", "Mark read"),
        Binding("l", "pick('label')", "Label"),
    ]

    def __init__(self, title: str, info: dict, allowed: list[str]):
        super().__init__()
        self.title_text = title
        self.info = info
        self.allowed = allowed

    def compose(self) -> ComposeResult:
        i = self.info
        lines = [f"[b]{self.title_text}[/b]", "[dim]Pick an action below. "
                 "Nothing has happened yet.[/dim]", ""]
        lines.append(f"  {i['eligible']} messages will be affected")
        if i["protected"]:
            lines.append(f"  {i['protected']} protected messages excluded")
        if i["oldest"]:
            span = f"{time.strftime('%b %Y', time.localtime(i['oldest']))}"
            span += f" - {time.strftime('%b %Y', time.localtime(i['newest']))}"
            lines.append(f"  {span}   {stats.human_size(i['bytes'])}")
        lines.append("")
        lines.append("[b]Categories[/b]")
        for c in i["categories"][:8]:
            warn = "  <-- protected" if c["protected"] else ""
            lines.append(f"  {c['key'] or 'unknown':<22}{c['emails']:>6}{warn}")
        lines.append("")
        lines.append("[b]Examples[/b]")
        for s in i["samples"][:6]:
            subj = (s["subject"] or "(no subject)")[:58]
            lines.append(f"  {s['from_email'][:30]:<32}{subj}")

        with Vertical(id="confirm-box"):
            yield VerticalScroll(Static("\n".join(lines), id="confirm-body"))
            with Horizontal(id="confirm-buttons"):
                for a, lbl in (("archive", "Archive (a)"), ("trash", "Trash (t)"),
                               ("mark_read", "Mark read (m)"), ("label", "Label (l)")):
                    if a in self.allowed:
                        yield Button(lbl, id=f"btn-{a}", variant="primary")
                yield Button("Cancel (esc)", id="btn-cancel")

    def action_pick(self, value: str) -> None:
        if value in self.allowed:
            self.dismiss(value)

    def action_dismiss_none(self) -> None:
        self.dismiss("")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        choice = (event.button.id or "").removeprefix("btn-")
        self.dismiss("" if choice == "cancel" else choice)


class Prompt(ModalScreen[str]):
    def __init__(self, question: str, default: str = ""):
        super().__init__()
        self.question = question
        self.default = default

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Label(self.question)
            yield Input(value=self.default, id="prompt-input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip())

    def on_key(self, event) -> None:
        if event.key == "escape":
            self.dismiss("")


class CleanerApp(App):
    CSS = """
    Screen { layers: base overlay; }
    #body { height: 1fr; }
    #nav { width: 18; border-right: solid $panel; padding: 0 1; }
    #nav > ListItem { padding: 0 1; }
    #main { padding: 0 1; height: 1fr; }
    #crumb { color: $accent; text-style: bold; height: 1; }
    #hint { color: $text-muted; height: 1; }
    #status { height: 1; color: $text-muted; border-top: solid $panel; padding: 0 1; }
    DataTable { height: 1fr; }
    #detail { height: 1fr; padding: 1 2; }
    #confirm-box {
        width: 92; height: 80%; border: round $accent; background: $surface;
        padding: 1 2; margin: 2 4;
    }
    #confirm-buttons { height: 3; align: left middle; }
    #confirm-buttons Button { margin-right: 1; }
    #prompt-box {
        width: 70; height: 7; border: round $accent; background: $surface;
        padding: 1 2; margin: 6 10;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("escape", "back", "Back"),
        Binding("s", "sync", "Sync"),
        Binding("r", "reclassify", "Reclassify"),
        Binding("a", "act('archive')", "Archive"),
        Binding("t", "act('trash')", "Trash"),
        Binding("m", "act('mark_read')", "Mark read"),
        Binding("l", "act('label')", "Label"),
        Binding("f", "act('star')", "Flag"),
        Binding("p", "rule('protect')", "Protect sender"),
        Binding("i", "rule('ignore')", "Ignore sender"),
        Binding("u", "undo", "Undo last"),
        Binding("o", "open_web", "Open in webmail"),
        Binding("A", "next_account", "Next account", show=False),
        Binding("d", "delete_rule", "Delete rule", show=False),
    ]

    def __init__(self, store: Store, account: Account):
        super().__init__()
        self.store = store
        self.account = account
        self.conn = db.connect(account)
        self.stack: list[Pane] = [Pane("dashboard", "Dashboard")]
        self.row_meta: list[dict] = []
        self.busy = False

    # -- layout -------------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Horizontal(id="body"):
            yield ListView(
                *[ListItem(Label(title), id=f"nav-{key}") for key, title in NAV],
                id="nav",
            )
            with Vertical(id="main"):
                yield Static("", id="crumb")
                yield Static("", id="hint")
                yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
                yield Static("", id="detail")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#detail", Static).display = False
        self.refresh_status()
        self.render_pane()
        self.query_one(DataTable).focus()

    # -- helpers ------------------------------------------------------------
    @property
    def pane(self) -> Pane:
        return self.stack[-1]

    def refresh_status(self, message: str = "") -> None:
        o = stats.overview(self.conn)
        last = db.get_state(self.conn, "last_sync")
        mode = "READ-ONLY" if not self.account.actions_enabled else "actions enabled"
        bits = [
            f"{self.account.display} [{self.account.provider_info.name}]",
            f"{o['total'] or 0} indexed",
            f"{o['unread'] or 0} unread",
            stats.human_size(o["bytes"]),
            f"synced {stats.ago(last)}",
            mode,
        ]
        text = "  |  ".join(bits)
        if message:
            text += f"     {message}"
        self.query_one("#status", Static).update(text)

    def notify_status(self, message: str) -> None:
        self.refresh_status(message)

    def push(self, pane: Pane) -> None:
        self.stack.append(pane)
        self.render_pane()

    def action_back(self) -> None:
        if len(self.stack) > 1:
            self.stack.pop()
            self.render_pane()

    # -- accounts -----------------------------------------------------------
    def switch_account(self, account: Account) -> None:
        """Point the whole dashboard at another mailbox. Nothing is shared."""
        if account.id == self.account.id:
            return
        self.conn.close()
        self.account = account
        self.store.set_active(account)
        self.conn = db.connect(account)
        self.stack = [Pane("dashboard", "Dashboard")]
        self.render_pane()
        self.notify_status(f"switched to {account.display}")

    def action_next_account(self) -> None:
        accts = self.store.accounts
        if len(accts) < 2:
            self.notify_status("only one account configured - add one: mclean add")
            return
        ids = [a.id for a in accts]
        self.switch_account(accts[(ids.index(self.account.id) + 1) % len(accts)])

    # -- navigation ---------------------------------------------------------
    @on(ListView.Selected, "#nav")
    def nav_selected(self, event: ListView.Selected) -> None:
        key = (event.item.id or "").removeprefix("nav-")
        title = dict(NAV)[key]
        self.stack = [Pane(key, title)]
        self.render_pane()
        self.query_one(DataTable).focus()

    @on(DataTable.RowSelected)
    def row_selected(self, event: DataTable.RowSelected) -> None:
        if event.cursor_row >= len(self.row_meta):
            return
        meta = self.row_meta[event.cursor_row]
        kind = self.pane.kind
        if kind == "accounts":
            if account := meta.get("account"):
                self.switch_account(account)
        elif kind == "domains":
            self.push(Pane("senders", f"Domains / {meta['key']}",
                           meta["where"], meta["params"],
                           {"domain": meta["key"]}))
        elif kind in ("senders", "categories", "frequency", "ages", "storage",
                      "cleanup"):
            self.push(Pane("messages", f"{self.pane.title} / {meta['key']}",
                           meta["where"], meta["params"]))
        elif kind in ("messages", "attention"):
            self.show_detail(meta)

    def current_meta(self) -> dict | None:
        table = self.query_one(DataTable)
        if table.cursor_row is None or table.cursor_row >= len(self.row_meta):
            return None
        return self.row_meta[table.cursor_row]

    # -- rendering ----------------------------------------------------------
    def render_pane(self) -> None:
        table = self.query_one(DataTable)
        detail = self.query_one("#detail", Static)
        detail.display = False
        table.display = True
        table.clear(columns=True)
        self.row_meta = []
        pane = self.pane
        self.query_one("#crumb", Static).update(pane.title)

        renderer = getattr(self, f"_render_{pane.kind}", None)
        if renderer:
            renderer(table, pane)
        self.refresh_status()

    def _hint(self, text: str) -> None:
        self.query_one("#hint", Static).update(text)

    @staticmethod
    def _cell(v) -> str:
        return f"{v:>6}" if isinstance(v, int) else str(v)

    def _add(self, table, values, meta) -> None:
        table.add_row(*[self._cell(v) for v in values])
        self.row_meta.append(meta)

    # dashboard
    def _render_dashboard(self, table, pane) -> None:
        table.display = False
        detail = self.query_one("#detail", Static)
        detail.display = True
        o = stats.overview(self.conn)
        cats = stats.categories(self.conn)
        doms = stats.domains(self.conn, limit=8)
        sug = stats.suggestions(self.conn)
        lines = []
        lines.append("[b]Mailbox[/b]")
        lines.append(f"  {o['total'] or 0} messages   {o['unread'] or 0} unread   "
                     f"{o['inbox'] or 0} in inbox   {stats.human_size(o['bytes'])}")
        lines.append(f"  {o['senders'] or 0} senders across {o['domains'] or 0} domains")
        if o["oldest"]:
            lines.append(f"  indexed {time.strftime('%d %b %Y', time.localtime(o['oldest']))}"
                         f" - {time.strftime('%d %b %Y', time.localtime(o['newest']))}")
        lines.append("")
        lines.append(f"  [b]{o['attention']}[/b] unread messages want attention   "
                     f"[b]{o['cleanup']}[/b] cleanup candidates   "
                     f"[b]{o['protected'] or 0}[/b] protected")
        lines.append("")
        lines.append("[b]Top domains[/b]")
        for d in doms:
            lines.append(f"  {d['key'][:32]:<34}{d['emails']:>7}{d['d30']:>7} /30d"
                         f"{d['unread'] or 0:>8} unread   {stats.human_size(d['bytes'])}")
        lines.append("")
        lines.append("[b]Categories[/b]")
        for c in cats[:10]:
            lines.append(f"  {(c['key'] or 'unknown')[:22]:<24}{c['emails']:>7}"
                         f"{c['unread'] or 0:>8} unread")
        lines.append("")
        lines.append("[b]Suggested cleanups[/b]")
        for s in sug:
            if s["emails"]:
                lines.append(f"  {s['key'][:36]:<38}{s['emails']:>7}   "
                             f"{stats.human_size(s['bytes'])}")
        self._hint("Pick a view on the left. s sync   r reclassify   q quit")
        detail.update("\n".join(lines))

    # grouped views
    def _render_domains(self, table, pane) -> None:
        table.add_columns("Domain", "Emails", "7d", "30d", "90d", "/week", "",
                          "Unread", "Inbox", "Size", "Last")
        for d in stats.domains(self.conn, limit=300):
            self._add(table, [
                d["key"][:38], d["emails"], d["d7"], d["d30"], d["d90"],
                d["per_week"], TREND.get(d["trend"], " "), d["unread"] or 0,
                d["inbox"] or 0, stats.human_size(d["bytes"]), stats.ago(d["last_seen"]),
            ], {"key": d["key"], "where": "from_domain = ?", "params": (d["key"],)})
        self._hint("Enter: senders in this domain   a/t/m/l act on the whole domain")

    def _render_senders(self, table, pane) -> None:
        domain = pane.extra.get("domain")
        table.add_columns("Sender", "Emails", "7d", "30d", "90d", "/week", "",
                          "Unread", "Size", "Last", "Dormant")
        for s in stats.senders(self.conn, limit=400, domain=domain):
            self._add(table, [
                s["key"][:42], s["emails"], s["d7"], s["d30"], s["d90"],
                s["per_week"], TREND.get(s["trend"], " "), s["unread"] or 0,
                stats.human_size(s["bytes"]), stats.ago(s["last_seen"]),
                f"{s['dormant_days']}d",
            ], {"key": s["key"], "where": "from_email = ?", "params": (s["key"],)})
        self._hint("Enter: messages   p protect sender   i ignore sender   a/t/m/l act")

    def _render_categories(self, table, pane) -> None:
        table.add_columns("Category", "Emails", "Unread", "Protected", "Size", "Last")
        for c in stats.categories(self.conn):
            key = c["key"] or "unknown"
            self._add(table, [
                key, c["emails"], c["unread"] or 0, c["protected"] or 0,
                stats.human_size(c["bytes"]), stats.ago(c["last_seen"]),
            ], {"key": key, "where": "category = ?", "params": (c["key"],)})
        self._hint("Enter: messages in this category")

    def _render_frequency(self, table, pane) -> None:
        table.add_columns("Frequency", "Senders", "Emails", "Unread")
        for f in stats.frequency(self.conn):
            self._add(table, [f["key"], f["senders"], f["emails"], f["unread"]],
                      {"key": f["key"], "where": "1=0", "params": ()})
        self._hint("How often each sender writes to you (last 30 days).")

    def _render_ages(self, table, pane) -> None:
        table.add_columns("Age", "Emails", "Unread", "Protected", "Size")
        now = int(time.time())
        for a, (name, lo, hi) in zip(stats.ages(self.conn), stats.AGE_BUCKETS):
            self._add(table, [
                a["key"], a["emails"], a["unread"] or 0, a["protected"] or 0,
                stats.human_size(a["bytes"]),
            ], {"key": a["key"],
                "where": "received_at <= ? AND received_at > ?",
                "params": (now - lo * 86400, now - hi * 86400)})
        self._hint("Enter: messages in this age band")

    def _render_storage(self, table, pane) -> None:
        table.add_columns("Domain", "Size", "Emails", "Avg", "Attachments")
        rows = stats.domains(self.conn, limit=100, order="bytes")
        for d in rows:
            att = stats.count(self.conn, "from_domain=? AND has_attachment=1", (d["key"],))
            self._add(table, [
                d["key"][:38], stats.human_size(d["bytes"]), d["emails"],
                stats.human_size((d["bytes"] or 0) // max(1, d["emails"])), att,
            ], {"key": d["key"], "where": "from_domain = ?", "params": (d["key"],)})
        self._hint("Largest senders by total size. Enter: their messages")

    def _render_cleanup(self, table, pane) -> None:
        table.add_columns("Suggestion", "Messages", "Size", "What it matches")
        for s in stats.suggestions(self.conn):
            self._add(table, [
                s["key"], s["emails"], stats.human_size(s["bytes"]), s["desc"],
            ], {"key": s["key"], "where": s["where"], "params": ()})
        self._hint("Enter: inspect messages   a archive   t trash   "
                   "(protected mail is always excluded)")

    def _render_attention(self, table, pane) -> None:
        table.add_columns("!", "From", "Subject", "Category", "When", "Size")
        marks = {"action required": "!!", "potentially important": "!", "read later": "."}
        for m in stats.attention(self.conn):
            self._add(table, [
                marks.get(m["attention"], " "), (m["from_email"] or "")[:30],
                (m["subject"] or "(no subject)")[:60],
                f"{m['category']}{'/' + m['subcategory'] if m['subcategory'] else ''}",
                stats.ago(m["received_at"]), stats.human_size(m["size"]),
            ], {"key": m["msg_key"], "where": "msg_key = ?",
                "params": (m["msg_key"],), "row": m})
        self._hint("Unread mail the classifier thinks matters. "
                   "Enter: details   o open in webmail")

    def _render_messages(self, table, pane) -> None:
        table.add_columns("", "From", "Subject", "Category", "Retention", "When", "Size")
        rows = stats.messages(self.conn, pane.where, pane.params, limit=1500)
        for m in rows:
            flag = ("P" if m["protected"] else "") + ("*" if m["is_starred"] else "")
            self._add(table, [
                (flag + ("u" if m["is_unread"] else "")).ljust(3),
                (m["from_email"] or "")[:28],
                (m["subject"] or "(no subject)")[:58],
                m["category"] or "", m["retention"] or "",
                stats.ago(m["received_at"]), stats.human_size(m["size"]),
            ], {"key": m["msg_key"], "where": "msg_key = ?",
                "params": (m["msg_key"],), "row": m})
        self._hint(f"{len(rows)} messages   P protected  * starred  u unread   "
                   "Enter: details   o open in webmail")

    def _render_accounts(self, table, pane) -> None:
        table.add_columns("", "Account", "Provider", "Mode", "Messages", "Unread",
                          "Size", "Last sync")
        for a in self.store.accounts:
            conn = self.conn if a.id == self.account.id else db.connect(a)
            try:
                o = stats.overview(conn)
                last = db.get_state(conn, "last_sync")
            finally:
                if conn is not self.conn:
                    conn.close()
            self._add(table, [
                ">" if a.id == self.account.id else " ",
                a.display[:32], a.provider_info.name[:22],
                "actions on" if a.actions_enabled else "read-only",
                o["total"] or 0, o["unread"] or 0,
                stats.human_size(o["bytes"]), stats.ago(last),
            ], {"key": a.id, "where": "1=0", "params": (), "account": a})
        self._hint("Enter switches account (or shift-A cycles). Each mailbox has "
                   "its own index, rules and history. Add one: mclean add")

    def _render_rules(self, table, pane) -> None:
        table.add_columns("id", "Match", "Value", "Action", "Category", "Created")
        for r in self.conn.execute("SELECT * FROM rules ORDER BY id DESC"):
            self._add(table, [
                r["id"], r["match_type"], r["match_value"], r["action"],
                r["category"] or "", stats.ago(r["created_at"]),
            ], {"key": r["id"], "where": "1=0", "params": (), "rule_id": r["id"]})
        self._hint("Rules override the classifier. Add them with p / i on a sender. "
                   "d deletes.  r re-runs classification.")

    def _render_history(self, table, pane) -> None:
        table.add_columns("Batch", "Action", "Detail", "Messages", "When", "Undone")
        for b in actions.batches(self.conn):
            self._add(table, [
                b["batch_id"], b["action"], b["detail"] or "", b["n"],
                stats.ago(b["ts"]), "yes" if b["undone"] else "",
            ], {"key": b["batch_id"], "where": "1=0", "params": (),
                "batch_id": b["batch_id"]})
        self._hint("Audit log. u undoes the selected batch.")

    def show_detail(self, meta: dict) -> None:
        m = meta.get("row")
        if not m:
            return
        labels = ", ".join(json.loads(m["labels"] or "[]")) or "-"
        lines = [
            f"[b]{m['subject'] or '(no subject)'}[/b]", "",
            f"  From        {m['from_name']} <{m['from_email']}>",
            f"  Domain      {m['from_domain']}",
            f"  Date        {time.strftime('%a %d %b %Y %H:%M', time.localtime(m['received_at']))}",
            f"  Size        {stats.human_size(m['size'])}"
            f"{'   has attachment' if m['has_attachment'] else ''}",
            f"  Labels      {labels}", "",
            f"  Category    {m['category']}"
            f"{'/' + m['subcategory'] if m['subcategory'] else ''}"
            f"   (confidence {m['confidence']})",
            f"  Attention   {m['attention']}",
            f"  Retention   {m['retention']}"
            f"{'   PROTECTED' if m['protected'] else ''}",
            f"  Why         {m['reasons']}",
            "", f"  List-Id     {m['list_id'] or '-'}",
            f"  Unsubscribe {'available' if m['unsubscribe'] else 'no'}",
            "", "[dim]Press o to open this message in your webmail, escape to go back.[/dim]",
        ]
        self.push(Pane("detail", "Message"))
        table = self.query_one(DataTable)
        table.display = False
        detail = self.query_one("#detail", Static)
        detail.display = True
        detail.update("\n".join(lines))
        self.stack[-1].extra["row"] = m
        self._hint("")

    def _render_detail(self, table, pane) -> None:
        pass

    # -- commands -----------------------------------------------------------
    def _client(self):
        try:
            return acct_mod.backend(self.account)
        except MailError as e:
            self.notify_status(str(e))
            return None

    def action_sync(self) -> None:
        if self.busy:
            return
        self.busy = True
        self.notify_status("syncing...")
        self._sync_worker(self.account)

    @work(thread=True)
    def _sync_worker(self, account: Account) -> None:
        conn = db.connect(account)
        try:
            client = self._client()
            if client is None:
                return
            with client:
                def progress(done, total):
                    self.call_from_thread(
                        self.notify_status, f"syncing {done}/{total}"
                    )
                n = sync.sync(conn, client, account.sync_days, progress=progress)
            self.call_from_thread(self.notify_status, f"sync done, {n} messages seen")
        except MailError as e:
            self.call_from_thread(self.notify_status, f"sync failed: {e}")
        except Exception as e:  # keep the UI alive on unexpected IMAP hiccups
            self.call_from_thread(self.notify_status, f"sync error: {e}")
        finally:
            conn.close()
            self.busy = False
            self.call_from_thread(self.render_pane)

    def action_reclassify(self) -> None:
        n = sync.reclassify_all(self.conn)
        self.render_pane()
        self.notify_status(f"reclassified {n} messages")

    def action_act(self, action: str) -> None:
        meta = self.current_meta()
        if not meta or meta["where"] == "1=0":
            return
        if not self.account.actions_enabled:
            self.notify_status(
                "Actions are disabled for this account. Enable with: "
                f"mclean enable-actions -a {self.account.id}"
            )
            return
        where, params = meta["where"], meta["params"]

        if action == "star":
            self._run_action("star", where, params, None)
            return

        info = actions.preview(self.conn, where, params)
        if info["eligible"] == 0:
            self.notify_status(
                f"nothing eligible ({info['protected']} protected messages excluded)"
            )
            return

        def after_confirm(choice: str | None) -> None:
            if not choice:
                return
            if choice == "label":
                def after_label(name: str | None) -> None:
                    if name:
                        self._run_action("label", where, params, name)
                question = ("Label to apply:" if self.account.provider_info.backend.supports_labels
                            else "Folder to file these into:")
                self.push_screen(Prompt(question, self.account.review_label),
                                 after_label)
            else:
                self._run_action(choice, where, params, None)

        self.push_screen(
            Confirm(str(meta["key"]), info, ["archive", "trash", "mark_read", "label"]),
            after_confirm,
        )

    def _run_action(self, action, where, params, label) -> None:
        if self.busy:
            return
        self.busy = True
        self.notify_status(f"{action}...")
        self._action_worker(action, where, params, label)

    @work(thread=True)
    def _action_worker(self, action, where, params, label) -> None:
        conn = db.connect(self.account)
        try:
            client = self._client()
            if client is None:
                return
            with client:
                res = actions.run(conn, client, action, where, params, label=label)
            msg = f"{action}: {res['count']} messages"
            if res["skipped"]:
                msg += f" ({res['skipped']} protected skipped)"
            if res["batch_id"]:
                msg += f"  batch {res['batch_id']} - press u to undo"
                self.last_batch = res["batch_id"]
            self.call_from_thread(self.notify_status, msg)
        except Exception as e:
            self.call_from_thread(self.notify_status, f"{action} failed: {e}")
        finally:
            conn.close()
            self.busy = False
            self.call_from_thread(self.render_pane)

    def action_undo(self) -> None:
        meta = self.current_meta()
        batch_id = None
        if self.pane.kind == "history" and meta:
            batch_id = meta.get("batch_id")
        else:
            rows = actions.batches(self.conn, limit=1)
            batch_id = rows[0]["batch_id"] if rows and not rows[0]["undone"] else None
        if not batch_id:
            self.notify_status("nothing to undo")
            return
        self._undo_worker(batch_id)

    @work(thread=True)
    def _undo_worker(self, batch_id: str) -> None:
        conn = db.connect(self.account)
        try:
            client = self._client()
            if client is None:
                return
            with client:
                n = actions.undo(conn, client, batch_id)
            self.call_from_thread(
                self.notify_status,
                f"undid {n} messages - run a sync to refresh the index",
            )
        except Exception as e:
            self.call_from_thread(self.notify_status, f"undo failed: {e}")
        finally:
            conn.close()
            self.call_from_thread(self.render_pane)

    def action_rule(self, kind: str) -> None:
        meta = self.current_meta()
        if not meta:
            return
        row = meta.get("row")
        if self.pane.kind == "senders":
            match_type, value = "sender", meta["key"]
        elif self.pane.kind == "domains":
            match_type, value = "domain", meta["key"]
        elif row:
            match_type, value = "sender", row["from_email"]
        else:
            return
        actions.add_rule(self.conn, match_type, value, kind)
        sync.reclassify_all(self.conn)
        self.render_pane()
        self.notify_status(f"rule added: {kind} {match_type} {value}")

    def action_delete_rule(self) -> None:
        meta = self.current_meta()
        if self.pane.kind != "rules" or not meta:
            return
        actions.delete_rule(self.conn, meta["rule_id"])
        sync.reclassify_all(self.conn)
        self.render_pane()
        self.notify_status("rule deleted")

    def action_open_web(self) -> None:
        meta = self.current_meta() or self.pane.extra
        row = meta.get("row") if meta else None
        if not row:
            self.notify_status("select a message first")
            return
        url = providers.message_url(self.account, row)
        if not url:
            self.notify_status(
                f"{self.account.provider_info.name} has no linkable web view"
            )
            return
        webbrowser.open(url)
        self.notify_status("opened in browser")


def run_app(store: Store, account: Account) -> None:
    CleanerApp(store, account).run()
