"""Accounts: several mailboxes, from several providers, each fully independent.

Every account gets its own directory under ~/.mailcleaner/accounts/<id>/ holding
its own SQLite index, and its own credential entry in the system keychain.
Nothing is shared between accounts except this config file, so switching
accounts is just picking a different directory - there is no cross-account query
anywhere in the app, and removing an account removes exactly its own data.

Credentials live in the macOS/Windows/Linux keychain via `keyring` when it is
available, otherwise in a 0600 file inside the account's directory. They are
only ever sent to that account's own IMAP host (and, for OAuth accounts, the
provider's login endpoint).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from . import providers
from .providers import oauth

LEGACY_DIR = Path(
    os.environ.get("GMAIL_CLEANER_HOME", Path.home() / ".gmail_cleaner")
)
APP_DIR = Path(os.environ.get("MAILCLEANER_HOME", Path.home() / ".mailcleaner"))
CONFIG_PATH = APP_DIR / "config.json"
ACCOUNTS_DIR = APP_DIR / "accounts"

KEYRING_SERVICE = "mailcleaner"
CONFIG_VERSION = 2


@dataclass
class Account:
    """One mailbox. Connection details plus the preferences that apply to it."""

    id: str
    email: str
    provider: str = "imap"
    label: str = ""
    host: str = ""
    port: int = 993
    security: str = "ssl"
    verify_cert: bool = True
    auth: str = "password"
    oauth_client_id: str = ""
    #: Empty means "let the provider decide" (Gmail: All Mail; others: every
    #: mailbox except junk/drafts/trash).
    sync_folders: list[str] = field(default_factory=list)
    #: Actions stay off per account until that account is explicitly opted in.
    actions_enabled: bool = False
    sync_days: int = 365
    review_label: str = "Cleanup/Review"
    created_at: int = 0

    @classmethod
    def from_provider(cls, provider_key: str, email: str, **over) -> "Account":
        p = providers.get(provider_key)
        acct = cls(
            id=make_id(email, provider_key),
            email=email,
            provider=p.key,
            label=over.pop("label", "") or email,
            host=over.pop("host", "") or p.host,
            port=int(over.pop("port", 0) or p.port),
            security=over.pop("security", "") or p.security,
            verify_cert=bool(over.pop("verify_cert", p.verify_cert)),
            auth=p.auth,
            created_at=int(time.time()),
        )
        for k, v in over.items():
            if k in cls.__dataclass_fields__:
                setattr(acct, k, v)
        return acct

    @property
    def provider_info(self) -> providers.Provider:
        return providers.get(self.provider)

    @property
    def display(self) -> str:
        return self.label or self.email

    @property
    def dir(self) -> Path:
        return ACCOUNTS_DIR / self.id

    @property
    def db_path(self) -> Path:
        return self.dir / "index.db"

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Account":
        known = {f.name: data[f.name] for f in fields(cls) if f.name in data}
        return cls(**known)


def make_id(email: str, provider_key: str) -> str:
    """A stable, filesystem-safe id. The provider is part of it so the same
    address on two services can be tracked as two independent accounts."""
    slug = re.sub(r"[^a-z0-9]+", "-", email.lower()).strip("-")
    return f"{provider_key}-{slug}"[:80]


@dataclass
class Store:
    """The whole config file: the accounts and which one is in front."""

    accounts: list[Account] = field(default_factory=list)
    active: str = ""

    # -- persistence --------------------------------------------------------
    @classmethod
    def load(cls) -> "Store":
        migrate_legacy()
        if not CONFIG_PATH.exists():
            return cls()
        data = json.loads(CONFIG_PATH.read_text() or "{}")
        store = cls(
            accounts=[Account.from_dict(a) for a in data.get("accounts", [])],
            active=data.get("active", ""),
        )
        if store.accounts and not store.get(store.active):
            store.active = store.accounts[0].id
        return store

    def save(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps({
            "version": CONFIG_VERSION,
            "active": self.active,
            "accounts": [a.to_dict() for a in self.accounts],
        }, indent=2))
        CONFIG_PATH.chmod(0o600)

    # -- lookup -------------------------------------------------------------
    def get(self, ident: str | None) -> Account | None:
        """Match on id, then address or label, then a unique prefix.

        Anything that matches two accounts resolves to nothing: the same address
        can exist on two providers, and guessing which one a bulk action meant
        is exactly the kind of mistake this tool must not make.
        """
        if not ident:
            return None
        ident = ident.strip()
        for a in self.accounts:
            if a.id == ident:
                return a
        low = ident.lower()
        for pool in (
            [a for a in self.accounts
             if a.email.lower() == low or a.label.lower() == low],
            [a for a in self.accounts
             if low in a.id.lower() or low in a.email.lower()
             or low in a.label.lower()],
        ):
            if len(pool) == 1:
                return pool[0]
            if len(pool) > 1:
                return None
        return None

    def require(self, ident: str | None = None) -> Account:
        acct = self.get(ident) if ident else self.current
        if acct is None:
            if ident:
                raise LookupError(
                    f"No single account matches {ident!r} - it is unknown, or it "
                    "matches more than one. Use the account id."
                )
            raise LookupError("No accounts configured yet. Run: mclean add")
        return acct

    @property
    def current(self) -> Account | None:
        return self.get(self.active) or (self.accounts[0] if self.accounts else None)

    # -- mutation -----------------------------------------------------------
    def add(self, account: Account) -> Account:
        existing = self.get(account.id)
        if existing:
            self.accounts[self.accounts.index(existing)] = account
        else:
            self.accounts.append(account)
        if not self.active:
            self.active = account.id
        account.dir.mkdir(parents=True, exist_ok=True)
        self.save()
        return account

    def remove(self, account: Account, purge: bool = True) -> None:
        self.accounts = [a for a in self.accounts if a.id != account.id]
        if self.active == account.id:
            self.active = self.accounts[0].id if self.accounts else ""
        clear_secret(account)
        if purge and account.dir.exists():
            shutil.rmtree(account.dir, ignore_errors=True)
        self.save()

    def set_active(self, account: Account) -> None:
        self.active = account.id
        self.save()

    def update(self, account: Account) -> None:
        for i, a in enumerate(self.accounts):
            if a.id == account.id:
                self.accounts[i] = account
        self.save()


# -- credentials -----------------------------------------------------------


@dataclass
class AuthSecret:
    """What the backend needs to authenticate, whichever scheme applies."""

    kind: str  # "password" | "xoauth2"
    password: str = ""
    blob: dict = field(default_factory=dict)
    account: Account | None = None

    def access_token(self) -> str:
        """A valid bearer token, refreshing and re-storing it if it has aged out."""
        if not self.blob:
            raise providers.MailError("No stored sign-in. Run: mclean login")
        if oauth.is_expired(self.blob):
            flow = oauth.flow_for(
                self.blob.get("flow") or "microsoft", self.blob.get("client_id")
            )
            self.blob = oauth.refresh_token(flow, self.blob)
            if self.account:
                store_oauth(self.account, self.blob)
        return self.blob["access_token"]


def _fallback_path(account: Account, suffix: str) -> Path:
    return account.dir / f"secret{suffix}"


def _keyring_user(account: Account, suffix: str) -> str:
    return f"{account.id}{suffix}"


def _write_secret(account: Account, suffix: str, value: str) -> str:
    account.dir.mkdir(parents=True, exist_ok=True)
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, _keyring_user(account, suffix), value)
        return "system keychain"
    except Exception:
        p = _fallback_path(account, suffix)
        p.write_text(value)
        p.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return str(p)


def _read_secret(account: Account, suffix: str) -> str | None:
    try:
        import keyring

        got = keyring.get_password(KEYRING_SERVICE, _keyring_user(account, suffix))
        if got:
            return got
    except Exception:
        pass
    p = _fallback_path(account, suffix)
    return p.read_text().strip() if p.exists() else None


def _drop_secret(account: Account, suffix: str) -> None:
    try:
        import keyring

        keyring.delete_password(KEYRING_SERVICE, _keyring_user(account, suffix))
    except Exception:
        pass
    p = _fallback_path(account, suffix)
    if p.exists():
        p.unlink()


def set_password(account: Account, password: str) -> str:
    return _write_secret(account, "", password)


def store_oauth(account: Account, blob: dict) -> str:
    return _write_secret(account, ".oauth", json.dumps(blob))


def get_secret(account: Account) -> AuthSecret | None:
    if account.auth == "xoauth2":
        raw = _read_secret(account, ".oauth")
        if not raw:
            return None
        try:
            blob = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return AuthSecret("xoauth2", blob=blob, account=account)
    pw = _read_secret(account, "")
    return AuthSecret("password", password=pw, account=account) if pw else None


def clear_secret(account: Account) -> None:
    _drop_secret(account, "")
    _drop_secret(account, ".oauth")


def backend(account: Account):
    """Connectable backend for `account`, or a clear error about what is missing."""
    secret = get_secret(account)
    if secret is None:
        what = ("a browser sign-in" if account.auth == "xoauth2"
                else "an app password")
        raise providers.MailError(
            f"No stored credential for {account.display}. "
            f"This account needs {what}: mclean login -a {account.id}"
        )
    return providers.backend_for(account, secret)


# -- migration from the Gmail-only layout ----------------------------------


def migrate_legacy() -> None:
    """Move a single-account ~/.gmail_cleaner install into the new layout.

    Runs once: it is skipped as soon as ~/.mailcleaner/config.json exists.
    The old directory is left untouched, so nothing is lost if this misfires.
    """
    if CONFIG_PATH.exists() or not (LEGACY_DIR / "config.json").exists():
        return
    try:
        old = json.loads((LEGACY_DIR / "config.json").read_text() or "{}")
    except json.JSONDecodeError:
        return
    email = old.get("email") or ""
    if not email:
        return

    acct = Account.from_provider("gmail", email)
    acct.actions_enabled = bool(old.get("actions_enabled", False))
    acct.sync_days = int(old.get("sync_days", 365))
    acct.review_label = old.get("review_label", "Cleanup/Review")
    acct.dir.mkdir(parents=True, exist_ok=True)

    # The old index is deliberately not carried over: it was keyed on Gmail
    # message ids under a different schema, and re-syncing rebuilds it in full.

    old_pw = LEGACY_DIR / "app_password"
    moved_pw = False
    if old_pw.exists():
        set_password(acct, old_pw.read_text().strip())
        moved_pw = True
    if not moved_pw:
        try:
            import keyring

            pw = keyring.get_password("gmail-cleaner", email)
            if pw:
                set_password(acct, pw)
        except Exception:
            pass

    store = Store(accounts=[acct], active=acct.id)
    store.save()
