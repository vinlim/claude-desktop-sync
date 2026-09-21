"""Carries out a plan on disk. Every change to an existing file passes the R9 guards
at the moment it happens, and nothing unique is removed without a kept copy (R8)."""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from session_sync.atomic import commit_create, commit_replace, create_exclusive, discard, stage, write_atomic
from session_sync.fingerprint import fingerprint
from session_sync.model import (Action, CreateRecord, CreateTombstone, Plan, ReplaceRecord, RetireRecord, RetireTmp,
                                RetireTombstone)
from session_sync.scanner import PartitionScan, Stamp, record_path, stamp_of, tmp_path, tombstone_path


@dataclass(frozen=True)
class Outcome:
    action: Action
    problem: Optional[str]  # None: done
    kept: Optional[Path] = None  # where the replaced or retired file went


class Refused(Exception):
    """A guard said no. The message is what the user reads."""


class Applier:
    def __init__(self, scans: Dict[str, PartitionScan], is_live: Callable[[Path], bool], kept_dir: Path,
                 before_create: Callable[[str, str], None] = lambda partition, session_id: None) -> None:
        self.scans = scans
        self.is_live = is_live
        self.kept_dir = kept_dir
        self.before_create = before_create  # told (partition, session id) just before a record is created
        self.handlers = {
            CreateRecord: self._create_record,
            ReplaceRecord: self._replace_record,
            RetireRecord: self._retire_record,
            RetireTmp: self._retire_tmp,
            CreateTombstone: self._create_tombstone,
            RetireTombstone: self._retire_tombstone,
        }

    def apply(self, plan: Plan) -> List[Outcome]:
        """The planner orders a session's actions; a failed step stops the rest of that session."""
        outcomes = []
        stopped = set()
        for action in plan.actions:
            if action.session_id in stopped:
                outcomes.append(Outcome(action, "an earlier step for this session did not complete"))
                continue
            outcome = self._attempt(action)
            if outcome.problem:
                stopped.add(action.session_id)
            outcomes.append(outcome)
        return outcomes

    def _attempt(self, action: Action) -> Outcome:
        try:
            return Outcome(action, None, kept=self.handlers[type(action)](action))
        except Refused as refusal:
            return Outcome(action, str(refusal))
        except OSError as error:
            return Outcome(action, "%s: %s" % (type(error).__name__, error))

    # -- records ---------------------------------------------------------------

    def _create_record(self, action: CreateRecord) -> None:
        data, mtime_ns = self._read_planned_record(action.source, action.session_id)
        target = record_path(Path(action.target), action.session_id)
        if os.path.lexists(target):
            raise Refused("a file appeared at the target since the scan")
        self.before_create(action.target, action.session_id)
        self._create(target, data, mtime_ns, "a file appeared at the target since the scan")

    def _replace_record(self, action: ReplaceRecord) -> Optional[Path]:
        data, mtime_ns = self._read_planned_record(action.source, action.session_id)
        partition = Path(action.target)
        path = record_path(partition, action.session_id)
        scanned = self.scans[action.target].records.get(action.session_id)
        self._guard_existing(partition, path, scanned)  # before anything is written: a refusal leaves no trace
        kept = self._keep(partition, path) if action.keep else None
        staged = stage(path, data, mtime_ns)
        try:
            self._guard_existing(partition, path, scanned)  # after the slow write, right before the rename
        except BaseException:  # a refusal, or being stopped while the guard probes for the app
            discard(staged)
            raise
        commit_replace(staged, path)
        return kept

    def _retire_record(self, action: RetireRecord) -> Optional[Path]:
        partition = Path(action.target)
        scan = self.scans[action.target]
        sibling = tmp_path(partition, action.session_id)
        scanned_sibling = scan.tmps.get(action.session_id)
        if scanned_sibling is None and os.path.lexists(sibling):
            raise Refused("a temp file appeared since the scan")
        # The temp file goes first: left behind alone, the app would promote it to a live record.
        if scanned_sibling is not None:
            self._retire(partition, sibling, scanned_sibling)
        return self._retire(partition, record_path(partition, action.session_id),
                            scan.records.get(action.session_id))

    def _retire_tmp(self, action: RetireTmp) -> Optional[Path]:
        partition = Path(action.target)
        return self._retire(partition, tmp_path(partition, action.session_id),
                            self.scans[action.target].tmps.get(action.session_id))

    # -- tombstones ------------------------------------------------------------

    def _create_tombstone(self, action: CreateTombstone) -> None:
        source = tombstone_path(Path(action.source), action.session_id)
        stamp = stamp_of(source)
        if stamp is None:
            raise Refused("the source tombstone is gone")
        target = tombstone_path(Path(action.target), action.session_id)
        if os.path.lexists(target):
            return  # the app, or an earlier run, already put one there
        try:
            create_exclusive(target, source.read_bytes(), stamp[0])
        except FileExistsError:
            pass

    def _retire_tombstone(self, action: RetireTombstone) -> Optional[Path]:
        partition = Path(action.target)
        return self._retire(partition, tombstone_path(partition, action.session_id),
                            self.scans[action.target].tombstones.get(action.session_id))

    # -- shared steps ----------------------------------------------------------

    def _create(self, target: Path, data: bytes, mtime_ns: int, refusal: str) -> None:
        """Checks first so a standing refusal writes nothing: a watched directory would refire on it."""
        if os.path.lexists(target):
            raise Refused(refusal)
        staged = stage(target, data, mtime_ns)
        try:
            commit_create(staged, target)
        except FileExistsError:
            raise Refused(refusal)
        except BaseException:  # stopped between staging and linking
            discard(staged)
            raise

    def _read_planned_record(self, source_key: str, session_id: str) -> Tuple[bytes, int]:
        """The bytes of exactly the version the planner chose (R2)."""
        scan = self.scans[source_key]
        scanned = scan.records.get(session_id)
        path = record_path(Path(source_key), session_id)
        if scanned is None or stamp_of(path) != scanned:
            raise Refused("source changed since the scan")
        data = path.read_bytes()
        planned = scan.snapshot.records[session_id]
        if stamp_of(path) != scanned or fingerprint(session_id, data).state_hash != planned.state_hash:
            raise Refused("source changed since the scan")
        return data, scanned[0]

    def _guard_existing(self, partition: Path, path: Path, scanned: Optional[Stamp]) -> None:
        """R9, asked at the moment of the change."""
        if self.is_live(partition):
            raise Refused("the partition is live")
        if scanned is None or stamp_of(path) != scanned:
            raise Refused("target changed since the scan")

    def _keep(self, partition: Path, path: Path) -> Path:
        stamp = stamp_of(path)
        if stamp is None:
            raise Refused("target changed since the scan")
        folder = self.kept_dir / ("%s_%s" % (partition.parent.name, partition.name))
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        kept = _unused_name(folder / path.name)
        write_atomic(kept, path.read_bytes(), stamp[0])
        return kept

    def _retire(self, partition: Path, path: Path, scanned: Optional[Stamp]) -> Path:
        self._guard_existing(partition, path, scanned)
        kept = self._keep(partition, path)
        self._guard_existing(partition, path, scanned)
        os.unlink(path)
        return kept


def _unused_name(path: Path) -> Path:
    """Two runs in one second share a kept folder, and a kept copy must never replace another."""
    candidate, attempt = path, 0
    while os.path.lexists(candidate):
        attempt += 1
        candidate = path.with_name("%s.%d" % (path.name, attempt))
    return candidate
