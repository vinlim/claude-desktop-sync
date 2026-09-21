"""Decides what a sync run should do. Pure: snapshots and state in, a plan out.

Rule numbers refer to DESIGN.md. Actions for one session id are emitted in the
order they must happen; whoever applies them stops that id at the first failure.
"""
from typing import List, Optional, Set

from session_sync.model import (CreateRecord, CreateTombstone, Plan, Problem, ReplaceRecord, RetireRecord, RetireTmp,
                                RetireTombstone, Snapshot, SyncState)


def plan(snapshots: List[Snapshot], state: SyncState, live: Set[str], prefer: Optional[str] = None,
         prefer_session: Optional[str] = None) -> Plan:
    """prefer settles ties in favour of one partition, for one session if prefer_session is given."""
    result = Plan()
    session_ids = set()
    for snapshot in snapshots:
        session_ids.update(snapshot.records, snapshot.tombstones, snapshot.orphan_tmps)
    for session_id in sorted(session_ids):
        preferred = prefer if prefer_session in (None, session_id) else None
        _plan_session(session_id, snapshots, state, live, preferred, result)
    return result


def _plan_session(session_id: str, snapshots: List[Snapshot], state: SyncState, live: Set[str],
                  prefer: Optional[str], result: Plan) -> None:
    holders = [s for s in snapshots if session_id in s.records]
    unreadable = [s for s in holders if not s.records[session_id].readable]
    if unreadable:  # R11: without a readable copy nothing about this id can be judged
        result.problems.extend(Problem("unreadable", session_id, s.key) for s in unreadable)
        return

    entombed = [s for s in snapshots if session_id in s.tombstones]
    if not entombed:
        if holders:
            _plan_record(session_id, snapshots, holders, state, live, prefer, result, absence_explained=False)
        return

    if holders and _used_after_delete(session_id, holders, entombed):
        version_chosen = _plan_record(session_id, snapshots, holders, state, live, prefer, result,
                                      absence_explained=True)
        # With the copies tied, nothing can be put back. The tombstone stays, or the partition
        # that deleted would read as having lost the record (R7) and never get it again.
        if version_chosen:
            for snapshot in entombed:
                _unless_live(snapshot, session_id, live, result, RetireTombstone(session_id, target=snapshot.key))
        return

    _plan_delete(session_id, snapshots, entombed[0], live, result)


def _used_after_delete(session_id: str, holders: List[Snapshot], entombed: List[Snapshot]) -> bool:
    """R6. Time alone decides: the app stamps a re-adopted session with the current time (F10)."""
    deleted_at = max(s.tombstones[session_id] for s in entombed)
    return max(s.records[session_id].last_activity_at for s in holders) > deleted_at


def _plan_delete(session_id: str, snapshots: List[Snapshot], tombstone_source: Snapshot, live: Set[str],
                 result: Plan) -> None:
    for snapshot in snapshots:
        record_remains = False
        if session_id in snapshot.records:
            record_remains = not _unless_live(snapshot, session_id, live, result,
                                              RetireRecord(session_id, target=snapshot.key))
        elif session_id in snapshot.orphan_tmps:
            _unless_live(snapshot, session_id, live, result, RetireTmp(session_id, target=snapshot.key))
        if session_id not in snapshot.tombstones and not record_remains:
            result.actions.append(CreateTombstone(session_id, source=tombstone_source.key, target=snapshot.key))


def _unless_live(snapshot: Snapshot, session_id: str, live: Set[str], result: Plan, action) -> bool:
    """R9: an existing file in a live partition is left alone. Returns whether the action was planned."""
    if snapshot.key in live:
        result.problems.append(Problem("live", session_id, snapshot.key))
        return False
    result.actions.append(action)
    return True


def _plan_record(session_id: str, snapshots: List[Snapshot], holders: List[Snapshot], state: SyncState,
                 live: Set[str], prefer: Optional[str], result: Plan, absence_explained: bool) -> bool:
    """Returns whether a version was chosen."""
    untouched = [s for s in holders if _still_as_synced(s, session_id, state)]
    winner = _one_sided_winner(session_id, holders, untouched) or _latest_activity_winner(session_id, holders, prefer)
    if winner is None:  # R4: a tie nobody settled
        result.problems.extend(Problem("tied", session_id, s.key) for s in _most_active(session_id, holders))
        return False

    winning = winner.records[session_id]
    for target in holders:
        held = target.records[session_id]
        if held.state_hash != winning.state_hash:
            # R8: only a copy still as synced, and strictly behind in activity, holds nothing unique.
            superseded = target in untouched and winning.last_activity_at > held.last_activity_at
            _unless_live(target, session_id, live, result,
                         ReplaceRecord(session_id, source=winner.key, target=target.key, keep=not superseded))

    for target in snapshots:
        if session_id in target.records:
            continue
        if not absence_explained and _was_held_by(target.key, session_id, state):  # R7
            result.problems.append(Problem("lost", session_id, target.key))
            continue
        result.actions.append(CreateRecord(session_id, source=winner.key, target=target.key))  # R5
    return True


def _still_as_synced(snapshot: Snapshot, session_id: str, state: SyncState) -> bool:
    """The copy is the agreed state, or a version the tool itself placed there and the app has not touched."""
    held = snapshot.records[session_id].state_hash
    return held == state.agreed.get(session_id) or held == state.placed.get(snapshot.key, {}).get(session_id)


def _one_sided_winner(session_id: str, holders: List[Snapshot], untouched: List[Snapshot]) -> Optional[Snapshot]:
    """R3: every copy that moved on moved to the same new state, and that state is not behind.

    A copy can also leave the synced state by going back in time (a restored backup, a
    promoted temp file, a login flushing stale memory). Its activity is then lower than
    what it would replace, and the case falls through to R4.
    """
    if len({s.records[session_id].state_hash for s in holders}) == 1:
        return holders[0]
    changed = [s for s in holders if s not in untouched]
    if len({s.records[session_id].state_hash for s in changed}) != 1:
        return None
    candidate = changed[0]
    behind = any(candidate.records[session_id].last_activity_at < s.records[session_id].last_activity_at
                 for s in untouched)
    return None if behind else candidate


def _most_active(session_id: str, holders: List[Snapshot]) -> List[Snapshot]:
    latest = max(s.records[session_id].last_activity_at for s in holders)
    return [s for s in holders if s.records[session_id].last_activity_at == latest]


def _latest_activity_winner(session_id: str, holders: List[Snapshot], prefer: Optional[str]) -> Optional[Snapshot]:
    """R4: later activity wins. Equal activity on different states is a tie."""
    leaders = _most_active(session_id, holders)
    if len({s.records[session_id].state_hash for s in leaders}) == 1:
        return leaders[0]
    return next((s for s in leaders if s.key == prefer), None)


def _was_held_by(partition: str, session_id: str, state: SyncState) -> bool:
    return session_id in state.seen.get(partition, ()) or session_id in state.placing.get(partition, ())
