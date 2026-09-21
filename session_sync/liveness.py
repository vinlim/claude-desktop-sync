"""Whether the running app may hold a partition in memory (DESIGN.md R9)."""
import json
import os
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# The main binary only. Helper processes live under Contents/Frameworks.
APP_PROCESS = r"Claude\.app/Contents/MacOS/Claude( |$)"
PGREP_TIMEOUT_S = 5

# The app records the new login before it flushes the previous login's pending saves
# (DESIGN.md F9), so for a while after a login change no partition is safe to change.
SWITCH_GRACE_MS = 120_000

Logins = Dict[str, Tuple[str, int]]  # app data root -> (login last seen there, when the tool first saw it)


def app_running(run=subprocess.run) -> bool:
    """pgrep exits 0 on a match and 1 on none. Every other outcome is read as running."""
    try:
        code = run(["pgrep", "-f", APP_PROCESS], capture_output=True, timeout=PGREP_TIMEOUT_S).returncode
    except (OSError, subprocess.SubprocessError):
        return True
    return code != 1


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


def observe_logins(partitions: List[Path], logins: Logins, now_ms: int) -> None:
    """Dates a login the first time it is seen. The app's config file cannot date it: the app
    rewrites that file about once a minute for unrelated reasons."""
    for root in {root_of(partition) for partition in partitions}:
        account = last_known_account(root)
        if account is not None and logins.get(str(root), (None, 0))[0] != account:
            logins[str(root)] = (account, now_ms)


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
