"""Backups of the enrolled partitions and the tool's state, and going back to one (DESIGN.md R13).

A backup is one zip: a member per file, the tool's state, and a manifest written last that
names every member with its size, time and SHA-256. Nothing is ever extracted by name from
the archive onto the disk: a restore writes only names this tool itself would write, into
partitions that are enrolled.
"""
import contextlib
import hashlib
import json
import os
import re
import stat
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from session_sync.atomic import TEMP_PREFIX, TEMP_SUFFIX, discard, write_atomic
from session_sync.scanner import RECORD_NAME, TMP_NAME, TOMBSTONE_NAME

FORMAT = 1
KEEP = 10
READ_ATTEMPTS = 3
MANIFEST = "manifest.json"
STATE_MEMBER = "state/state.json"
ARCHIVE_NAME = re.compile(r"^(\d{8}-\d{6}(?:-\d+)?)\.zip$")


class BackupFailed(Exception):
    """The backup could not be taken. Nothing was left behind."""


class BackupUnusable(Exception):
    """The archive cannot be trusted, so nothing is restored from it."""


class RestoreRefused(Exception):
    """The archive is fine, but the restore cannot go ahead: it does not fit what is enrolled
    now, or the present could not be saved whole."""


class RestoreIncomplete(Exception):
    """A write or removal failed part way. What was restored before it stays, the sync history
    was not touched, and running the same restore again finishes the rest."""


@dataclass(frozen=True)
class BackupInfo:
    id: str
    path: Path
    created_ms: int = 0
    reason: str = ""
    note: str = ""
    size_bytes: int = 0
    counts: Tuple[Tuple[str, int, int], ...] = ()  # (partition, records, delete markers)
    usable: bool = True
    unreadable: int = 0  # files left out because they could not be read; a restore leaves those alone
    pruned: Tuple[str, ...] = field(default=(), compare=False)  # older backups this one pushed out


@dataclass(frozen=True)
class RestorePlan:
    backup: BackupInfo
    writes: Dict[str, List[str]]
    removals: Dict[str, List[str]]
    unchanged: Dict[str, int]
    restores_state: bool


# -- taking ------------------------------------------------------------------

def take(partitions: List[Path], state_path: Path, backups_dir: Path, reason: str, note: str = "",
         now_ns: Callable[[], int] = time.time_ns, protected: Iterable[Path] = ()) -> BackupInfo:
    """Only reads the partitions, so it is safe beside the running app. Retention spares the
    archive just taken and any in `protected`, such as the one a restore is about to read."""
    backups_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    backups_dir.mkdir(exist_ok=True, mode=0o700)
    created_ns = now_ns()
    destination = _unused_archive(backups_dir, created_ns)
    temporary = backups_dir / ("%s%d-%s%s" % (TEMP_PREFIX, os.getpid(), destination.name, TEMP_SUFFIX))
    manifest = {"format": FORMAT, "created_ms": created_ns // 1_000_000, "reason": reason, "note": note,
                "partitions": [], "state": None}
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            with zipfile.ZipFile(handle, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                for index, partition in enumerate(partitions):
                    files, unreadable = _add_partition(archive, index, partition)
                    manifest["partitions"].append({"path": str(partition), "files": files, "unreadable": unreadable})
                state = _read_stable(state_path)
                if state is not None:
                    archive.writestr(STATE_MEMBER, state[0])
                    manifest["state"] = _described(state[0], state[1])
                archive.writestr(MANIFEST, json.dumps(manifest, indent=1, sort_keys=True))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    except OSError as error:
        discard(temporary)
        raise BackupFailed("The backup could not be written (%s: %s)." % (type(error).__name__, error)) from error
    except BaseException:
        discard(temporary)
        raise
    pruned = prune(backups_dir, never=(destination, *protected))
    return _info(destination, manifest, pruned=tuple(path.name for path in pruned))


def _add_partition(archive: zipfile.ZipFile, index: int, partition: Path) -> Tuple[Dict[str, dict], List[str]]:
    files, unreadable = {}, []
    for name in sorted(os.listdir(partition)):
        if not _is_ours(name):
            continue
        try:
            read = _read_stable(partition / name)
        except OSError:
            # Unreadable is not absent (R11): it is named, so a restore never takes it for an extra file.
            unreadable.append(name)
            continue
        if read is None:  # the app removed it, or it is not a regular file
            continue
        archive.writestr("partitions/%d/%s" % (index, name), read[0])
        files[name] = _described(read[0], read[1])
    return files, unreadable


def _read_stable(path: Path) -> Optional[Tuple[bytes, int]]:
    """The bytes and the file's time, or None when there is no regular file to read. The app may
    save while this reads, and one of its save paths writes in place, so a read only counts when
    the file looked the same before and after."""
    for _ in range(READ_ATTEMPTS):
        try:
            if not stat.S_ISREG(os.lstat(path).st_mode):
                return None
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor, "rb") as handle:
            before = _stamp(handle.fileno())
            data = handle.read()
            after = _stamp(handle.fileno())
        if before == after and len(data) == after[1]:
            return data, after[0]
    raise BackupFailed("%s kept changing while it was read. Nothing was saved. Try again." % path.name)


