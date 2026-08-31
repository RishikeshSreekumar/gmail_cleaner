"""The provider registry: what each mail service needs to connect.

Adding a provider is one entry here plus, only if its IMAP dialect is unusual,
a backend subclass. Gmail is the only one that needs a subclass today.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Type

from .base import ImapBackend, MailError, MsgRef, RawMessage
from .gmail import GmailBackend

__all__ = [
    "PROVIDERS", "Provider", "get", "choices", "ImapBackend", "GmailBackend",
    "MailError", "MsgRef", "RawMessage", "backend_for", "message_url",
    "guess", "DOMAIN_HINTS",
]


@dataclass(frozen=True)
class Provider:
    key: str
    name: str
    host: str = ""
    port: int = 993
    #: "ssl" (implicit TLS), "starttls", or "none".
    security: str = "ssl"
    #: Relaxed only for local bridges that present a self-signed certificate.
    verify_cert: bool = True
    #: "password" (an app password, usually) or "xoauth2" (device-code sign-in).
    auth: str = "password"
    #: Name of the OAuth flow in providers.oauth, when auth is xoauth2.
    oauth_flow: str = ""
    backend: Type[ImapBackend] = ImapBackend
    #: Asked for at setup time because it varies per person/server.
    ask_host: bool = False
    web_url: str = ""
    setup_help: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def uses_oauth(self) -> bool:
        return self.auth == "xoauth2"


GMAIL_HELP = """[b]Gmail[/b]

  1. Turn on 2-Step Verification:  https://myaccount.google.com/signinoptions/two-step-verification
  2. Create an app password:       https://myaccount.google.com/apppasswords
     Google shows a 16-character code - paste it below.
  3. Enable IMAP:                  Gmail -> Settings -> Forwarding and POP/IMAP

No Google Cloud project, OAuth client or consent screen is involved. Revoke the
app password on that page and this tool loses all access."""

OUTLOOK_HELP = """[b]Outlook.com / Microsoft 365[/b]

Microsoft turned off IMAP basic authentication, so an app password will not work
here. Instead you will get a short code to type at https://microsoft.com/devicelogin.
Sign in there once and this tool keeps a refresh token in your keychain.

The permission requested is IMAP access to your own mailbox, nothing else. You
can revoke it any time at https://myaccount.microsoft.com/settings under the
apps you have given access to."""

PROTON_HELP = """[b]Proton Mail[/b]

Proton does not expose IMAP directly - mail is decrypted locally by Proton Mail
Bridge, which needs a paid Proton plan.

  1. Install and sign in to Bridge: https://proton.me/mail/bridge
  2. Open Bridge -> your account -> Mailbox details.
  3. Bridge shows a hostname (127.0.0.1), a port and a Bridge-specific password.
     Use that password below, not your Proton account password.

Bridge listens on your own machine with a self-signed certificate, so
certificate verification is relaxed for this account only."""

FASTMAIL_HELP = """[b]Fastmail[/b]

Create an app password at https://app.fastmail.com/settings/security/devicekeys
with the "Mail (IMAP)" scope, and paste it below."""

ICLOUD_HELP = """[b]iCloud Mail[/b]

Create an app-specific password at https://account.apple.com/account/manage
(Sign-In and Security -> App-Specific Passwords) and paste it below.
Your address must be the @icloud.com / @me.com one, not an alias."""

YAHOO_HELP = """[b]Yahoo Mail[/b]

Create an app password at https://login.yahoo.com/account/security
("Generate app password") and paste it below."""

ZOHO_HELP = """[b]Zoho Mail[/b]

Enable IMAP under Settings -> Mail Accounts -> IMAP Access, then create an
application-specific password at https://accounts.zoho.com/home#security/apppasswords.
If your account is on a regional data centre, use imap.zoho.eu / imap.zoho.in."""

GENERIC_HELP = """[b]Any other IMAP server[/b]

Enter the server's hostname and port. Most servers use 993 with implicit TLS;
older ones use 143 with STARTTLS.

