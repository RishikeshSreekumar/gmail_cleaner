"""Command line entry point."""

from __future__ import annotations

import getpass
import time
import webbrowser

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import accounts as acct_mod
from . import db, providers, stats, sync
from .accounts import Account, Store
from .providers import MailError, oauth

app = typer.Typer(
    add_completion=False,
    help="Local mailbox observability + cleanup dashboard. Metadata only, "
         "no permanent delete. Gmail, Outlook, Proton, Fastmail, iCloud, "
         "Yahoo, Zoho or any IMAP server.",
    invoke_without_command=True,
)
console = Console()

ACCOUNT_OPT = typer.Option(
    None, "--account", "-a",
    help="Which account to act on (id, address or label). Defaults to the active one.",
)


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
    if ctx.invoked_subcommand is None:
        ui(account_id)


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
        console.print(f"[red]{e}[/red]")
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
def list_accounts() -> None:
    """List every configured mailbox."""
    store = _store()
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
    """Open the terminal dashboard (default)."""
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
    """Open the browser dashboard (local only, same safety rules as the TUI)."""
    store = _store()
    account = _pick(store, account_id)
    if store.active != account.id:
        store.set_active(account)
    db.connect(account).close()
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
def status(account_id: str | None = ACCOUNT_OPT) -> None:
    """Show what is indexed for an account."""
    store = _store()
    account = _pick(store, account_id)
    conn = _open(account)
    o = stats.overview(conn)
    secret = acct_mod.get_secret(account)
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
) -> None:
    """Quick terminal summary without opening the dashboard."""
    store = _store()
    account = _pick(store, account_id)
    conn = _open(account)
    rows = {
        "domain": lambda: stats.domains(conn, limit),
        "sender": lambda: stats.senders(conn, limit),
        "category": lambda: stats.categories(conn)[:limit],
    }[by]()
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


if __name__ == "__main__":
    app()
