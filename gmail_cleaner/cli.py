"""Command line entry point."""

from __future__ import annotations

import getpass
import sys
import time

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import actions, db, stats, sync
from .config import CONFIG_PATH, DB_PATH, Config, clear_password, get_password, set_password
from .imapclient import GmailImap, ImapError

app = typer.Typer(
    add_completion=False,
    help="Local Gmail observability + cleanup dashboard. Metadata only, no permanent delete.",
    invoke_without_command=True,
)
console = Console()

SETUP_HELP = """[b]One-time Gmail setup[/b]

  1. Turn on 2-Step Verification:  https://myaccount.google.com/signinoptions/two-step-verification
  2. Create an app password:       https://myaccount.google.com/apppasswords
     Name it anything (e.g. "gmail cleaner"). Google shows a 16-character code.
  3. Make sure IMAP is on:         Gmail -> Settings -> Forwarding and POP/IMAP -> Enable IMAP

No Google Cloud project, OAuth client or browser consent screen is involved.
The app password is stored in your system keychain and only ever sent to imap.gmail.com.
Revoke it any time on the app passwords page and this tool loses all access.
"""


@app.callback()
def main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        ui()


@app.command()
def setup() -> None:
    """Connect a Gmail account using an app password."""
    console.print(Panel(SETUP_HELP, border_style="cyan", title="Setup"))
    cfg = Config.load()
    email = typer.prompt("Gmail address", default=cfg.email or None)
    password = getpass.getpass("App password (16 chars, spaces ignored): ")
    password = password.replace(" ", "").strip()
    if not password:
        console.print("[red]No password entered.[/red]")
        raise typer.Exit(1)

    console.print("Testing connection...")
    try:
        with GmailImap(email, password) as client:
            validity = client.uidvalidity()
    except ImapError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    where = set_password(email, password)
    cfg.email = email
    cfg.save()
    db.connect()
    console.print(f"[green]Connected.[/green] App password stored in {where}.")
    console.print(f"Config: {CONFIG_PATH}\nIndex:  {DB_PATH}")
    console.print("\nNext: [b]gclean sync[/b] to index the last year, then [b]gclean[/b] for the dashboard.")


@app.command("sync")
def sync_cmd(
    days: int = typer.Option(None, "--days", "-d", help="How far back to index."),
    full: bool = typer.Option(False, "--full", help="Re-fetch everything in the window."),
) -> None:
    """Pull message metadata from Gmail into the local index."""
    cfg = Config.load()
    pw = get_password(cfg.email)
    if not cfg.email or not pw:
        console.print("[red]Not configured.[/red] Run: gclean setup")
        raise typer.Exit(1)
    if days:
        cfg.sync_days = days
        cfg.save()
    conn = db.connect()
    started = time.time()
    with console.status("Connecting to Gmail...") as status:
        try:
            with GmailImap(cfg.email, pw) as client:
                def progress(done, total):
                    status.update(f"Fetching metadata {done}/{total}")
                n = sync.sync(conn, client, cfg.sync_days, full=full, progress=progress)
        except ImapError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1)
    o = stats.overview(conn)
    console.print(
        f"[green]Synced[/green] {n} messages in {time.time() - started:.0f}s. "
        f"Index holds {o['total']} messages, {o['unread'] or 0} unread, "
        f"{stats.human_size(o['bytes'])}."
    )


@app.command()
def ui() -> None:
    """Open the dashboard (default)."""
    cfg = Config.load()
    conn = db.connect()
    if not cfg.email:
        console.print("[yellow]Not configured yet.[/yellow] Run: gclean setup")
        raise typer.Exit(1)
    from .tui import run_app

    run_app(conn, cfg)


@app.command("enable-actions")
def enable_actions() -> None:
    """Allow archive / trash / label / mark-read. Off by default."""
    cfg = Config.load()
    cfg.actions_enabled = True
    cfg.save()
    console.print(
        "[green]Actions enabled.[/green] Trash means Gmail's Trash (recoverable "
        "for 30 days). This tool still cannot permanently delete anything."
    )


@app.command("disable-actions")
def disable_actions() -> None:
    """Go back to read-only."""
    cfg = Config.load()
    cfg.actions_enabled = False
    cfg.save()
    console.print("[green]Back to read-only.[/green]")


@app.command()
def status() -> None:
    """Show what is indexed."""
    cfg = Config.load()
    conn = db.connect()
    o = stats.overview(conn)
    t = Table(show_header=False, box=None)
    t.add_row("Account", cfg.email or "(not configured)")
    t.add_row("Credential", "stored" if get_password(cfg.email) else "missing")
    t.add_row("Mode", "actions enabled" if cfg.actions_enabled else "read-only")
    t.add_row("Window", f"last {cfg.sync_days} days")
    t.add_row("Last sync", stats.ago(db.get_state(conn, "last_sync")))
    t.add_row("Messages", str(o["total"] or 0))
    t.add_row("Unread", str(o["unread"] or 0))
    t.add_row("Protected", str(o["protected"] or 0))
    t.add_row("Cleanup candidates", str(o["cleanup"]))
    t.add_row("Size", stats.human_size(o["bytes"]))
    t.add_row("Database", str(DB_PATH))
    console.print(t)


@app.command()
def top(
    by: str = typer.Option("domain", help="domain | sender | category"),
    limit: int = typer.Option(20, "-n"),
) -> None:
    """Quick terminal summary without opening the dashboard."""
    conn = db.connect()
    rows = {
        "domain": lambda: stats.domains(conn, limit),
        "sender": lambda: stats.senders(conn, limit),
        "category": lambda: stats.categories(conn)[:limit],
    }[by]()
    t = Table(title=f"Top by {by}")
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


@app.command()
def reclassify() -> None:
    """Re-run the classifier over the index (after editing rules)."""
    conn = db.connect()
    n = sync.reclassify_all(conn)
    console.print(f"[green]Reclassified[/green] {n} messages.")


@app.command()
def logout() -> None:
    """Forget the app password. The index stays."""
    cfg = Config.load()
    clear_password(cfg.email)
    console.print("Credential removed. Revoke it too at https://myaccount.google.com/apppasswords")


if __name__ == "__main__":
    app()