def _stamp(descriptor: int) -> Tuple[int, int]:
    seen = os.fstat(descriptor)
    return seen.st_mtime_ns, seen.st_size


def _described(data: bytes, mtime_ns: int) -> dict:
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mtime_ns": mtime_ns}


def _is_ours(name: str) -> bool:
    return bool(RECORD_NAME.match(name) or TMP_NAME.match(name) or TOMBSTONE_NAME.match(name))


def _unused_archive(backups_dir: Path, created_ns: int) -> Path:
    label = time.strftime("%Y%m%d-%H%M%S", time.localtime(created_ns // 1_000_000_000))
    candidate, attempt = backups_dir / (label + ".zip"), 1
    while os.path.lexists(candidate):
        attempt += 1
        candidate = backups_dir / ("%s-%d.zip" % (label, attempt))
    return candidate


# -- listing and pruning -----------------------------------------------------

def listing(backups_dir: Path) -> List[BackupInfo]:
    """Newest first. An archive that cannot be read is listed as unusable and stops nothing."""
    try:
        names = [name for name in os.listdir(backups_dir) if ARCHIVE_NAME.match(name)]
    except FileNotFoundError:
        return []
    found = []
    for name in sorted(names, reverse=True):
        path = backups_dir / name
        try:
            found.append(_info(path, _manifest_of(path)))
        except BackupUnusable as unusable:
            found.append(BackupInfo(id=_id_of(path), path=path, reason=str(unusable), usable=False,
                                    size_bytes=_size_of(path)))
    return found


def find(backups_dir: Path, backup_id: str) -> BackupInfo:
    for entry in listing(backups_dir):
        if entry.id == backup_id:
            return entry
    raise RestoreRefused("There is no backup called %s. List them with --backups." % backup_id)


def prune(backups_dir: Path, never: Iterable[Path]) -> List[Path]:
    """Keeps the newest KEEP archives and every path in `never`. Best effort: an archive that
    cannot be removed stays and is not counted."""
    spared, removed = set(never), []
    for entry in listing(backups_dir)[KEEP:]:
        if entry.path in spared:
            continue
        try:
            os.unlink(entry.path)
        except OSError:
            continue
        removed.append(entry.path)
    return removed


def _manifest_of(path: Path) -> dict:
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = json.loads(archive.read(MANIFEST))
    except (zipfile.BadZipFile, KeyError, ValueError, OSError) as error:
        raise BackupUnusable("cannot be read (%s)" % type(error).__name__) from error
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT:
        raise BackupUnusable("written in a format this version does not read")
    return manifest


def _info(path: Path, manifest: dict, pruned: Tuple[str, ...] = ()) -> BackupInfo:
    try:
        counts = tuple((str(entry["path"]),
                        sum(1 for name in entry["files"] if RECORD_NAME.match(name)),
                        sum(1 for name in entry["files"] if TOMBSTONE_NAME.match(name)))
                       for entry in manifest["partitions"])
        return BackupInfo(id=_id_of(path), path=path, created_ms=int(manifest["created_ms"]),
                          reason=str(manifest["reason"]), note=str(manifest["note"]), size_bytes=_size_of(path),
                          counts=counts, pruned=pruned,
                          unreadable=sum(len(entry["unreadable"]) for entry in manifest["partitions"]))
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise BackupUnusable("its manifest is damaged (%s)" % type(error).__name__) from error


def _id_of(path: Path) -> str:
    return path.name[:-len(".zip")]


def _size_of(path: Path) -> int:
    try:
        return os.lstat(path).st_size
    except OSError:
        return 0


# -- restoring ---------------------------------------------------------------

def plan_restore(backup_path: Path, partitions: List[Path]) -> RestorePlan:
    """Checks the whole archive, then compares it with the present. Writes nothing."""
    manifest = _manifest_of(backup_path)
    info = _info(backup_path, manifest)
    _same_partitions_or_refuse(info, partitions)
    writes, removals, unchanged = {}, {}, {}
    with zipfile.ZipFile(backup_path) as archive:
        for index, entry in enumerate(manifest["partitions"]):
            partition, files = Path(entry["path"]), entry["files"]
            for name, described in files.items():
                _verified(archive, "partitions/%d/%s" % (index, _ours_or_unusable(name)), described)
            present = _present_or_refuse(partition)
            differing = sorted(name for name, described in files.items()
                               if present.get(name) != described["sha256"])
            writes[str(partition)] = differing
            removals[str(partition)] = sorted(name for name in present
                                              if name not in files and name not in entry["unreadable"])
            unchanged[str(partition)] = len(files) - len(differing)
        if manifest["state"] is not None:
            _verified(archive, STATE_MEMBER, manifest["state"])
    return RestorePlan(backup=info, writes=writes, removals=removals, unchanged=unchanged,
                       restores_state=manifest["state"] is not None)


def apply_restore(plan: RestorePlan, state_path: Path, now_ns: Callable[[], int] = time.time_ns) -> None:
    """Makes every partition equal to the backup and puts the tool's state back. The caller has
    made sure the app is not running and has saved the present. A file that cannot be written
    or removed stops it there, before the state is touched: the same restore run again finishes
    the rest."""
    manifest = _manifest_of(plan.backup.path)
    with zipfile.ZipFile(plan.backup.path) as archive:
        indexes = {entry["path"]: index for index, entry in enumerate(manifest["partitions"])}
        planned = [(partition, name, "partitions/%d/%s" % (indexes[partition], _ours_or_unusable(name)),
                    manifest["partitions"][indexes[partition]]["files"][name])
                   for partition, names in plan.writes.items() for name in names]
        for _, _, member, described in planned:  # every member once more, before the first write
            _verified(archive, member, described)
        for partition, name, member, described in planned:
            path = Path(partition) / name
            with _or_stopped("write", path, plan.backup.id):
                write_atomic(path, _verified(archive, member, described), described["mtime_ns"])
        for partition, names in plan.removals.items():
            for name in names:
                path = Path(partition) / _ours_or_unusable(name)
                with _or_stopped("remove", path, plan.backup.id):
                    _unlink_if_there(path)
        if manifest["state"] is not None:
            with _or_stopped("write", state_path, plan.backup.id):
                write_atomic(state_path, _verified(archive, STATE_MEMBER, manifest["state"]))
        elif state_path.exists():
            # The backup predates any sync history, so the later history goes aside, as --reset-state does.
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now_ns() // 1_000_000_000))
            with _or_stopped("set aside", state_path, plan.backup.id):
                state_path.rename(state_path.with_name("%s.before-restore-%s" % (state_path.name, stamp)))


@contextlib.contextmanager
def _or_stopped(step: str, path: Path, backup_id: str):
    try:
        yield
    except OSError as error:
        raise RestoreIncomplete(
            "The restore stopped: could not %s %s (%s: %s). What was restored before it stays and the sync "
            "history was not touched. Fix access and run --restore %s --apply again; it finishes the rest."
            % (step, path, type(error).__name__, error, backup_id)) from error


def _unlink_if_there(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass  # a restore stopped part way is run again


def _same_partitions_or_refuse(info: BackupInfo, partitions: List[Path]) -> None:
    saved, enrolled = {path for path, _, _ in info.counts}, {str(path) for path in partitions}
    if saved == enrolled:
        return
    lines = ["Backup %s does not cover the partitions enrolled now, and a restore rolls all of them back "
             "together. Nothing was changed." % info.id]
    lines += ["  in the backup, not enrolled now: %s" % path for path in sorted(saved - enrolled)]
    lines += ["  enrolled now, not in the backup: %s" % path for path in sorted(enrolled - saved)]
    raise RestoreRefused("\n".join(lines))


def _ours_or_unusable(name: str) -> str:
    if not _is_ours(name):
        raise BackupUnusable("it names a file this tool would never write: %r" % name)
    return name


def _verified(archive: zipfile.ZipFile, member: str, described: dict) -> bytes:
    try:
        data = archive.read(member)
    except (KeyError, zipfile.BadZipFile, OSError) as error:
        raise BackupUnusable("%s is missing or damaged (%s)" % (member, type(error).__name__)) from error
    if len(data) != described["size"] or hashlib.sha256(data).hexdigest() != described["sha256"]:
        raise BackupUnusable("%s does not match its checksum" % member)
    return data


def _present_or_refuse(partition: Path) -> Dict[str, Optional[str]]:
    """What the partition holds now, by checksum. A file that cannot be read stops the plan: the
    backup a restore takes of the present would not hold it, and it would be written over or
    removed with no copy kept."""
    digests, unreadable = {}, []
    try:
        names = sorted(name for name in os.listdir(partition) if _is_ours(name))
    except OSError as error:
        raise RestoreRefused("%s cannot be read (%s). Nothing was changed." % (partition, error)) from error
    for name in names:
        try:
            digests[name] = _sha256_now(partition / name)
        except OSError:
            unreadable.append(name)
    if unreadable:
        raise RestoreRefused(
            "%d files in %s cannot be read: %s. The backup a restore takes of the present would not hold "
            "them, so nothing is restored until they can be read. Nothing was changed."
            % (len(unreadable), partition, ", ".join(unreadable)))
    return digests


def _sha256_now(path: Path) -> Optional[str]:
    """None when there is no regular file there. A file that cannot be read raises."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None
