"""`mclean doctor`: walk the connection one step at a time and say which step
broke, in terms of the provider's own setup instructions.

A failed login is the most common way a new user gives up, and the raw IMAP
error ("AUTHENTICATIONFAILED", "[ALERT] Application-specific password
required") rarely says which of three setup steps was missed. Each check here
maps a failure onto the step that fixes it.
"""

from __future__ import annotations

import socket
import ssl
from dataclasses import dataclass

from . import accounts as acct_mod
from . import db
from .providers import MailError

#: (substring of the server's error, what to do). Checked in order.
LOGIN_HINTS = {
    "gmail": [
        ("application-specific password required",
         "Gmail wants an app password, not your Google password. Step 2: "
         "https://myaccount.google.com/apppasswords"),
        ("web browser", "Google blocked the sign-in. Sign in once at "
         "https://mail.google.com, then retry with an app password."),
        ("imap access is disabled",
         "Step 3: turn on IMAP in Gmail -> Settings -> Forwarding and POP/IMAP."),
        ("invalid credentials",
         "Wrong app password, or 2-Step Verification is off (step 1: "
         "https://myaccount.google.com/signinoptions/two-step-verification). "
         "Create a fresh app password and enter it again."),
    ],
    "outlook": [
        ("authenticate failed", "The sign-in token was refused. Sign in again. "
         "If your organisation blocks third-party apps, set "
         "MAILCLEANER_MS_CLIENT_ID to your own Azure app."),
    ],
    "icloud": [
        ("authentication failed", "iCloud needs an app-specific password from "
         "https://account.apple.com/account/manage, and the @icloud.com address."),
    ],
    "yahoo": [
        ("authentication failed", "Yahoo needs an app password from "
         "https://login.yahoo.com/account/security."),
    ],
    "fastmail": [
        ("authentication failed", "Fastmail needs an app password with the "
         "Mail (IMAP) scope: https://app.fastmail.com/settings/security/devicekeys"),
    ],
    "zoho": [
        ("authentication failed", "Enable IMAP in Zoho Mail settings and use an "
         "application-specific password. Regional accounts use imap.zoho.eu / .in."),
    ],
    "protonmail": [
        ("", "Use the Bridge password from Bridge -> Mailbox details, and make "
         "sure Bridge is running."),
    ],
}


@dataclass
class Check:
    name: str
    ok: bool | None  # None = skipped
    detail: str = ""
    fix: str = ""


def login_fix(provider: str, error: str) -> str:
    low = (error or "").lower()
    for needle, fix in LOGIN_HINTS.get(provider, []):
        if needle in low:
            return fix
    return "Check the address and the (app) password, then enter it again."


def diagnose(account, connect_timeout: float = 8.0) -> list[Check]:
    """Every check in order. Later network checks are skipped once one fails."""
    out: list[Check] = []

    secret = acct_mod.get_secret(account)
    if secret is None:
        out.append(Check("Credential stored", False,
                         "nothing in the keychain for this account",
                         "Enter the app password again, or sign in again."))
    else:
        out.append(Check("Credential stored", True, secret.kind))

    try:
        addrs = {i[4][0] for i in socket.getaddrinfo(account.host, account.port,
                                                     type=socket.SOCK_STREAM)}
        out.append(Check("Server name resolves", True,
                         f"{account.host} -> {', '.join(sorted(addrs)[:3])}"))
    except socket.gaierror as e:
        out.append(Check("Server name resolves", False, str(e),
                         "Check the hostname and your network connection."))
        return out + _rest_skipped()

    try:
        with socket.create_connection((account.host, account.port), connect_timeout) as s:
            out.append(Check("Port reachable", True, f"{account.host}:{account.port}"))
            if account.security == "ssl":
                ctx = ssl.create_default_context()
                if not account.verify_cert:
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                try:
                    with ctx.wrap_socket(s, server_hostname=account.host) as t:
                        out.append(Check("TLS handshake", True, t.version() or ""))
                except ssl.SSLError as e:
                    out.append(Check("TLS handshake", False, str(e),
                                     "The server's certificate was refused. For a "
                                     "local bridge, use the Proton provider."))
                    return out + _rest_skipped(skip_tls=True)
            else:
                out.append(Check("TLS handshake", None, f"{account.security} - checked at login"))
    except OSError as e:
        hint = ("Is Proton Mail Bridge running?" if account.provider == "protonmail"
                else "A firewall or VPN may block IMAP; try another network.")
        out.append(Check("Port reachable", False, str(e), hint))
        return out + _rest_skipped(skip_tls=True)

    if secret is None:
        return out + [Check("Login", None, "no credential"),
                      Check("MOVE support", None), Check("Folders", None)]

    try:
        with acct_mod.backend(account) as client:
            out.append(Check("Login", True, account.email))
            if getattr(client, "supports_labels", False):
                out.append(Check("MOVE support", True, "not needed: Gmail uses labels"))
            elif getattr(client, "move_supported", True):
                out.append(Check("MOVE support", True, "archive/trash/label available"))
            else:
                out.append(Check("MOVE support", False,
                                 "server lacks the IMAP MOVE extension",
                                 "Browsing works; archive/trash/label are refused "
                                 "rather than emulated with a delete."))
            folders = client.sync_folders()
            out.append(Check("Folders", True,
                             f"{len(folders)} to index: {', '.join(folders[:4])}"
                             + ("..." if len(folders) > 4 else "")))
    except MailError as e:
        out.append(Check("Login", False, str(e).splitlines()[0][:200],
                         login_fix(account.provider, str(e))))
        out += [Check("MOVE support", None), Check("Folders", None)]
    return out


def _rest_skipped(skip_tls: bool = False) -> list[Check]:
    names = (["TLS handshake"] if not skip_tls else []) + ["Login", "MOVE support", "Folders"]
    return [Check(n, None, "skipped") for n in names if n]


def index_checks(account) -> list[Check]:
    out = []
    if not account.db_path.exists():
        return [Check("Local index", None, "not created yet", "Sync the mailbox.")]
    conn = db.connect(account)
    try:
        n = conn.execute("SELECT COUNT(*) c FROM emails").fetchone()["c"]
        last = db.get_state(conn, "last_sync")
    finally:
        conn.close()
    out.append(Check("Local index", n > 0, f"{n} messages",
                     "" if n else "Sync the mailbox."))
    from . import stats
    out.append(Check("Last sync", bool(last), stats.ago(last),
                     "" if last else "Sync the mailbox."))
    out.append(Check("Actions", True,
                     "on" if account.actions_enabled else "read-only"))
    return out
