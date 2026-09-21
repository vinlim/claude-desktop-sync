"""A small table DSL so each planner scenario reads as a specification."""
from session_sync.model import Copy, Snapshot, SyncState

X = "11111111-1111-4111-8111-111111111111"
Y = "22222222-2222-4222-8222-222222222222"
NOW = 1_800_000_000_000


def copy(state, activity=0):
    return Copy(state_hash=state, last_activity_at=activity)


def unreadable():
    return Copy(state_hash=None)


def snapshot(key, records=None, tombstones=None, orphan_tmps=()):
    return Snapshot(key=key, records=dict(records or {}), tombstones=dict(tombstones or {}),
                    orphan_tmps=frozenset(orphan_tmps))


def state(agreed=None, deleted=(), seen=None, placing=None):
    return SyncState(agreed=dict(agreed or {}), deleted=set(deleted),
                     seen={k: set(v) for k, v in (seen or {}).items()},
                     placing={k: set(v) for k, v in (placing or {}).items()})
