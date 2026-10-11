"""Command line entry point."""

from __future__ import annotations

import csv
import getpass
import json
import sys
import time
import webbrowser
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import accounts as acct_mod
from . import actions, db, doctor, policies, providers, stats, sync
from .accounts import Account, Store
from .providers import MailError, oauth

app = typer.Typer(
    add_completion=False,
    help="Local mailbox observability + cleanup. Run with no command to open "
         "the app in your browser. Metadata only, no permanent delete. Gmail, "
         "Outlook, Proton, Fastmail, iCloud, Yahoo, Zoho or any IMAP server.",
    invoke_without_command=True,
)
console = Console()

ACCOUNT_OPT = typer.Option(
    None, "--account", "-a",
    help="Which account to act on (id, address or label). Defaults to the active one.",
)
JSON_OPT = typer.Option(False, "--json", help="Print machine-readable JSON instead.")


def _emit_json(obj) -> None:
    """JSON on stdout for scripts; nothing else is printed alongside it."""
    sys.stdout.write(json.dumps(obj, default=str, indent=2) + "\n")


# -- helpers ---------------------------------------------------------------


def _store() -> Store:
    return Store.load()


def _pick(store: Store, ident: str | None) -> Account:
    try:
        return store.require(ident)
    except LookupError as e:
        console.print(f"[red]{e}[/red]")
        if store.accounts:
            console.print("Known accounts: " + ", ".join(a.id for a in store.accounts))
        raise typer.Exit(1)


def _open(account: Account):
    return db.connect(account)


# -- account management ----------------------------------------------------


@app.callback()
def main(ctx: typer.Context, account_id: str | None = ACCOUNT_OPT) -> None:
    # No command: open the browser app. Everything is reachable from there, so a
    # downloaded binary is usable by someone who never opens a terminal again.
    if ctx.invoked_subcommand is None:
        gui(account_id, port=8765, no_browser=False, verbose=False)


@app.command("providers")
def list_providers() -> None:
    """Show the mail services this tool knows how to connect to."""
    t = Table(title="Supported providers")
    for col in ("Key", "Service", "Host", "Sign-in", "Notes"):
        t.add_column(col)
    for p in providers.choices():
        t.add_row(
            p.key, p.name, p.host or "(you supply it)",
            "browser device code" if p.uses_oauth else "app password",
            "; ".join(p.notes),
        )
    console.print(t)
    console.print("\nAdd one with: [b]mclean add[/b]")


@app.command("add")
def add_account(
    email: str = typer.Option(None, "--email", "-e", help="The mailbox address."),
    provider: str = typer.Option(None, "--provider", "-p", help="Provider key."),
    label: str = typer.Option(None, "--label", "-l", help="A short name for it."),
    host: str = typer.Option(None, "--host", help="IMAP host, for custom servers."),
    port: int = typer.Option(None, "--port", help="IMAP port."),
) -> None:
    """Connect another mailbox. Accounts are independent of each other."""
    store = _store()
    email = (email or typer.prompt("Email address")).strip()

    if not provider:
        guess = providers.guess(email)
        console.print(_provider_menu(guess))
        answer = typer.prompt("Provider", default=guess)
        provider = answer.strip().lower()
    if provider not in providers.PROVIDERS:
        console.print(f"[red]Unknown provider {provider!r}.[/red] "
                      f"Known: {', '.join(providers.PROVIDERS)}")
        raise typer.Exit(1)

    info = providers.get(provider)
    console.print(Panel(info.setup_help, border_style="cyan",
                        title=f"Setup - {info.name}"))
    if info.setup_url and sys.stdin.isatty():
        if typer.confirm(f"Open {info.setup_url} in your browser now?", default=True):
            try:
                webbrowser.open(info.setup_url)
            except Exception:
                pass

    if info.ask_host:
        host = host or typer.prompt("IMAP host", default=info.host or None)
        port = port or int(typer.prompt("IMAP port", default=str(info.port)))
        security = typer.prompt(
            "Connection security (ssl / starttls / none)", default=info.security
        )
    else:
        host, port, security = info.host, info.port, info.security

    account = Account.from_provider(
        provider, email, host=host, port=port, security=security, label=label or "",
    )
    if store.get(account.id):
        console.print(
            f"[yellow]{email} on {info.name} is already configured "
            f"({account.id}).[/yellow] Re-authenticating it."
        )
    account.dir.mkdir(parents=True, exist_ok=True)

    if not _authenticate(account):
        raise typer.Exit(1)

    console.print("Testing the connection...")
    try:
        client = acct_mod.backend(account)
        with client:
            folders = client.sync_folders()
    except MailError as e:
        console.print(f"[red]{e}[/red]\n")
        console.print("Checking each step to find what is wrong:")
        _print_checks(doctor.diagnose(account))
        console.print(f"\nFix that step, then run [b]mclean add -e {email} "
                      f"-p {provider}[/b] again.")
        raise typer.Exit(1)

    store.add(account)
    store.set_active(account)
    db.connect(account).close()
    console.print(
        f"[green]Connected.[/green] {account.display} is now the active account."
    )
    console.print(f"  Folders to index: {len(folders)} "
                  f"({', '.join(folders[:4])}{'...' if len(folders) > 4 else ''})")
    console.print(f"  Index: {account.db_path}")
    console.print("\nNext: [b]mclean sync[/b], then [b]mclean[/b] for the dashboard.")


