"""Reads one partition from disk into a snapshot, plus the file stamps the applier guards on."""
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

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


def stamp_of(path: Path) -> Optional[Stamp]:
    """None unless the path is a regular file right now."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return (info.st_mtime_ns, info.st_size)


def scan_partition(path: Path, cache: Dict[str, CacheEntry], now_ns: int) -> PartitionScan:
    copies: Dict[str, Copy] = {}
    deleted_at: Dict[str, int] = {}
    result = PartitionScan(path=path, snapshot=Snapshot(key=str(path), records=copies, tombstones=deleted_at))

    for name in sorted(os.listdir(path)):
        entry = path / name
        stamp = stamp_of(entry)  # the app may rename or remove files under the scan
        if stamp is None:
            continue
        record, tmp, tombstone = RECORD_NAME.match(name), TMP_NAME.match(name), TOMBSTONE_NAME.match(name)
        if record:
            copy = _read_record(entry, record.group(1), stamp, cache, now_ns)
            if copy is not None:
                copies[record.group(1)] = copy
                result.records[record.group(1)] = stamp
                result.cache[record.group(1)] = (stamp[0], stamp[1], copy.state_hash, copy.last_activity_at)
        elif tmp:
            result.tmps[tmp.group(1)] = stamp
        elif tombstone:
            when = _read_delete_time(entry, now_ns // 1_000_000)
            if when is not None:
                deleted_at[tombstone.group(1)] = when
                result.tombstones[tombstone.group(1)] = stamp

    orphans = frozenset(sid for sid in result.tmps if sid not in copies)
    result.snapshot = Snapshot(key=str(path), records=copies, tombstones=deleted_at, orphan_tmps=orphans)
    return result


def _read_record(path: Path, session_id: str, stamp: Stamp, cache: Dict[str, CacheEntry],
                 now_ns: int) -> Optional[Copy]:
    cached = cache.get(session_id)
    settled = now_ns - stamp[0] >= RACY_WINDOW_NS
    if cached is not None and settled and (cached[0], cached[1]) == stamp:
        copy = Copy(state_hash=cached[2], last_activity_at=cached[3])
    else:
        try:
            data = path.read_bytes()
        except OSError:
            return None
        try:
            copy = fingerprint(session_id, data)
        except Exception:  # one odd record must never stop the run: it is unreadable (R11)
            copy = UNREADABLE
    # Activity in the future would outrank every tombstone, and the session could never be deleted.
    return Copy(copy.state_hash, min(copy.last_activity_at, now_ns // 1_000_000))


def _read_delete_time(path: Path, now_ms: int) -> Optional[int]:
    """The tombstone's content, unless it is unusable or in the future; then the file's own time."""
    try:
        claimed = int(path.read_text().strip())
    except (OSError, ValueError):
        claimed = -1
    if 0 <= claimed <= now_ms:
        return claimed
    try:
        return min(os.lstat(path).st_mtime_ns // 1_000_000, now_ms)
    except OSError:
        return None
