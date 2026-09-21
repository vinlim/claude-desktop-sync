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
from session_sync.intent_log import IntentLog
from session_sync.enrolment import (EnrolmentError, load_enrolled, reject_same_directory_twice,
                                    unenrolled_with_records, validate_partition)
from session_sync.liveness import app_running, is_live, observe_logins
from session_sync.model import Action, CreateRecord, Problem, ReplaceRecord
from session_sync.planner import plan
from session_sync.scanner import PartitionScan, scan_partition, stamp_of
from session_sync.settle import settle
from session_sync.state_store import StateUnusable, StoredState, encode_state, load_state, save_state

KEPT_MAX_AGE_S = 30 * 86400
KEPT_MAX_BYTES = 500 * 1024 * 1024
STALE_TEMP_AGE_S = 600
HEARTBEAT_S = 300  # how stale "last clean run" may get before an otherwise idle run writes it down


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

    @property
    def intent_log_path(self) -> Path:
        return self.state_dir / "placing.log"


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


def sync(settings: Settings, apply: bool, prefer: Optional[str] = None, prefer_session: Optional[str] = None,
         now_ns: Callable[[], int] = time.time_ns, running: Callable[[], bool] = app_running) -> RunReport:
    partitions = _enrolled_or_abort(settings)
    stored = _state_or_abort(settings)
    on_disk = encode_state(stored)
    intents = IntentLog(settings.intent_log_path)
    for partition, session_ids in intents.read().items():  # left by a run that did not finish
        stored.sync.placing.setdefault(partition, set()).update(session_ids)
    if apply:
        _sweep_stale_temps(partitions + [settings.state_dir], now_ns())
        _sweep_stale_temps(_folders_under(settings.kept_root), now_ns())

    scans = _scan_or_abort(partitions, stored.cache, now_ns, "Nothing was changed.")
    app_is_running = running()
    observe_logins(partitions, stored.logins, now_ns() // 1_000_000)
    live = {str(p) for p in partitions if is_live(p, app_is_running, now_ns() // 1_000_000, stored.logins)}
    the_plan = plan([scan.snapshot for scan in scans.values()], stored.sync, live,
                    _resolve_prefer(prefer, partitions), prefer_session)
    report = RunReport(
        applied=apply,
        partitions=[PartitionSummary(p, len(scans[str(p)].records), len(scans[str(p)].tombstones), str(p) in live)
                    for p in partitions],
        planned=list(the_plan.actions), problems=list(the_plan.problems),
        unenrolled=unenrolled_with_records(partitions))
    if not apply:
        return report

    if _remember_what_was_seen(stored, scans):
        # Durable before the first write: a run that dies must not forget what it saw (R7).
        save_state(settings.state_path, stored)
        on_disk = encode_state(stored)
    kept_dir = settings.kept_root / time.strftime("%Y%m%d-%H%M%S", time.localtime(now_ns() // 1_000_000_000))
    applier = Applier(scans, kept_dir=kept_dir, before_create=intents.record, is_live=lambda partition: is_live(
        partition, running(), now_ns() // 1_000_000, stored.logins))
    report.outcomes = applier.apply(the_plan)
    _remember_placements(stored, report.done, scans)

    fresh = _scan_or_abort(partitions, {key: scan.cache for key, scan in scans.items()}, now_ns,
                           "This run's writes stand, but what it learned was not saved. The next run checks again.")
    stored.sync = settle(stored.sync, [scan.snapshot for scan in fresh.values()],
                         also_present=_created(report.done))
    stored.cache = {key: scan.cache for key, scan in fresh.items()}
    if not report.failures:
        _prune_kept(settings.kept_root, now_ns(), never=kept_dir)
    _save_if_worth_it(settings, stored, on_disk, clean=not report.failures, now_ms=now_ns() // 1_000_000)
    intents.clear()
    return report


def _remember_what_was_seen(stored: StoredState, scans: Dict[str, PartitionScan]) -> bool:
    """Returns whether anything new was seen."""
    grew = False
    for key, scan in scans.items():
        known = stored.sync.seen.setdefault(key, set())
        grew = grew or not known.issuperset(scan.snapshot.records)
        known.update(scan.snapshot.records)
    return grew


def _created(done: List[Outcome]) -> Dict[str, set]:
    created: Dict[str, set] = {}
    for outcome in done:
        if isinstance(outcome.action, CreateRecord):
            created.setdefault(outcome.action.target, set()).add(outcome.action.session_id)
    return created


def _remember_placements(stored: StoredState, done: List[Outcome], scans: Dict[str, PartitionScan]) -> None:
    """R3: a version the tool placed is not a change made by the app."""
    for outcome in done:
        action = outcome.action
        if isinstance(action, (CreateRecord, ReplaceRecord)):
            placed = scans[action.source].snapshot.records[action.session_id].state_hash
            stored.sync.placed.setdefault(action.target, {})[action.session_id] = placed


def _save_if_worth_it(settings: Settings, stored: StoredState, on_disk: dict, clean: bool, now_ms: int) -> None:
    """An unattended run fires every few seconds while the app saves. Most runs change nothing,
    so the last clean run is only written down when something else changed or it has gone stale."""
    changed = {k: v for k, v in encode_state(stored).items() if k != "last_success_ms"} != \
              {k: v for k, v in on_disk.items() if k != "last_success_ms"}
    stale = now_ms - stored.last_success_ms > HEARTBEAT_S * 1000
    if clean and (changed or stale):
        stored.last_success_ms = now_ms
    if changed or (clean and stale):
        save_state(settings.state_path, stored)


def forget_presence(settings: Settings, session_id: str) -> List[str]:
    """R7's way out for one session: the next run may recreate it where it was lost."""
    stored = load_state(settings.state_path)
    forgotten = []
    for memory in (stored.sync.seen, stored.sync.placing):
        for partition, ids in memory.items():
            if session_id in ids:
                ids.discard(session_id)
                forgotten.append(partition)
    if forgotten:
        save_state(settings.state_path, stored)
    return sorted(set(forgotten))


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
        reject_same_directory_twice(partitions)
    except EnrolmentError as error:
        raise RunAborted(str(error))
    return partitions


def _state_or_abort(settings: Settings) -> StoredState:
    try:
        return load_state(settings.state_path)
    except StateUnusable as error:
        raise RunAborted(str(error))


def _scan_or_abort(partitions: List[Path], cache: Dict[str, dict], clock: Callable[[], int],
                   consequence: str) -> Dict[str, PartitionScan]:
    """A partition that cannot be listed says nothing about what it holds, so nothing is decided."""
    scans = {}
    for partition in partitions:
        try:
            scans[str(partition)] = scan_partition(partition, cache.get(str(partition), {}), clock=clock)
        except OSError as error:
            raise RunAborted("%s cannot be read (%s: %s). %s"
                             % (partition, type(error).__name__, error, consequence))
    return scans


def _resolve_prefer(prefer: Optional[str], partitions: List[Path]) -> Optional[str]:
    if prefer is None:
        return None
    for partition in partitions:
        if prefer in (str(partition), label(partition)):
            return str(partition)
    raise RunAborted("--prefer %s names no enrolled partition. Use a path or label shown by --list." % prefer)


def _sweep_stale_temps(folders: List[Path], now: int) -> None:
    """A killed run can leave a staged file behind. Anything this old belongs to no running sync."""
    for folder in folders:
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        for name in names:
            if not (name.startswith(TEMP_PREFIX) and name.endswith(TEMP_SUFFIX)):
                continue
            stamp = stamp_of(folder / name)
            if stamp is not None and now - stamp[0] > STALE_TEMP_AGE_S * 1_000_000_000:
                try:
                    os.unlink(folder / name)
                except OSError:
                    pass


def _folders_under(root: Path) -> List[Path]:
    return [Path(folder) for folder, _, _ in os.walk(root)]


def _prune_kept(kept_root: Path, now: int, never: Path) -> None:
    """Kept copies are bounded by age and by total size, oldest run first. The run that just
    finished is never touched: its report names those paths."""
    try:
        runs = sorted((os.lstat(run).st_mtime_ns, run) for run in kept_root.iterdir()
                      if run.is_dir() and not run.is_symlink() and run != never)
    except OSError:
        return
    sizes = {run: _size_of(run) for _, run in runs}
    total = sum(sizes.values())
    for written_ns, run in runs:
        if now - written_ns > KEPT_MAX_AGE_S * 1_000_000_000 or total > KEPT_MAX_BYTES:
            shutil.rmtree(run, ignore_errors=True)
            total -= sizes[run]


def _size_of(folder: Path) -> int:
    total = 0
    for parent, _, files in os.walk(folder):
        for name in files:
            try:
                total += os.lstat(os.path.join(parent, name)).st_size
            except OSError:
                pass
    return total
