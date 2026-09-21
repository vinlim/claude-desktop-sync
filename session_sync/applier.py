"""Carries out a plan on disk. Every change to an existing file passes the R9 guards
at the moment it happens, and nothing unique is removed without a kept copy (R8)."""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from session_sync.atomic import create_exclusive, write_atomic
from session_sync.fingerprint import fingerprint
from session_sync.model import (Action, CreateRecord, CreateTombstone, Plan, ReplaceRecord, RetireRecord, RetireTmp,
                                RetireTombstone)
from session_sync.scanner import PartitionScan, Stamp, record_path, stamp_of, tmp_path, tombstone_path


@dataclass(frozen=True)
class Outcome:
    action: Action
    problem: Optional[str]  # None: done


class Refused(Exception):
    """A guard said no. The message is what the user reads."""


class Applier:
    def __init__(self, scans: Dict[str, PartitionScan], is_live: Callable[[Path], bool], kept_dir: Path) -> None:
        self.scans = scans
        self.is_live = is_live
        self.kept_dir = kept_dir
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
            problem = self._attempt(action)
            if problem:
                stopped.add(action.session_id)
            outcomes.append(Outcome(action, problem))
        return outcomes

    def _attempt(self, action: Action) -> Optional[str]:
        try:
            self.handlers[type(action)](action)
        except Refused as refusal:
            return str(refusal)
        except OSError as error:
            return "%s: %s" % (type(error).__name__, error)
        return None

    # -- records ---------------------------------------------------------------

    def _create_record(self, action: CreateRecord) -> None:
        data, mtime_ns = self._read_planned_record(action.source, action.session_id)
        try:
            create_exclusive(record_path(Path(action.target), action.session_id), data, mtime_ns)
        except FileExistsError:
            raise Refused("a file appeared at the target since the scan")

    def _replace_record(self, action: ReplaceRecord) -> None:
        data, mtime_ns = self._read_planned_record(action.source, action.session_id)
        partition = Path(action.target)
        path = record_path(partition, action.session_id)
        scanned = self.scans[action.target].records.get(action.session_id)
        self._guard_existing(partition, path, scanned)
        if action.keep:
            self._keep(partition, path)
            self._guard_existing(partition, path, scanned)
        write_atomic(path, data, mtime_ns)

    def _retire_record(self, action: RetireRecord) -> None:
        partition = Path(action.target)
        scan = self.scans[action.target]
        sibling = tmp_path(partition, action.session_id)
        scanned_sibling = scan.tmps.get(action.session_id)
        if scanned_sibling is None and os.path.lexists(sibling):
            raise Refused("a temp file appeared since the scan")
        # The temp file goes first: left behind alone, the app would promote it to a live record.
        if scanned_sibling is not None:
            self._retire(partition, sibling, scanned_sibling)
        self._retire(partition, record_path(partition, action.session_id), scan.records.get(action.session_id))

    def _retire_tmp(self, action: RetireTmp) -> None:
        partition = Path(action.target)
        self._retire(partition, tmp_path(partition, action.session_id),
                     self.scans[action.target].tmps.get(action.session_id))

    # -- tombstones ------------------------------------------------------------

    def _create_tombstone(self, action: CreateTombstone) -> None:
        source = tombstone_path(Path(action.source), action.session_id)
        stamp = stamp_of(source)
        if stamp is None:
            raise Refused("the source tombstone is gone")
        try:
            create_exclusive(tombstone_path(Path(action.target), action.session_id), source.read_bytes(), stamp[0])
        except FileExistsError:
            pass  # the app, or an earlier run, already put one there

    def _retire_tombstone(self, action: RetireTombstone) -> None:
        partition = Path(action.target)
        self._retire(partition, tombstone_path(partition, action.session_id),
                     self.scans[action.target].tombstones.get(action.session_id))

    # -- shared steps ----------------------------------------------------------

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

    def _keep(self, partition: Path, path: Path) -> None:
        stamp = stamp_of(path)
        if stamp is None:
            raise Refused("target changed since the scan")
        folder = self.kept_dir / ("%s_%s" % (partition.parent.name, partition.name))
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        write_atomic(folder / path.name, path.read_bytes(), stamp[0])

    def _retire(self, partition: Path, path: Path, scanned: Optional[Stamp]) -> None:
        self._guard_existing(partition, path, scanned)
        self._keep(partition, path)
        self._guard_existing(partition, path, scanned)
        os.unlink(path)
