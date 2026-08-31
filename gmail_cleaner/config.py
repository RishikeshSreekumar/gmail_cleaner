"""Local config + credential storage.

The app password lives in the macOS Keychain (via keyring) when available,
otherwise in a 0600 file next to the database. Nothing is ever sent anywhere
except imap.gmail.com.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, asdict
from pathlib import Path

APP_DIR = Path(os.environ.get("GMAIL_CLEANER_HOME", Path.home() / ".gmail_cleaner"))
CONFIG_PATH = APP_DIR / "config.json"
DB_PATH = APP_DIR / "index.db"

KEYRING_SERVICE = "gmail-cleaner"


@dataclass
class Config:
    email: str = ""
    #: Actions (archive/trash/label/mark-read) are disabled until the user opts in.
    actions_enabled: bool = False
    #: How far back the initial sync reaches, in days.
    sync_days: int = 365
    #: Gmail label applied by the "Review" action.
    review_label: str = "Cleanup/Review"

    @classmethod
    def load(cls) -> "Config":
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text())
            known = {f: data[f] for f in cls.__dataclass_fields__ if f in data}
            return cls(**known)
        return cls()

    def save(self) -> None:
        APP_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2))
        CONFIG_PATH.chmod(0o600)


def _fallback_path() -> Path:
    return APP_DIR / "app_password"


def set_password(email: str, password: str) -> str:
    """Store the app password. Returns where it went, for display."""
    APP_DIR.mkdir(parents=True, exist_ok=True)
    try:
        import keyring

        keyring.set_password(KEYRING_SERVICE, email, password)
        return "system keychain"
    except Exception:
        p = _fallback_path()
        p.write_text(password)
        p.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return str(p)


def get_password(email: str) -> str | None:
    try:
        import keyring

        pw = keyring.get_password(KEYRING_SERVICE, email)
        if pw:
            return pw
    except Exception:
        pass
    p = _fallback_path()
    if p.exists():
        return p.read_text().strip()
    return None


def clear_password(email: str) -> None:
    try:
        import keyring

        keyring.delete_password(KEYRING_SERVICE, email)
    except Exception:
        pass
    p = _fallback_path()
    if p.exists():
        p.unlink()
