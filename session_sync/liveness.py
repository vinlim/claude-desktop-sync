"""Whether the running app may hold a partition in memory (DESIGN.md R9)."""
import json
import subprocess
from pathlib import Path
from typing import Optional

# The main binary only. Helper processes live under Contents/Frameworks.
APP_PROCESS = r"Claude\.app/Contents/MacOS/Claude( |$)"
PGREP_TIMEOUT_S = 5


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


def is_live(partition: Path, running: bool) -> bool:
    """A partition is <root>/claude-code-sessions/<account>/<org>."""
    if not running:
        return False
    account = last_known_account(partition.parents[2])
    return account is None or account == partition.parent.name.lower()