def _provider_menu(guess: str) -> str:
    lines = ["Which service is this?"]
    for p in providers.choices():
        mark = " [green](guessed)[/green]" if p.key == guess else ""
        lines.append(f"  [b]{p.key:<12}[/b] {p.name}{mark}")
    return "\n".join(lines)


def _authenticate(account: Account) -> bool:
    """Obtain and store whatever credential this provider needs."""
    if account.auth == "xoauth2":
        return _device_login(account)
    password = getpass.getpass("App password (spaces are ignored): ")
    password = password.replace(" ", "").strip()
    if not password:
        console.print("[red]No password entered.[/red]")
        return False
    where = acct_mod.set_password(account, password)
    console.print(f"Password stored in {where}.")
    return True


def _device_login(account: Account) -> bool:
    info = account.provider_info
    flow = oauth.flow_for(info.oauth_flow, account.oauth_client_id or None)
    try:
        device = oauth.start_device_login(flow)
    except MailError as e:
        console.print(f"[red]{e}[/red]")
        return False

    url = device.get("verification_uri") or device.get("verification_url")
    console.print(Panel(
        f"Open [b]{url}[/b] and enter the code\n\n"
        f"        [b cyan]{device['user_code']}[/b cyan]\n\n"
        f"Sign in as [b]{account.email}[/b]. Waiting...",
        border_style="cyan", title="Browser sign-in",
    ))
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        with console.status("Waiting for the browser sign-in to finish..."):
            blob = oauth.poll_for_token(flow, device)
    except MailError as e:
        console.print(f"[red]{e}[/red]")
        return False
    where = acct_mod.store_oauth(account, blob)
    console.print(f"[green]Signed in.[/green] Token stored in {where}.")
    return True


@app.command("accounts")
def list_accounts(as_json: bool = JSON_OPT) -> None:
    """List every configured mailbox."""
    store = _store()
    if as_json:
        rows = []
        for a in store.accounts:
            conn = _open(a)
            try:
                o = stats.overview(conn)
                last = db.get_state(conn, "last_sync")
            finally:
                conn.close()
            rows.append({"id": a.id, "address": a.email, "label": a.display,
                         "provider": a.provider, "active": a.id == store.active,
                         "actions_enabled": a.actions_enabled,
                         "messages": o["total"] or 0, "last_sync": last})
        return _emit_json(rows)
    if not store.accounts:
        console.print("[yellow]No accounts yet.[/yellow] Add one: mclean add")
        return
    t = Table(title="Accounts")
    for col in ("", "Id", "Address", "Provider", "Mode", "Messages", "Last sync"):
        t.add_column(col)
    for a in store.accounts:
        conn = _open(a)
        try:
            o = stats.overview(conn)
            last = db.get_state(conn, "last_sync")
        finally:
            conn.close()
        t.add_row(
            "*" if a.id == store.active else " ",
            a.id, a.display, a.provider_info.name,
            "actions on" if a.actions_enabled else "read-only",
            str(o["total"] or 0), stats.ago(last),
        )
    console.print(t)
    console.print("[dim]* is the active account. Switch with: mclean use <id>[/dim]")


@app.command("use")
def use_account(ident: str = typer.Argument(..., help="Account id, address or label.")) -> None:
    """Switch the active account."""
    store = _store()
    account = _pick(store, ident)
    store.set_active(account)
    console.print(f"[green]Active account:[/green] {account.display} "
                  f"({account.provider_info.name})")


