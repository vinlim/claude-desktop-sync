"""Reduces a record's bytes to the state the planner compares. Pure."""
import hashlib
import json

from session_sync.model import Copy

# Top-level keys the app rewrites without the user changing anything (DESIGN.md F6).
VOLATILE_KEYS = ("lastFocusedAt", "processGoneReason")

UNREADABLE = Copy(state_hash=None)


def fingerprint(session_id: str, data: bytes) -> Copy:
    try:
        record = json.loads(data.decode("utf-8"))
    except ValueError:  # covers both bad UTF-8 and bad JSON
        return UNREADABLE
    if not isinstance(record, dict) or record.get("sessionId") != "local_" + session_id:
        return UNREADABLE

    state = {key: value for key, value in record.items() if key not in VOLATILE_KEYS}
    canonical = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return Copy(state_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                last_activity_at=_activity(record.get("lastActivityAt")))


def _activity(value) -> int:
    # bool is an int in Python; a record holding true here is not a timestamp.
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value
