"""Whether the running app may hold a partition in memory (DESIGN.md R9)."""
import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# The main binary only, matched against executable paths from ps. Helper processes live under
# Contents/Frameworks. Not pgrep: it leaves its own ancestors out, so a run started from a
# shell inside the app never saw the app, and that miss read as "not running".
APP_BINARY = re.compile(r"Claude\.app/Contents/MacOS/Claude$")
PS_TIMEOUT_S = 5

# The app writes a line for every login change. It dates a change the tool did not see happen.
APP_LOG = Path.home() / "Library" / "Logs" / "Claude" / "main.log"
APP_LOG_TAIL = 16 * 1024 * 1024  # the app rotates the log at about 10 MB, so this is the whole current file
# A logout line ends in "uuid: X \u2192 <none>", so the uuid alone tells the two apart.
LOGIN_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) .*Login-state transition \(loggedOut: [^,]+, "
                        r"uuid: \S+ \u2192 ([0-9A-Fa-f-]{36})\)")

# The app records the new login before it flushes the previous login's pending saves
# (DESIGN.md F9), so for a while after a login change no partition is safe to change.
SWITCH_GRACE_MS = 120_000

Logins = Dict[str, Tuple[str, int]]  # app data root -> (login last seen there, when it changed, in ms)


def app_running(run=subprocess.run) -> bool:
    """Reads the process list by executable path. A listing that fails is read as running."""
    try:
        listed = run(["ps", "-axo", "comm="], capture_output=True, timeout=PS_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return True
    if listed.returncode != 0:
        return True
    names = listed.stdout.decode("utf-8", errors="replace")  # a process may be named in any bytes
    return any(APP_BINARY.search(line.strip()) for line in names.splitlines())


def last_known_account(root: Path) -> Optional[str]:
    try:
        config = json.loads((root / "config.json").read_text())
    except (OSError, ValueError):
        return None
    value = config.get("lastKnownAccountUuid") if isinstance(config, dict) else None
    return value.lower() if isinstance(value, str) else None


def root_of(partition: Path) -> Path:
    """A partition is <root>/claude-code-sessions/<account>/<org>."""
    return partition.parents[2]


def observe_logins(partitions: List[Path], logins: Logins, now_ms: int, app_log: Optional[Path] = None) -> None:
    """Dates a login change: by the app's own log where its last login line names the login,
    else by the moment the tool first sees the change. A newer login to the same account in
    the log moves the date forward, so a round trip that ended where it began still gets its
    grace. The app's config file cannot date a change, because the app rewrites that file
    about once a minute for unrelated reasons."""
    for root in {root_of(partition) for partition in partitions}:
        account = last_known_account(root)
        if account is None:
            continue
        dated = login_dated_by_app(app_log, account, now_ms) if app_log is not None else None
        known = logins.get(str(root))
        if known is None or known[0] != account:
            logins[str(root)] = (account, now_ms if dated is None else dated)
        elif dated is not None and dated > known[1]:
            logins[str(root)] = (account, dated)


def login_dated_by_app(app_log: Path, account: str, now_ms: int) -> Optional[int]:
    """When the app's log last recorded a login to this account, in ms, or None when the end of
    the log does not say. A time past now is read as now: the log's clock is not this one."""
    try:
        with open(app_log, "rb") as handle:
            handle.seek(max(0, os.fstat(handle.fileno()).st_size - APP_LOG_TAIL))
            tail = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        found = LOGIN_LINE.match(line)
        if found is None:
            continue
        if found.group(2).lower() != account.lower():
            return None
        try:
            stamped = int(time.mktime(time.strptime(found.group(1), "%Y-%m-%d %H:%M:%S"))) * 1000
        except (ValueError, OverflowError):
            return None
        return min(stamped, now_ms)
    return None


def is_live(partition: Path, running: bool, now_ms: int, logins: Logins) -> bool:
    if not running:
        return False
    root = root_of(partition)
    if os.path.lexists(root / "config.json.journal"):
        return True  # a config write is in flight, so the login on disk may be the old one
    account = last_known_account(root)
    if account is None or account == partition.parent.name.lower():
        return True
    seen = logins.get(str(root))
    if seen is None or seen[0] != account:
        return True  # a login the tool has not dated yet: the first run, or a change in mid-run
    return now_ms - seen[1] < SWITCH_GRACE_MS
