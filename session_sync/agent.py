"""The optional launchd agent that syncs whenever an enrolled partition changes (DESIGN.md R12)."""
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import List, Sequence

LABEL = "local.claude-desktop-session-sync"
PLIST = Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")
MINIMUM_PYTHON = "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)"


class AgentError(Exception):
    pass


def default_python_candidates() -> List[str]:
    """launchd has a bare PATH, so the interpreter is an absolute path."""
    return ["/usr/bin/python3", os.path.realpath(sys.executable)]


def choose_python(candidates: Sequence[str], run=subprocess.run) -> str:
    """/usr/bin/python3 can be a stub that only offers to install developer tools, so each
    candidate has to actually run."""
    for candidate in candidates:
        try:
            if run([candidate, "-c", MINIMUM_PYTHON], capture_output=True, timeout=20).returncode == 0:
                return candidate
        except (OSError, subprocess.SubprocessError):
            continue
    raise AgentError("No usable Python 3.9+ interpreter among: %s. Install Python 3 and try again."
                     % ", ".join(candidates))


def agent_definition(python: str, entry_script: Path, partitions: List[Path], log_path: Path) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [python, str(entry_script), "--apply", "--quiet"],
        # launchd watches a directory's own entries, which is what the app's
        # write-then-rename saves change. The interval is the fallback.
        "WatchPaths": [str(partition) for partition in partitions],
        "StartInterval": 300,
        "ThrottleInterval": 10,
        "RunAtLoad": True,
        "ProcessType": "Background",
        "StandardOutPath": str(log_path),
        "StandardErrorPath": str(log_path),
    }


def is_installed(plist: Path = PLIST) -> bool:
    return plist.exists()


def install(entry_script: Path, partitions: List[Path], log_path: Path, plist: Path = PLIST) -> None:
    definition = agent_definition(choose_python(default_python_candidates()), entry_script, partitions, log_path)
    uninstall(plist)
    plist.parent.mkdir(parents=True, exist_ok=True)
    with open(plist, "wb") as handle:
        plistlib.dump(definition, handle)
    loaded = subprocess.run(["launchctl", "bootstrap", "gui/%d" % os.getuid(), str(plist)],
                            capture_output=True, text=True)
    if loaded.returncode:
        raise AgentError("launchctl could not load %s: %s" % (plist, loaded.stderr.strip()))


def uninstall(plist: Path = PLIST) -> bool:
    subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (os.getuid(), LABEL)], capture_output=True)
    if not plist.exists():
        return False
    plist.unlink()
    return True


def cap_log(log_path: Path, max_bytes: int = 1_000_000, keep_bytes: int = 256_000) -> None:
    """Keeps the newest whole lines once the log outgrows the cap."""
    try:
        if log_path.stat().st_size <= max_bytes:
            return
        data = log_path.read_bytes()[-keep_bytes:]
    except OSError:
        return
    first_newline = data.find(b"\n")
    log_path.write_bytes(data[first_newline + 1:] if first_newline >= 0 else b"")
