"""Reads one partition from disk into a snapshot, plus the file stamps the applier guards on."""
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from session_sync.fingerprint import UNREADABLE, fingerprint
from session_sync.model import Copy, Snapshot

# The app's own filters: a name that starts with local_ and ends with .json, or starts with deleted_.
SESSION_ID = r"[A-Za-z0-9_-]+"
RECORD_NAME = re.compile(r"^local_(%s)\.json$" % SESSION_ID)
TMP_NAME = re.compile(r"^local_(%s)\.json\.tmp$" % SESSION_ID)
TOMBSTONE_NAME = re.compile(r"^deleted_(%s)$" % SESSION_ID)

# Two saves inside one timestamp tick can share mtime and size, so a file this
# fresh is never trusted to match its cache entry.
RACY_WINDOW_NS = 2_000_000_000

Stamp = Tuple[int, int]  # (mtime_ns, size)
CacheEntry = Tuple[int, int, Optional[str], int]  # stamp, state hash, last activity


@dataclass
class PartitionScan:
    path: Path
    snapshot: Snapshot
    records: Dict[str, Stamp] = field(default_factory=dict)
    tombstones: Dict[str, Stamp] = field(default_factory=dict)
    tmps: Dict[str, Stamp] = field(default_factory=dict)
    cache: Dict[str, CacheEntry] = field(default_factory=dict)


def record_path(partition: Path, session_id: str) -> Path:
    return partition / ("local_%s.json" % session_id)


def tmp_path(partition: Path, session_id: str) -> Path:
    return partition / ("local_%s.json.tmp" % session_id)


def tombstone_path(partition: Path, session_id: str) -> Path:
    return partition / ("deleted_%s" % session_id)


def inspect(path: Path) -> Optional[Stamp]:
    """None when the path is gone or is not a regular file. Any other failure is raised: a scan
    that could not look at an entry knows nothing about it, and must not read that as absence."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return (info.st_mtime_ns, info.st_size)


def stamp_of(path: Path) -> Optional[Stamp]:
    """For guards and cleanup: what cannot be inspected is simply not what was scanned."""
    try:
        return inspect(path)
    except OSError:
        return None


def scan_partition(path: Path, cache: Dict[str, CacheEntry], clock: Callable[[], int]) -> PartitionScan:
    """clock returns nanoseconds. It is read again after each file is read: the app stamps a
    session in use with the current time every few seconds, and judged against the moment the
    scan began, a save that lands during the scan would look like the future."""
    started_ns = clock()
    copies: Dict[str, Copy] = {}
    deleted_at: Dict[str, int] = {}
    result = PartitionScan(path=path, snapshot=Snapshot(key=str(path), records=copies, tombstones=deleted_at))

    for name in sorted(os.listdir(path)):
        entry = path / name
        stamp = inspect(entry)  # None: the app renamed or removed it under the scan
        if stamp is None:
            continue
        record, tmp, tombstone = RECORD_NAME.match(name), TMP_NAME.match(name), TOMBSTONE_NAME.match(name)
        if record:
            copy = _read_record(entry, record.group(1), stamp, cache, started_ns)
            if copy is not None:
                # A time in the future cannot be ordered against a delete. Read as "now" it would
                # outrank every tombstone. The cache keeps it as written, for when the clock catches up.
                ahead = copy.last_activity_at > clock() // 1_000_000
                copies[record.group(1)] = Copy(copy.state_hash, copy.last_activity_at, future_dated=ahead)
                result.records[record.group(1)] = stamp
                result.cache[record.group(1)] = (stamp[0], stamp[1], copy.state_hash, copy.last_activity_at)
        elif tmp:
            result.tmps[tmp.group(1)] = stamp
        elif tombstone:
            when = _read_delete_time(entry, clock)
            if when is not None:
                deleted_at[tombstone.group(1)] = when
                result.tombstones[tombstone.group(1)] = stamp

    orphans = frozenset(sid for sid in result.tmps if sid not in copies)
    result.snapshot = Snapshot(key=str(path), records=copies, tombstones=deleted_at, orphan_tmps=orphans)
    return result


def _read_record(path: Path, session_id: str, stamp: Stamp, cache: Dict[str, CacheEntry],
                 now_ns: int) -> Optional[Copy]:
    """None means the file is gone. A file that is there but cannot be read is unreadable, never absent."""
    cached = cache.get(session_id)
    settled = now_ns - stamp[0] >= RACY_WINDOW_NS
    # An unreadable verdict is never reused: fixing permissions changes neither time nor size.
    if cached is not None and cached[2] is not None and settled and (cached[0], cached[1]) == stamp:
        return Copy(state_hash=cached[2], last_activity_at=cached[3])
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        return UNREADABLE
    try:
        return fingerprint(session_id, data)
    except Exception:  # one odd record must never stop the run: it is unreadable (R11)
        return UNREADABLE


def _read_delete_time(path: Path, clock: Callable[[], int]) -> Optional[int]:
    """The tombstone's content, unless it is unusable or in the future; then the file's own time."""
    try:
        claimed = int(path.read_text().strip())
    except (OSError, ValueError):
        claimed = -1
    now_ms = clock() // 1_000_000
    if 0 <= claimed <= now_ms:
        return claimed
    written = inspect(path)
    return None if written is None else min(written[0] // 1_000_000, now_ms)
