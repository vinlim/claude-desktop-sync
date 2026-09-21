"""One sync run, in the order that keeps a crash harmless (DESIGN.md R10):
load, scan, plan, journal intents, write, rescan, settle, save."""
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from session_sync.applier import Applier, Outcome
from session_sync.atomic import TEMP_PREFIX, TEMP_SUFFIX
from session_sync.enrolment import EnrolmentError, load_enrolled, unenrolled_with_records, validate_partition
from session_sync.liveness import app_running, is_live
from session_sync.model import Action, CreateRecord, Plan, Problem
from session_sync.planner import plan
from session_sync.scanner import PartitionScan, scan_partition, stamp_of
from session_sync.settle import settle
from session_sync.state_store import StateUnusable, StoredState, load_state, save_state

KEPT_MAX_AGE_S = 30 * 86400
STALE_TEMP_AGE_S = 600


@dataclass(frozen=True)
class Settings:
    state_dir: Path

    @property
    def config_path(self) -> Path:
        return self.state_dir / "config.json"

    @property
    def state_path(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def kept_root(self) -> Path:
        return self.state_dir / "kept"

    @property
    def lock_path(self) -> Path:
        return self.state_dir / "lock"

    @property
    def log_path(self) -> Path:
        return self.state_dir / "agent.log"


class RunAborted(Exception):
    """Nothing was changed. The message says what to fix."""


@dataclass
class PartitionSummary:
    path: Path
    records: int
    tombstones: int
    live: bool


@dataclass
class RunReport:
    applied: bool
    partitions: List[PartitionSummary]
    planned: List[Action]
    problems: List[Problem]
    unenrolled: Dict[Path, int]
    outcomes: List[Outcome] = field(default_factory=list)

    @property
    def failures(self) -> List[Outcome]:
        return [outcome for outcome in self.outcomes if outcome.problem]

    @property
    def done(self) -> List[Outcome]:
        return [outcome for outcome in self.outcomes if not outcome.problem]


def label(partition: Path) -> str:
    return "%s/%s" % (partition.parent.name[:8], partition.name[:8])


def sync(settings: Settings, apply: bool, prefer: Optional[str] = None,
         now_ns: Callable[[], int] = time.time_ns, running: Callable[[], bool] = app_running) -> RunReport:
    partitions = _enrolled_or_abort(settings)
    stored = _state_or_abort(settings)
    if apply:
        _sweep_stale_temps(partitions, now_ns())

    scans = _scan(partitions, stored.cache, now_ns())
    app_is_running = running()
    live = {str(p) for p in partitions if is_live(p, app_is_running)}
    the_plan = plan([scan.snapshot for scan in scans.values()], stored.sync, live,
                    _resolve_prefer(prefer, partitions))
    report = RunReport(
        applied=apply,
        partitions=[PartitionSummary(p, len(scans[str(p)].records), len(scans[str(p)].tombstones), str(p) in live)
                    for p in partitions],
        planned=list(the_plan.actions), problems=list(the_plan.problems),
        unenrolled=unenrolled_with_records(partitions))
    if not apply:
        return report

    _journal_intents(settings, stored, the_plan)
    kept_dir = settings.kept_root / time.strftime("%Y%m%d-%H%M%S", time.localtime(now_ns() // 1_000_000_000))
    applier = Applier(scans, is_live=lambda partition: is_live(partition, running()), kept_dir=kept_dir)
    report.outcomes = applier.apply(the_plan)

    fresh = _scan(partitions, {key: scan.cache for key, scan in scans.items()}, now_ns())
    stored.sync = settle(stored.sync, [scan.snapshot for scan in fresh.values()])
    stored.cache = {key: scan.cache for key, scan in fresh.items()}
    if not report.failures:
        stored.last_success_ms = now_ns() // 1_000_000
        _prune_kept(settings.kept_root, now_ns())
    save_state(settings.state_path, stored)
    return report


def remember_reported(settings: Settings, digest: str) -> None:
    """R12: a standing problem is logged once, so the next quiet run must know what this one said."""
    stored = load_state(settings.state_path)
    if stored.reported != digest:
        stored.reported = digest
        save_state(settings.state_path, stored)


def _enrolled_or_abort(settings: Settings) -> List[Path]:
    try:
        partitions = load_enrolled(settings.config_path)
        if len(partitions) < 2:
            raise EnrolmentError("%d partition(s) enrolled; syncing needs at least two. "
                                 "Run with --list to see candidates and --enroll PATH to add one." % len(partitions))
        for partition in partitions:
            validate_partition(partition)
    except EnrolmentError as error:
        raise RunAborted(str(error))
    return partitions


def _state_or_abort(settings: Settings) -> StoredState:
    try:
        return load_state(settings.state_path)
    except StateUnusable as error:
        raise RunAborted(str(error))


def _scan(partitions: List[Path], cache: Dict[str, dict], now: int) -> Dict[str, PartitionScan]:
    return {str(p): scan_partition(p, cache.get(str(p), {}), now_ns=now) for p in partitions}


def _resolve_prefer(prefer: Optional[str], partitions: List[Path]) -> Optional[str]:
    if prefer is None:
        return None
    for partition in partitions:
        if prefer in (str(partition), label(partition)):
            return str(partition)
    raise RunAborted("--prefer %s names no enrolled partition. Use a path or label shown by --list." % prefer)


def _journal_intents(settings: Settings, stored: StoredState, the_plan: Plan) -> None:
    """R7, R10: if this run dies after creating a record, the next run must know the
    record was there, or it would put back one the app removed in between."""
    for action in the_plan.actions:
        if isinstance(action, CreateRecord):
            stored.sync.placing.setdefault(action.target, set()).add(action.session_id)
    save_state(settings.state_path, stored)


def _sweep_stale_temps(partitions: List[Path], now: int) -> None:
    for partition in partitions:
        for name in os.listdir(partition):
            if not (name.startswith(TEMP_PREFIX) and name.endswith(TEMP_SUFFIX)):
                continue
            stamp = stamp_of(partition / name)
            if stamp is not None and now - stamp[0] > STALE_TEMP_AGE_S * 1_000_000_000:
                try:
                    os.unlink(partition / name)
                except OSError:
                    pass


def _prune_kept(kept_root: Path, now: int) -> None:
    try:
        runs = list(kept_root.iterdir())
    except OSError:
        return
    for run_dir in runs:
        try:
            too_old = now - os.lstat(run_dir).st_mtime_ns > KEPT_MAX_AGE_S * 1_000_000_000
        except OSError:
            continue
        if too_old and run_dir.is_dir() and not run_dir.is_symlink():
            shutil.rmtree(run_dir, ignore_errors=True)
