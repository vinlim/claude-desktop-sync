"""Which partitions the tool may touch (DESIGN.md R1). Nothing is synced by discovery."""
import json
import os
import re
import stat
from pathlib import Path
from typing import Dict, List, Sequence

from session_sync.atomic import write_atomic
from session_sync.scanner import RECORD_NAME

SESSIONS_DIR = "claude-code-sessions"
UUID = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


class EnrolmentError(Exception):
    pass


def validate_partition(path: Path) -> None:
    """<root>/claude-code-sessions/<uuid>/<uuid>, every component below the root a real directory."""
    if not path.is_absolute() or len(path.parts) < 5 or path.parent.parent.name != SESSIONS_DIR:
        raise EnrolmentError("%s is not shaped <app data>/%s/<account>/<org>" % (path, SESSIONS_DIR))
    if not UUID.match(path.name) or not UUID.match(path.parent.name):
        raise EnrolmentError("%s: the account and org folders must be UUIDs" % path)
    for component in (path.parent.parent, path.parent, path):
        try:
            mode = os.lstat(component).st_mode
        except OSError:
            raise EnrolmentError("%s does not exist" % component)
        if not stat.S_ISDIR(mode):
            raise EnrolmentError("%s is not a real directory (the app refuses symlinks here too)" % component)


def load_enrolled(config: Path) -> List[Path]:
    try:
        text = config.read_text()
    except FileNotFoundError:
        return []
    try:
        entries = json.loads(text)["partitions"]
        if not isinstance(entries, list) or not all(isinstance(entry, str) for entry in entries):
            raise ValueError("partitions must be a list of paths")
    except (ValueError, KeyError, TypeError) as error:
        raise EnrolmentError("%s cannot be read (%s). Fix it or remove it, then enrol the partitions again."
                             % (config, error))
    return [Path(entry) for entry in entries]


def enrol(config: Path, partition: Path) -> List[Path]:
    partition = Path(os.path.abspath(os.path.expanduser(str(partition))))
    validate_partition(partition)
    enrolled = load_enrolled(config)
    if partition not in enrolled:
        enrolled.append(partition)
    _save(config, enrolled)
    return enrolled


def unenrol(config: Path, partition: Path) -> List[Path]:
    partition = Path(os.path.abspath(os.path.expanduser(str(partition))))
    enrolled = [p for p in load_enrolled(config) if p != partition]
    _save(config, enrolled)
    return enrolled


def unenrolled_with_records(enrolled: List[Path], also_under: Sequence[Path] = ()) -> Dict[Path, int]:
    """Partitions that hold records but were never enrolled, with their record counts.

    Looks beside the enrolled partitions, and under any extra sessions directory given.
    """
    found: Dict[Path, int] = {}
    for base in sorted({partition.parent.parent for partition in enrolled} | set(also_under)):
        for account in _real_uuid_dirs(base):
            for candidate in _real_uuid_dirs(account):
                if candidate in enrolled:
                    continue
                try:
                    count = sum(1 for name in os.listdir(candidate) if RECORD_NAME.match(name))
                except OSError:
                    continue
                if count:
                    found[candidate] = count
    return found


def _real_uuid_dirs(parent: Path) -> List[Path]:
    try:
        names = sorted(os.listdir(parent))
    except OSError:
        return []
    return [parent / name for name in names
            if UUID.match(name) and stat.S_ISDIR(os.lstat(parent / name).st_mode)]


def _save(config: Path, enrolled: List[Path]) -> None:
    config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_atomic(config, json.dumps({"partitions": [str(p) for p in enrolled]}, indent=1).encode("utf-8"))
