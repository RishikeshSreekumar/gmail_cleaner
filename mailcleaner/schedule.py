"""Background sync on a timer, using the OS's own scheduler.

    mclean schedule install --every 6 [--apply-rules] [--notify]
    mclean schedule status
    mclean schedule remove

macOS gets a launchd agent, Linux a systemd user timer (or a crontab line to
paste when systemd is not there), Windows a Task Scheduler entry. Each run is
`mclean background-run`: sync every account, apply *auto* policies on accounts
that have actions enabled (only with --apply-rules), write a digest next to
each index, and optionally raise a desktop notification.

Nothing is installed unless the user runs `schedule install`, and `schedule
remove` takes it all out again.
"""

from __future__ import annotations

import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import accounts as acct_mod
from . import db, policies, stats, sync
from .accounts import APP_DIR, Store

LABEL = "work.mailcleaner.sync"
LOG = APP_DIR / "background.log"


def mclean_command() -> list[str]:
    """How to invoke this same mclean from a scheduler with no PATH."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    found = shutil.which("mclean")
    if found:
        return [str(Path(found).resolve())]
    return [sys.executable, "-m", "mailcleaner.cli"]


def job_args(apply_rules: bool, notify: bool) -> list[str]:
    args = mclean_command() + ["background-run"]
    if apply_rules:
        args.append("--apply-rules")
    if notify:
        args.append("--notify")
    return args


# -- per-OS definitions -----------------------------------------------------


def launchd_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def launchd_plist(args: list[str], every_hours: int) -> str:
    items = "\n".join(f"    <string>{_xml(a)}</string>" for a in args)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
{items}
  </array>
  <key>StartInterval</key>
  <integer>{every_hours * 3600}</integer>
  <key>RunAtLoad</key>
  <false/>
  <key>ProcessType</key>
  <string>Background</string>
  <key>StandardOutPath</key>
  <string>{_xml(str(LOG))}</string>
  <key>StandardErrorPath</key>
  <string>{_xml(str(LOG))}</string>
</dict>
</plist>
"""


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def systemd_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"


def systemd_units(args: list[str], every_hours: int) -> dict[str, str]:
    cmd = " ".join(shlex.quote(a) for a in args)
    return {
        "mailcleaner-sync.service": (
            "[Unit]\nDescription=mailcleaner background sync\n\n"
            f"[Service]\nType=oneshot\nExecStart={cmd}\n"
        ),
        "mailcleaner-sync.timer": (
            "[Unit]\nDescription=mailcleaner background sync timer\n\n"
            f"[Timer]\nOnBootSec=10min\nOnUnitActiveSec={every_hours}h\n"
            "Persistent=true\n\n[Install]\nWantedBy=timers.target\n"
        ),
    }


def cron_line(args: list[str], every_hours: int) -> str:
    cmd = " ".join(shlex.quote(a) for a in args)
    return f"0 */{every_hours} * * * {cmd} >> {shlex.quote(str(LOG))} 2>&1"


def schtasks_create(args: list[str], every_hours: int) -> list[str]:
    tr = subprocess.list2cmdline(args)
    return ["schtasks", "/Create", "/SC", "HOURLY", "/MO", str(every_hours),
            "/TN", "mailcleaner-sync", "/TR", tr, "/F"]


# -- install / remove / status ---------------------------------------------


def install(every_hours: int = 6, apply_rules: bool = False, notify: bool = True,
            run=subprocess.run, system: str | None = None) -> str:
    """Install the job for this OS. Returns a description of what was done."""
    if not 1 <= every_hours <= 168:
        raise ValueError("--every must be between 1 and 168 hours")
    system = system or platform.system()
    args = job_args(apply_rules, notify)
    APP_DIR.mkdir(parents=True, exist_ok=True)

    if system == "Darwin":
        path = launchd_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        uid = os.getuid()
        if path.exists():
            run(["launchctl", "bootout", f"gui/{uid}", str(path)], check=False,
                capture_output=True)
        path.write_text(launchd_plist(args, every_hours))
        run(["launchctl", "bootstrap", f"gui/{uid}", str(path)], check=False,
            capture_output=True)
        return f"launchd agent {path} (every {every_hours}h, log {LOG})"

    if system == "Linux":
        if shutil.which("systemctl"):
            d = systemd_dir()
            d.mkdir(parents=True, exist_ok=True)
            for name, text in systemd_units(args, every_hours).items():
                (d / name).write_text(text)
            run(["systemctl", "--user", "daemon-reload"], check=False)
            run(["systemctl", "--user", "enable", "--now", "mailcleaner-sync.timer"],
                check=False)
            return f"systemd user timer in {d} (every {every_hours}h)"
        return ("No systemd here. Add this line with `crontab -e`:\n  "
                + cron_line(args, every_hours))

    if system == "Windows":
        run(schtasks_create(args, every_hours), check=False)
        return f"Task Scheduler task mailcleaner-sync (every {every_hours}h)"

    raise RuntimeError(f"Do not know how to schedule jobs on {system}")