@app.command("remove")
def remove_account(
    ident: str = typer.Argument(..., help="Account id, address or label."),
    keep_index: bool = typer.Option(False, "--keep-index",
                                    help="Leave the local index on disk."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
) -> None:
    """Forget an account. Only local data is removed; no mail is touched."""
    store = _store()
    account = _pick(store, ident)
    if not yes:
        typer.confirm(
            f"Remove {account.display} and its local index? "
            "(No mail is deleted; this only forgets the account here.)",
            abort=True,
        )
    store.remove(account, purge=not keep_index)
    console.print(f"[green]Removed[/green] {account.display}.")
    if account.auth == "xoauth2":
        console.print("Also revoke access at https://myaccount.microsoft.com/settings")


@app.command("login")
def login(account_id: str | None = ACCOUNT_OPT) -> None:
    """Re-enter the password, or redo the browser sign-in, for an account."""
    store = _store()
    account = _pick(store, account_id)
    console.print(Panel(account.provider_info.setup_help, border_style="cyan",
                        title=f"{account.display} - {account.provider_info.name}"))
    if not _authenticate(account):
        raise typer.Exit(1)
    try:
        with acct_mod.backend(account):
            pass
    except MailError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print("[green]Credential accepted.[/green]")


@app.command()
def logout(account_id: str | None = ACCOUNT_OPT) -> None:
    """Forget an account's credential. The index stays."""
    store = _store()
    account = _pick(store, account_id)
    acct_mod.clear_secret(account)
    console.print(f"Credential for {account.display} removed.")
    if account.provider == "gmail":
        console.print("Revoke it too at https://myaccount.google.com/apppasswords")


# -- working with an account ----------------------------------------------


@app.command("sync")
def sync_cmd(
    account_id: str | None = ACCOUNT_OPT,
    all_accounts: bool = typer.Option(False, "--all", help="Sync every account."),
    days: int = typer.Option(None, "--days", "-d", help="How far back to index."),
    full: bool = typer.Option(False, "--full", help="Re-fetch everything in the window."),
) -> None:
    """Pull message metadata from the server into the local index."""
    store = _store()
    targets = store.accounts if all_accounts else [_pick(store, account_id)]
    if not targets:
        console.print("[red]No accounts configured.[/red] Run: mclean add")
        raise typer.Exit(1)
    for account in targets:
        _sync_one(store, account, days, full)


def _sync_one(store: Store, account: Account, days: int | None, full: bool) -> None:
    if days:
        account.sync_days = days
        store.update(account)
    conn = _open(account)
    started = time.time()
    try:
        with console.status(f"[{account.display}] connecting...") as status:
            client = acct_mod.backend(account)
            with client:
                def progress(done, total):
                    status.update(f"[{account.display}] fetching {done}/{total}")
                n = sync.sync(conn, client, account.sync_days, full=full,
                              progress=progress)
        o = stats.overview(conn)
        console.print(
            f"[green]{account.display}[/green]: synced {n} messages in "
            f"{time.time() - started:.0f}s. Index holds "
            f"{o['total']} messages, {o['unread'] or 0} unread, "
            f"{stats.human_size(o['bytes'])}."
        )
    except MailError as e:
        console.print(f"[red]{account.display}: {e}[/red]")
    finally:
        conn.close()


@app.command()
def ui(account_id: str | None = ACCOUNT_OPT) -> None:
    """Open the terminal dashboard."""
    store = _store()
    account = _pick(store, account_id)
    from .tui import run_app

    run_app(store, account)


@app.command()
def gui(
    account_id: str | None = ACCOUNT_OPT,
    port: int = typer.Option(8765, "--port", "-p", help="Port on 127.0.0.1."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Do not open a browser."),
    verbose: bool = typer.Option(False, "--verbose", help="Log every request."),
) -> None:
    """Open the browser app (the default when mclean is run with no command)."""
    store = _store()
    if store.accounts:
        account = _pick(store, account_id)
        if store.active != account.id:
            store.set_active(account)
        db.connect(account).close()
    # With no mailbox yet, the page itself walks the user through adding one.
    from .web import serve

    serve(port=port, open_browser=not no_browser, verbose=verbose)


@app.command("enable-actions")
def enable_actions(account_id: str | None = ACCOUNT_OPT) -> None:
    """Allow archive / trash / label / mark-read for one account. Off by default."""
    store = _store()
    account = _pick(store, account_id)
    account.actions_enabled = True
    store.update(account)
    verb = ("adds the Trash label" if account.provider == "gmail"
            else "moves mail to the Trash folder")
    console.print(
        f"[green]Actions enabled for {account.display}.[/green] Trash {verb}, "
        "which you can undo here or empty yourself. This tool still cannot "
        "permanently delete anything."
    )


@app.command("disable-actions")
def disable_actions(account_id: str | None = ACCOUNT_OPT) -> None:
    """Go back to read-only for one account."""
    store = _store()
    account = _pick(store, account_id)
    account.actions_enabled = False
    store.update(account)
    console.print(f"[green]{account.display} is read-only again.[/green]")


@app.command()
def status(account_id: str | None = ACCOUNT_OPT, as_json: bool = JSON_OPT) -> None:
    """Show what is indexed for an account."""
    store = _store()
    account = _pick(store, account_id)
    conn = _open(account)
    o = stats.overview(conn)
    secret = acct_mod.get_secret(account)
    if as_json:
        out = {"account": account.id, "address": account.email,
               "provider": account.provider, "host": account.host,
               "credential": bool(secret), "actions_enabled": account.actions_enabled,
               "sync_days": account.sync_days, "locales": account.locales or "all",
               "llm_provider": account.llm_provider,
               "folders": db.get_state(conn, "folders") or [],
               "last_sync": db.get_state(conn, "last_sync"), **o}
        conn.close()
        return _emit_json(out)
    t = Table(show_header=False, box=None)
    t.add_row("Account", f"{account.display}  ({account.id})")
    t.add_row("Provider", account.provider_info.name)
    t.add_row("Server", f"{account.host}:{account.port} ({account.security})")
    t.add_row("Sign-in", "browser sign-in" if account.auth == "xoauth2"
                          else "app password")
    t.add_row("Credential", "stored" if secret else "missing")
    t.add_row("Mode", "actions enabled" if account.actions_enabled else "read-only")
    t.add_row("Window", f"last {account.sync_days} days")
    t.add_row("Folders", ", ".join(db.get_state(conn, "folders") or []) or "-")
    t.add_row("Last sync", stats.ago(db.get_state(conn, "last_sync")))
    t.add_row("Messages", str(o["total"] or 0))
    t.add_row("Unread", str(o["unread"] or 0))
    t.add_row("Protected", str(o["protected"] or 0))
    t.add_row("Cleanup candidates", str(o["cleanup"]))
    t.add_row("Size", stats.human_size(o["bytes"]))
    t.add_row("Database", str(account.db_path))
    console.print(t)
    conn.close()


@app.command()
def top(
    account_id: str | None = ACCOUNT_OPT,
    by: str = typer.Option("domain", help="domain | sender | category"),
    limit: int = typer.Option(20, "-n"),
    as_json: bool = JSON_OPT,
) -> None:
    """Quick terminal summary without opening the dashboard."""
    store = _store()
    account = _pick(store, account_id)
    conn = _open(account)
    if by not in ("domain", "sender", "category"):
        console.print("[red]--by must be domain, sender or category[/red]")
        raise typer.Exit(1)
    rows = {
        "domain": lambda: stats.domains(conn, limit),
        "sender": lambda: stats.senders(conn, limit),
        "category": lambda: stats.categories(conn)[:limit],
    }[by]()
    if as_json:
        conn.close()
        return _emit_json(rows)
    t = Table(title=f"{account.display} - top by {by}")
    t.add_column(by.title())
    for col in ("Emails", "30d", "Unread", "Size", "Last"):
        t.add_column(col, justify="right")
    for r in rows:
        t.add_row(
            str(r["key"] or "unknown"), str(r["emails"]), str(r.get("d30", "-")),
            str(r.get("unread") or 0), stats.human_size(r.get("bytes")),
            stats.ago(r.get("last_seen")),
        )
    console.print(t)
    conn.close()


@app.command()
def reclassify(account_id: str | None = ACCOUNT_OPT) -> None:
    """Re-run the classifier over the index (after editing rules)."""
    store = _store()
    account = _pick(store, account_id)
    conn = _open(account)
    n = sync.reclassify_all(conn)
    console.print(f"[green]Reclassified[/green] {n} messages for {account.display}.")
    conn.close()


# -- doctor ------------------------------------------------------------------


def _print_checks(checks) -> None:
    for c in checks:
        mark = ("[green]ok[/green]  " if c.ok else
                "[dim]--[/dim]  " if c.ok is None else "[red]FAIL[/red]")
        line = f"  {mark} {c.name:<22} {c.detail}"
        console.print(line)
        if c.fix and c.ok is False:
            console.print(f"         [yellow]-> {c.fix}[/yellow]")


@app.command("doctor")
def doctor_cmd(account_id: str | None = ACCOUNT_OPT, as_json: bool = JSON_OPT) -> None:
    """Check an account's connection step by step and say what to fix."""
    store = _store()
    account = _pick(store, account_id)
    checks = doctor.diagnose(account) + doctor.index_checks(account)
    if as_json:
        return _emit_json([c.__dict__ for c in checks])
    console.print(f"[b]{account.display}[/b] ({account.provider_info.name}, "
                  f"{account.host}:{account.port})")
    _print_checks(checks)
    if any(c.ok is False for c in checks):
        raise typer.Exit(1)


# -- export / digest ---------------------------------------------------------


def _parse_filter(where: str | None) -> tuple[str, tuple]:
    if not where:
        return "1=1", ()
    try:
        return policies.compile_filter(json.loads(where))
    except json.JSONDecodeError as e:
        console.print(f"[red]--where is not valid JSON: {e}[/red]")
    except policies.FilterError as e:
        console.print(f"[red]{e}[/red]")
    raise typer.Exit(1)


@app.command("export")
def export(
    what: str = typer.Argument(..., help="senders | domains | categories | messages"),
    account_id: str | None = ACCOUNT_OPT,
    fmt: str = typer.Option("csv", "--format", "-f", help="csv | json"),
    out: Path = typer.Option(None, "--output", "-o", help="File to write (default stdout)."),
    where: str = typer.Option(None, "--where",
                              help='Messages only: a filter, e.g. \'{"category":"newsletter"}\''),
    limit: int = typer.Option(100000, "-n"),
) -> None:
    """Export what the dashboard shows, for a spreadsheet or a script."""
    store = _store()
    account = _pick(store, account_id)
    conn = _open(account)
    try:
        if what == "senders":
            rows = stats.senders(conn, limit)
        elif what == "domains":
            rows = stats.domains(conn, limit)
        elif what == "categories":
            rows = stats.categories(conn)
        elif what == "messages":
            w, params = _parse_filter(where)
            cols = ("received_at from_name from_email from_domain subject size "
                    "category subcategory attention retention protected is_unread "
                    "is_inbox is_starred list_id folder").split()
            rows = [{c: r[c] for c in cols}
                    for r in stats.messages(conn, w, params, limit=limit)]
        else:
            console.print("[red]export senders | domains | categories | messages[/red]")
            raise typer.Exit(1)
    finally:
        conn.close()

    fh = open(out, "w", newline="", encoding="utf-8") if out else sys.stdout
    try:
        if fmt == "json":
            fh.write(json.dumps(rows, default=str, indent=2) + "\n")
        elif fmt == "csv":
            if rows:
                w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
        else:
            console.print("[red]--format must be csv or json[/red]")
            raise typer.Exit(1)
    finally:
        if out:
            fh.close()
    if out:
        console.print(f"Wrote {len(rows)} {what} to {out}")


@app.command("digest")
def digest_cmd(
    account_id: str | None = ACCOUNT_OPT,
    all_accounts: bool = typer.Option(False, "--all", help="Every account."),
    days: int = typer.Option(7, "--days", "-d"),
    as_json: bool = JSON_OPT,
) -> None:
    """What changed recently: new mail, what wants you, who got louder."""
    store = _store()
    targets = store.accounts if all_accounts else [_pick(store, account_id)]
    out = {}
    for a in targets:
        conn = _open(a)
        try:
            out[a.id] = stats.digest(conn, days)
        finally:
            conn.close()
    if as_json:
        return _emit_json(out)
    for a in targets:
        console.print(stats.digest_text(out[a.id], a.display))


# -- compound rules ------------------------------------------------------------

rules_app = typer.Typer(help="Compound rules: conditions + an action. They suggest "
                             "until you mark them auto.")
app.add_typer(rules_app, name="rules")


@rules_app.command("list")
def rules_list(account_id: str | None = ACCOUNT_OPT, as_json: bool = JSON_OPT) -> None:
    """Show protect/ignore rules and compound rules with what each matches now."""
    account = _pick(_store(), account_id)
    conn = _open(account)
    try:
        simple = [dict(r) for r in conn.execute("SELECT * FROM rules ORDER BY id")]
        pols = policies.list_all(conn)
    finally:
        conn.close()
    if as_json:
        return _emit_json({"rules": simple, "policies": pols})
    if simple:
        t = Table(title="Overrides (protect / ignore / category)")
        for col in ("id", "Match", "Value", "Action", "Category"):
            t.add_column(col)
        for r in simple:
            t.add_row(str(r["id"]), r["match_type"], r["match_value"], r["action"],
                      r["category"] or "")
        console.print(t)
    t = Table(title="Compound rules")
    for col in ("id", "Name", "When", "Then", "Mode", "Matches now", "Size"):
        t.add_column(col)
    for p in pols:
        then = p["action"] + (f" '{p['label']}'" if p["label"] else "")
        t.add_row(str(p["id"]), p["name"], p["summary"], then,
                  "auto" if p["auto"] else "suggest", str(p["matches"]),
                  stats.human_size(p["bytes"]))
    console.print(t)
    if not pols:
        console.print("[dim]None yet. Example: mclean rules add \"Old newsletters\" "
                      "--category newsletter --older-than 180 --action archive[/dim]")


@rules_app.command("add")
def rules_add(
    name: str = typer.Argument(..., help="A short name."),
    action: str = typer.Option(..., "--action", help="archive | mark_read | label | star"),
    label: str = typer.Option(None, "--label", help="For --action label."),
    where: str = typer.Option(None, "--where", help="A JSON filter (see `mclean rules fields`)."),
    sender: str = typer.Option(None, "--sender"),
    domain: str = typer.Option(None, "--domain"),
    category: str = typer.Option(None, "--category"),
    older_than: int = typer.Option(None, "--older-than", help="Days."),
    unread: bool = typer.Option(None, "--unread/--read"),
    auto: bool = typer.Option(False, "--auto", help="Apply on `rules apply` and scheduled runs."),
    account_id: str | None = ACCOUNT_OPT,
) -> None:
    """Add a compound rule. It only suggests unless --auto is given."""
    account = _pick(_store(), account_id)
    cond = json.loads(where) if where else {}
    for k, v in (("sender", sender), ("domain", domain), ("category", category),
                 ("older_than_days", older_than), ("unread", unread)):
        if v is not None:
            cond[k] = v
    conn = _open(account)
    try:
        pid = policies.add(conn, name, cond, action, label, auto)
        p = [x for x in policies.list_all(conn) if x["id"] == pid][0]
    except (policies.FilterError, json.JSONDecodeError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        conn.close()
    console.print(f"[green]Rule {pid} added[/green] ({'auto' if auto else 'suggest'}): "
                  f"when {p['summary']} -> {action}. Matches {p['matches']} messages now "
                  f"({p['protected']} protected, always excluded).")


@rules_app.command("fields")
def rules_fields() -> None:
    """The fields a filter (--where) can use."""
    t = Table(title="Filter fields")
    for col in ("Field", "Type", "Meaning"):
        t.add_column(col)
    for k, (kind, help_) in policies.FIELDS.items():
        t.add_row(k, kind, help_)
    console.print(t)


@rules_app.command("remove")
def rules_remove(rule_id: int, account_id: str | None = ACCOUNT_OPT) -> None:
    """Delete a compound rule (mail is not touched)."""
    account = _pick(_store(), account_id)
    conn = _open(account)
    ok = policies.remove(conn, rule_id)
    conn.close()
    console.print("[green]Removed.[/green]" if ok else "[yellow]No such rule.[/yellow]")


@rules_app.command("auto")
def rules_auto(rule_id: int, on: bool = typer.Option(True, "--on/--off"),
               account_id: str | None = ACCOUNT_OPT) -> None:
    """Switch a compound rule between suggest and auto."""
    account = _pick(_store(), account_id)
    conn = _open(account)
    ok = policies.set_auto(conn, rule_id, on)
    conn.close()
    console.print(f"Rule {rule_id}: {'auto' if on else 'suggest'}" if ok
                  else "[yellow]No such rule.[/yellow]")


@rules_app.command("run")
def rules_run(account_id: str | None = ACCOUNT_OPT) -> None:
    """Preview what every rule would do now. Changes nothing."""
    account = _pick(_store(), account_id)
    conn = _open(account)
    try:
        for p in policies.list_all(conn):
            where, params = policies.where_for(p)
            info = actions.preview(conn, where, params)
            console.print(f"[b]{p['id']}. {p['name']}[/b] "
                          f"({'auto' if p['auto'] else 'suggest'}) -> {p['action']}: "
                          f"{info['eligible']} messages, {stats.human_size(info['bytes'])}, "
                          f"{info['protected']} protected excluded")
            for m in info["samples"][:3]:
                console.print(f"    {m['from_email'][:30]:<32}{(m['subject'] or '')[:60]}")
    finally:
        conn.close()


@rules_app.command("apply")
def rules_apply(
    account_id: str | None = ACCOUNT_OPT,
    rule_id: int = typer.Option(None, "--rule", help="Apply just this rule, auto or not."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation."),
) -> None:
    """Apply auto rules (or one named rule) to the mailbox. Each is undoable."""
    store = _store()
    account = _pick(store, account_id)
    if not account.actions_enabled:
        console.print(f"[red]Actions are disabled for {account.display}.[/red] "
                      f"Run: mclean enable-actions -a {account.id}")
        raise typer.Exit(1)
    conn = _open(account)
    try:
        pols = [p for p in policies.list_all(conn)
                if (p["id"] == rule_id if rule_id else p["auto"])]
        if not pols:
            console.print("Nothing to apply (no auto rules; use --rule ID for one).")
            return
        for p in pols:
            console.print(f"  {p['id']}. {p['name']}: {p['action']} {p['matches']} messages")
        if not yes:
            typer.confirm("Apply these now?", abort=True)
        with acct_mod.backend(account) as client:
            res = policies.apply(conn, client, only_auto=rule_id is None,
                                 policy_id=rule_id)
        for r in res:
            console.print(f"[green]{r['policy']}[/green]: {r['count']} messages"
                          + (f" (batch {r['batch_id']}, undo from History)"
                             if r["batch_id"] else ""))
    except MailError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        conn.close()


# -- unsubscribe ---------------------------------------------------------------

unsub_app = typer.Typer(help="Unsubscribe from mailing lists via List-Unsubscribe.")
app.add_typer(unsub_app, name="unsubscribe")


@unsub_app.command("list")
def unsub_list(account_id: str | None = ACCOUNT_OPT, limit: int = typer.Option(40, "-n"),
               as_json: bool = JSON_OPT) -> None:
    """Senders that offer an unsubscribe, loudest first."""
    from . import unsubscribe

    account = _pick(_store(), account_id)
    conn = _open(account)
    rows = unsubscribe.senders(conn, limit)
    conn.close()
    if as_json:
        return _emit_json(rows)
    t = Table(title=f"{account.display} - can unsubscribe")
    for col in ("Sender", "Emails", "Unread", "Size", "How", "Done"):
        t.add_column(col)
    for r in rows:
        t.add_row(r["key"][:44], str(r["emails"]), str(r["unread"] or 0),
                  stats.human_size(r["bytes"]), r["method"],
                  stats.ago(r["unsubscribed_at"]) if r["unsubscribed_at"] else "")
    console.print(t)


@unsub_app.command("sender")
def unsub_sender(
    address: str = typer.Argument(..., help="The sender address."),
    archive: bool = typer.Option(False, "--archive",
                                 help="Also archive their unprotected inbox mail."),
    yes: bool = typer.Option(False, "--yes", "-y"),
    account_id: str | None = ACCOUNT_OPT,
) -> None:
    """Unsubscribe from one sender (one-click where offered, else open the link)."""
    from . import unsubscribe

    store = _store()
    account = _pick(store, account_id)
    if not account.actions_enabled:
        console.print(f"[red]Actions are disabled for {account.display}.[/red] "
                      f"Run: mclean enable-actions -a {account.id}")
        raise typer.Exit(1)
    conn = _open(account)
    try:
        opt = unsubscribe.options(conn, address)
        if not opt:
            console.print(f"[yellow]{address} has not offered an unsubscribe.[/yellow]")
            raise typer.Exit(1)
        what = {"one-click": f"send a one-click unsubscribe to {opt['host']}",
                "link": f"open the unsubscribe page on {opt['host']} in your browser",
                "mailto": "open an unsubscribe email in your mail client"}[opt["method"]]
        if not yes:
            typer.confirm(f"This will {what}. It cannot be undone from here. Continue?",
                          abort=True)
        try:
            res = unsubscribe.unsubscribe(conn, address)
        except unsubscribe.UnsubscribeError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
        if res["status"] == "ok":
            console.print(f"[green]Unsubscribed[/green] ({res['host']} answered "
                          f"HTTP {res['http_status']}).")
        else:
            webbrowser.open(res["open"])
            console.print("Opened it for you - finish there.")
        if archive:
            where, params = "from_email = ? AND is_inbox = 1", (opt["sender"],)
            info = actions.preview(conn, where, params)
            console.print(f"Archive {info['eligible']} inbox messages from {address} "
                          f"({info['protected']} protected stay)?")
            if info["eligible"] and (yes or typer.confirm("Archive them?", default=True)):
                with acct_mod.backend(account) as client:
                    r = actions.run(conn, client, actions.ARCHIVE, where, params)
                console.print(f"[green]Archived {r['count']}[/green] (batch {r['batch_id']}).")
    finally:
        conn.close()


# -- LLM advisor ---------------------------------------------------------------

llm_app = typer.Typer(help="Optional LLM advisor: proposes rules, never acts.")
app.add_typer(llm_app, name="llm")


@llm_app.command("setup")
def llm_setup(
    provider: str = typer.Option(..., "--provider", "-p", help="off | ollama | anthropic"),
    model: str = typer.Option(None, "--model", "-m"),
    host: str = typer.Option(None, "--host", help="Ollama URL (default http://127.0.0.1:11434)."),
    share_subjects: bool = typer.Option(True, "--share-subjects/--no-share-subjects",
                                        help="Send recent subject lines, or only sender stats."),
    cloud_ok: bool = typer.Option(False, "--cloud-ok",
                                  help="Confirm sending metadata to a cloud API."),
    account_id: str | None = ACCOUNT_OPT,
) -> None:
    """Turn the LLM advisor on or off for one account."""
    from . import llm

    store = _store()
    account = _pick(store, account_id)
    if provider not in llm.PROVIDERS:
        console.print(f"[red]--provider must be one of {', '.join(llm.PROVIDERS)}[/red]")
        raise typer.Exit(1)
    if provider == "anthropic":
        sent = ("sender addresses, message counts, read rates"
                + (" and up to 5 recent subject lines per sender" if share_subjects else ""))
        console.print(Panel(
            f"The Anthropic provider sends {sent} for the senders it is asked about "
            "to api.anthropic.com. No message bodies, ever. Nothing is sent until you "
            "run `mclean llm suggest` or ask a question.", title="Cloud model",
            border_style="yellow"))
        if not cloud_ok and not typer.confirm("Allow this for " + account.display + "?"):
            raise typer.Exit(1)
        if sys.stdin.isatty() and typer.confirm(
                "Store an Anthropic API key in the keychain for this account? "
                "(No: use ANTHROPIC_API_KEY or an `ant auth login` profile)", default=False):
            key = getpass.getpass("API key: ").strip()
            if key:
                acct_mod.set_llm_key(account, key)
    if provider != "anthropic":
        acct_mod.clear_llm_key(account)
    account.llm_provider = provider
    account.llm_model = model or (llm.DEFAULT_MODELS.get(provider, "") if provider != "off" else "")
    account.llm_host = host or ""
    account.llm_share_subjects = share_subjects
    store.update(account)
    if provider == "off":
        console.print(f"LLM advisor off for {account.display}.")
    else:
        console.print(f"[green]LLM advisor on[/green] for {account.display}: "
                      f"{provider} / {account.llm_model}. Next: mclean llm suggest")


@llm_app.command("suggest")
def llm_suggest(account_id: str | None = ACCOUNT_OPT,
                limit: int = typer.Option(40, "-n", help="How many senders to ask about.")) -> None:
    """Ask the model about unsure or loud senders. Stores pending suggestions."""
    from . import llm

    account = _pick(_store(), account_id)
    conn = _open(account)
    try:
        with console.status("Asking the model...") as st:
            res = llm.suggest_rules(conn, account, limit=limit,
                                    progress=lambda d, t: st.update(f"Asked about {d}/{t} senders"))
    except llm.LLMError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        conn.close()
    console.print(f"Asked about {res['asked']} senders with {res['model']}: "
                  f"{res['stored']} suggestions, {res['dropped']} dropped by the safety "
                  "checks. Review: mclean llm review")


@llm_app.command("review")
def llm_review(account_id: str | None = ACCOUNT_OPT, as_json: bool = JSON_OPT) -> None:
    """List pending suggestions."""
    from . import llm

    account = _pick(_store(), account_id)
    conn = _open(account)
    rows = llm.suggestions(conn)
    conn.close()
    if as_json:
        return _emit_json(rows)
    t = Table(title="Pending suggestions")
    for col in ("id", "Sender", "Suggests", "Emails", "Conf", "Why"):
        t.add_column(col)
    for r in rows:
        what = r["action"] + (f" -> {r['category']}" if r["category"] else "")
        t.add_row(str(r["id"]), r["match_value"][:36], what, str(r["emails"]),
                  f"{r['confidence']:.2f}", (r["reason"] or "")[:60])
    console.print(t)
    if rows:
        console.print("[dim]mclean llm accept ID [ID...] / mclean llm reject ID [ID...][/dim]")


def _decide(ids: list[int], accept: bool, account_id) -> None:
    from . import llm

    account = _pick(_store(), account_id)
    conn = _open(account)
    try:
        for i in ids:
            try:
                r = llm.decide(conn, i, accept)
                console.print(f"{'[green]accepted[/green]' if accept else 'rejected'} "
                              f"{r['action']} {r['match_value']}")
            except llm.LLMError as e:
                console.print(f"[yellow]{i}: {e}[/yellow]")
    finally:
        conn.close()


@llm_app.command("accept")
def llm_accept(ids: list[int], account_id: str | None = ACCOUNT_OPT) -> None:
    """Turn suggestions into rules (and reclassify)."""
    _decide(ids, True, account_id)


@llm_app.command("reject")
def llm_reject(ids: list[int], account_id: str | None = ACCOUNT_OPT) -> None:
    """Dismiss suggestions; they are not asked about again."""
    _decide(ids, False, account_id)


@llm_app.command("ask")
def llm_ask(text: str = typer.Argument(..., help='e.g. "unread newsletters older than a year"'),
            account_id: str | None = ACCOUNT_OPT, as_json: bool = JSON_OPT) -> None:
    """Plain-English search: shows the filter it built and what it matches."""
    from . import llm

    account = _pick(_store(), account_id)
    conn = _open(account)
    try:
        doms = [d["key"] for d in stats.domains(conn, 60)]
        q = llm.parse_query(text, account, known_domains=doms)
        where, params = policies.compile_filter(q["filter"])
        info = actions.preview(conn, where, params)
    except llm.LLMError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    finally:
        conn.close()
    if as_json:
        return _emit_json({**q, "total": info["total"], "eligible": info["eligible"]})
    console.print(f"[b]{q['summary']}[/b]\n[dim]{q['explanation']}[/dim]")
    console.print(f"{info['total']} messages ({info['protected']} protected), "
                  f"{stats.human_size(info['bytes'])}")
    for m in info["samples"][:8]:
        console.print(f"  {m['from_email'][:30]:<32}{(m['subject'] or '')[:60]}")
    console.print(f"[dim]Save it as a rule: mclean rules add NAME --where "
                  f"'{json.dumps(q['filter'])}' --action archive[/dim]")


# -- schedule / background -------------------------------------------------------

schedule_app = typer.Typer(help="Sync in the background with the OS scheduler.")
app.add_typer(schedule_app, name="schedule")


@schedule_app.command("install")
def schedule_install(
    every: int = typer.Option(6, "--every", help="Hours between runs."),
    apply_rules: bool = typer.Option(False, "--apply-rules",
                                     help="Also apply auto rules on accounts with actions on."),
    notify: bool = typer.Option(True, "--notify/--no-notify", help="Desktop notification."),
) -> None:
    """Install a background sync job for every account."""
    from . import schedule

    try:
        console.print("[green]Installed:[/green] " + schedule.install(every, apply_rules, notify))
    except (ValueError, RuntimeError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)


@schedule_app.command("remove")
def schedule_remove() -> None:
    """Remove the background job."""
    from . import schedule

    console.print(schedule.remove())


@schedule_app.command("status")
def schedule_status(as_json: bool = JSON_OPT) -> None:
    """Is the background job installed, and what did it last do."""
    from . import schedule

    st = schedule.status()
    if as_json:
        return _emit_json(st)
    console.print(f"Installed: {st['installed']}   ({st['where']})")
    console.print(f"Log: {st['log']}")
    if st["last_line"]:
        console.print(f"Last: {st['last_line']}")


@app.command("background-run", hidden=True)
def background_run(apply_rules: bool = typer.Option(False, "--apply-rules"),
                   notify: bool = typer.Option(False, "--notify")) -> None:
    """What the scheduled job runs."""
    from . import schedule

    schedule.background_run(apply_rules, notify)


# -- MCP / eval / locale -----------------------------------------------------------


@app.command("mcp")
def mcp() -> None:
    """Run the read-only MCP server on stdio (for Claude and other MCP clients)."""
    from . import mcp_server

    mcp_server.serve()


eval_app = typer.Typer(help="Measure the classifier against mail you label by hand.")
app.add_typer(eval_app, name="eval")


@eval_app.command("export")
def eval_export(out: Path = typer.Option(Path("labels.csv"), "--output", "-o"),
                n: int = typer.Option(300, "-n"),
                seed: int = typer.Option(None, "--seed"),
                account_id: str | None = ACCOUNT_OPT) -> None:
    """Write a random sample to label (fill true_category and true_protected)."""
    from . import evaluate

    account = _pick(_store(), account_id)
    conn = _open(account)
    k = evaluate.export_sample(conn, out, n, seed)
    conn.close()
    console.print(f"Wrote {k} messages to {out}. Fill in true_category and "
                  "true_protected (1/0), then: mclean eval score " + str(out))


@eval_app.command("score")
def eval_score(path: Path, account_id: str | None = ACCOUNT_OPT,
               as_json: bool = JSON_OPT) -> None:
    """Score today's classifier and rules against a labelled file."""
    from . import evaluate

    store = _store()
    account = store.get(account_id) if account_id else store.current
    conn = _open(account) if account else None
    try:
        r = evaluate.score_file(path, conn)
    finally:
        if conn:
            conn.close()
    if as_json:
        return _emit_json(r)
    colour = "green" if r["missed_protection"] == 0 else "red"
    console.print(f"[{colour}]Missed protection: {r['missed_protection']}[/{colour}] "
                  f"of {r['protection_labelled']} labelled "
                  f"(over-protected: {r['over_protection']})")
    for m in r["missed_examples"]:
        console.print(f"   {m['from_email']}: {m['subject']} (predicted {m['predicted']})")
    console.print(f"Category accuracy: {r['accuracy']:.1%} over {r['labelled']} messages")
    t = Table()
    for col in ("Category", "Support", "Precision", "Recall", "F1"):
        t.add_column(col)
    for c in r["categories"]:
        t.add_row(c["category"], str(c["support"]), f"{c['precision']:.2f}",
                  f"{c['recall']:.2f}", f"{c['f1']:.2f}")
    console.print(t)


@app.command("locale")
def locale_cmd(
    set_: str = typer.Option(None, "--set",
                             help="Comma-separated packs, e.g. en,in. 'all' for every pack."),
    account_id: str | None = ACCOUNT_OPT,
) -> None:
    """Show or choose the classifier's keyword packs for an account."""
    from .classify import LOCALES

    store = _store()
    account = _pick(store, account_id)
    if set_ is None:
        cur = account.locales or ["all"]
        console.print(f"{account.display}: {', '.join(cur)}   "
                      f"(available: {', '.join(LOCALES)})")
        return
    chosen = [] if set_.strip() == "all" else [x.strip() for x in set_.split(",") if x.strip()]
    bad = [x for x in chosen if x not in LOCALES]
    if bad:
        console.print(f"[red]Unknown pack(s): {', '.join(bad)}[/red] "
                      f"Available: {', '.join(LOCALES)}")
        raise typer.Exit(1)
    account.locales = chosen
    store.update(account)
    conn = _open(account)
    n = sync.reclassify_all(conn)
    conn.close()
    console.print(f"[green]{account.display}[/green]: packs {', '.join(chosen) or 'all'}; "
                  f"reclassified {n} messages.")


if __name__ == "__main__":
    app()
