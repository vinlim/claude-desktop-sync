"""Works out what to remember after a run, from what the partitions hold now. Pure."""
from typing import List

from session_sync.model import Snapshot, SyncState


def settle(state: SyncState, snapshots: List[Snapshot]) -> SyncState:
    agreed = dict(state.agreed)
    deleted = set(state.deleted)
    seen = {s.key: set(state.seen.get(s.key, ())) | set(s.records) for s in snapshots}

    known_ids = set(agreed) | deleted
    for snapshot in snapshots:
        known_ids.update(snapshot.records, snapshot.tombstones, seen[snapshot.key])

    for session_id in known_ids:
        holders = [s for s in snapshots if session_id in s.records]
        entombed = [s for s in snapshots if session_id in s.tombstones]

        if not holders and not entombed:
            _forget(session_id, agreed, seen)
        elif not holders and len(entombed) == len(snapshots):
            deleted.add(session_id)
            _forget(session_id, agreed, seen)
        elif len(holders) == len(snapshots):
            hashes = {s.records[session_id].state_hash for s in holders}
            if len(hashes) == 1 and None not in hashes:
                agreed[session_id] = hashes.pop()
            # The flag outlives the record until every stale tombstone is gone: without
            # it the next run would read the re-created record as deleted.
            if not entombed:
                deleted.discard(session_id)

    return SyncState(agreed=agreed, deleted=deleted, seen=seen, placing={})


def _forget(session_id: str, agreed: dict, seen: dict) -> None:
    agreed.pop(session_id, None)
    for ids in seen.values():
        ids.discard(session_id)