def remove(run=subprocess.run, system: str | None = None) -> str:
    system = system or platform.system()
    if system == "Darwin":
        path = launchd_path()
        if not path.exists():
            return "No launchd agent installed."
        run(["launchctl", "bootout", f"gui/{os.getuid()}", str(path)], check=False,
            capture_output=True)
        path.unlink()
        return f"Removed {path}"
    if system == "Linux":
        d = systemd_dir()
        if not (d / "mailcleaner-sync.timer").exists():
            return "No systemd timer installed (remove any crontab line by hand)."
        run(["systemctl", "--user", "disable", "--now", "mailcleaner-sync.timer"],
            check=False)
        for name in ("mailcleaner-sync.timer", "mailcleaner-sync.service"):
            (d / name).unlink(missing_ok=True)
        run(["systemctl", "--user", "daemon-reload"], check=False)
        return f"Removed the timer from {d}"
    if system == "Windows":
        run(["schtasks", "/Delete", "/TN", "mailcleaner-sync", "/F"], check=False)
        return "Removed Task Scheduler task mailcleaner-sync"
    return "Nothing to remove."


def status(system: str | None = None) -> dict:
    system = system or platform.system()
    if system == "Darwin":
        installed, where = launchd_path().exists(), str(launchd_path())
    elif system == "Linux":
        p = systemd_dir() / "mailcleaner-sync.timer"
        installed, where = p.exists(), str(p)
    else:
        installed, where = None, "Task Scheduler (check with: schtasks /Query /TN mailcleaner-sync)"
    last = None
    if LOG.exists():
        lines = LOG.read_text(errors="replace").strip().splitlines()
        last = lines[-1] if lines else None
    return {"installed": installed, "where": where, "log": str(LOG), "last_line": last}


# -- the job itself ---------------------------------------------------------


def background_run(apply_rules: bool = False, notify: bool = False,
                   backend=None, notifier=None) -> list[dict]:
    """Sync every account, optionally apply auto policies, write digests."""
    backend = backend or acct_mod.backend
    out = []
    for account in Store.load().accounts:
        entry = {"account": account.id, "ok": True}
        conn = db.connect(account)
        try:
            with backend(account) as client:
                entry["synced"] = sync.sync(conn, client, account.sync_days)
                if apply_rules and account.actions_enabled:
                    entry["policies"] = [
                        {k: r[k] for k in ("policy", "count", "batch_id")}
                        for r in policies.apply(conn, client, only_auto=True)
                    ]
            d = stats.digest(conn, 7)
            entry["digest"] = d
            (account.dir / "digest.json").write_text(json.dumps(d, default=str, indent=2))
        except Exception as e:  # one broken mailbox must not stop the others
            entry.update(ok=False, error=str(e))
        finally:
            conn.close()
        out.append(entry)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"{stamp} {account.id}: " + (
            f"synced {entry.get('synced', 0)}" if entry["ok"] else f"failed: {entry['error']}"),
            flush=True)
    if notify:
        _notify_summary(out, notifier or desktop_notify)
    return out


def _notify_summary(results: list[dict], notifier) -> None:
    lines = []
    for r in results:
        if not r["ok"]:
            lines.append(f"{r['account']}: sync failed")
            continue
        d = r["digest"]
        want = d["action_required"] + d["potentially_important"]
        bits = []
        if want:
            bits.append(f"{want} need attention")
        if d["alerts"]:
            bits.append(f"{len(d['alerts'])} senders got louder")
        if d["pending_suggestions"]:
            bits.append(f"{d['pending_suggestions']} rule suggestions")
        if bits:
            lines.append(f"{r['account']}: " + ", ".join(bits))
    if lines:
        notifier("mailcleaner", "\n".join(lines[:4]))


def desktop_notify(title: str, body: str) -> None:
    """Best effort; a missing notifier is not an error."""
    system = platform.system()
    try:
        if system == "Darwin":
            script = (f"display notification {json.dumps(body)} "
                      f"with title {json.dumps(title)}")
            subprocess.run(["osascript", "-e", script], check=False, capture_output=True)
        elif system == "Linux" and shutil.which("notify-send"):
            subprocess.run(["notify-send", title, body], check=False)
    except OSError:
        pass