The server must support the IMAP MOVE extension (RFC 6851) for archive, trash
and label to work - this tool refuses the copy-then-delete fallback because
that is a delete."""


PROVIDERS: dict[str, Provider] = {
    "gmail": Provider(
        key="gmail", name="Gmail", host="imap.gmail.com",
        backend=GmailBackend, web_url="https://mail.google.com/",
        setup_help=GMAIL_HELP,
        notes=["Label-shaped: archive and trash never move a message."],
    ),
    "outlook": Provider(
        key="outlook", name="Outlook.com / Microsoft 365",
        host="outlook.office365.com", auth="xoauth2", oauth_flow="microsoft",
        web_url="https://outlook.live.com/mail/0/",
        setup_help=OUTLOOK_HELP,
        notes=["Sign-in is a browser device code; there is no app password."],
    ),
    "protonmail": Provider(
        key="protonmail", name="Proton Mail (via Bridge)",
        host="127.0.0.1", port=1143, security="starttls", verify_cert=False,
        ask_host=True, web_url="https://mail.proton.me/",
        setup_help=PROTON_HELP,
        notes=["Requires Proton Mail Bridge running locally."],
    ),
    "fastmail": Provider(
        key="fastmail", name="Fastmail", host="imap.fastmail.com",
        web_url="https://app.fastmail.com/mail/", setup_help=FASTMAIL_HELP,
    ),
    "icloud": Provider(
        key="icloud", name="iCloud Mail", host="imap.mail.me.com",
        web_url="https://www.icloud.com/mail/", setup_help=ICLOUD_HELP,
    ),
    "yahoo": Provider(
        key="yahoo", name="Yahoo Mail", host="imap.mail.yahoo.com",
        web_url="https://mail.yahoo.com/", setup_help=YAHOO_HELP,
    ),
    "zoho": Provider(
        key="zoho", name="Zoho Mail", host="imap.zoho.com",
        web_url="https://mail.zoho.com/", setup_help=ZOHO_HELP,
    ),
    "imap": Provider(
        key="imap", name="Other IMAP server", ask_host=True,
        setup_help=GENERIC_HELP,
    ),
}

#: Guessed from the address at setup time, so the common cases need no choice.
DOMAIN_HINTS = {
    "gmail.com": "gmail", "googlemail.com": "gmail",
    "outlook.com": "outlook", "hotmail.com": "outlook", "live.com": "outlook",
    "msn.com": "outlook", "office365.com": "outlook",
    "protonmail.com": "protonmail", "proton.me": "protonmail", "pm.me": "protonmail",
    "fastmail.com": "fastmail", "fastmail.fm": "fastmail",
    "icloud.com": "icloud", "me.com": "icloud", "mac.com": "icloud",
    "yahoo.com": "yahoo", "yahoo.co.uk": "yahoo", "ymail.com": "yahoo",
    "zoho.com": "zoho", "zohomail.com": "zoho",
}


def get(key: str) -> Provider:
    try:
        return PROVIDERS[key]
    except KeyError:
        raise MailError(
            f"Unknown provider {key!r}. Known: {', '.join(PROVIDERS)}"
        )


def choices() -> list[Provider]:
    return list(PROVIDERS.values())


def guess(email: str) -> str:
    return DOMAIN_HINTS.get(email.rsplit("@", 1)[-1].lower(), "imap")


def message_url(account, row: dict) -> str | None:
    """A link to open one message in the provider's web UI.

    Only Gmail exposes a per-thread URL that can be built offline; for everyone
    else the best we can honestly do is open the mailbox itself.
    """
    provider = get(account.provider)
    if provider.key == "gmail":
        try:
            return ("https://mail.google.com/mail/u/0/#all/"
                    + format(int(row["thread_key"]), "x"))
        except (TypeError, ValueError, KeyError):
            pass
    return provider.web_url or None


def backend_for(account, secret) -> ImapBackend:
    """The connected-mailbox object every other module talks to."""
    return get(account.provider).backend(account, secret)
