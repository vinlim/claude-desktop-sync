"""Works out what to remember after a run, from what the partitions hold now. Pure."""
from typing import List

from session_sync.model import Snapshot, SyncState


def settle(state: SyncState, snapshots: List[Snapshot]) -> SyncState:
    agreed = dict(state.agreed)
    seen = {s.key: set(state.seen.get(s.key, ())) | set(s.records) for s in snapshots}

    known_ids = set(agreed)
    for snapshot in snapshots:
        known_ids.update(snapshot.records, seen[snapshot.key])

    for session_id in known_ids:
        holders = [s for s in snapshots if session_id in s.records]
        if not holders:
            # Gone everywhere. Forgetting it here keeps a later re-adoption from looking lost (R7).
            agreed.pop(session_id, None)
            for ids in seen.values():
                ids.discard(session_id)
        elif len(holders) == len(snapshots):
            hashes = {s.records[session_id].state_hash for s in holders}
            if len(hashes) == 1 and None not in hashes:
                agreed[session_id] = hashes.pop()

    return SyncState(agreed=agreed, seen=seen, placing={}, placed=_still_in_place(state, snapshots, agreed))


def _still_in_place(state: SyncState, snapshots: List[Snapshot], agreed: dict) -> dict:
    """A placement is worth remembering only while the copy is still there, untouched, and not yet agreed."""
    kept = {}
    for snapshot in snapshots:
        entries = {sid: placed for sid, placed in state.placed.get(snapshot.key, {}).items()
                   if sid in snapshot.records and snapshot.records[sid].state_hash == placed
                   and agreed.get(sid) != placed}
        if entries:
            kept[snapshot.key] = entries
    return kept
