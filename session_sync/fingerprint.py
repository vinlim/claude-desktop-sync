"""Reduces a record's bytes to the state the planner compares. Pure."""
import hashlib
import json
import math

from session_sync.model import Copy

# Top-level keys the app rewrites with no user action (DESIGN.md F6): lastFocusedAt on
# every click, errorAt stamped afresh by each login for a side session that never started.
VOLATILE_KEYS = ("lastFocusedAt", "errorAt")

UNREADABLE = Copy(state_hash=None)


def fingerprint(session_id: str, data: bytes) -> Copy:
    try:
        record = json.loads(data.decode("utf-8"))
    except ValueError:  # covers both bad UTF-8 and bad JSON
        return UNREADABLE
    if not isinstance(record, dict) or record.get("sessionId") != "local_" + session_id:
        return UNREADABLE

    state = {key: value for key, value in record.items() if key not in VOLATILE_KEYS}
    # ASCII output, because the app cuts strings by UTF-16 code unit and a record can
    # hold half a surrogate pair, which cannot be encoded as UTF-8.
    canonical = json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return Copy(state_hash=hashlib.sha256(canonical.encode("ascii")).hexdigest(),
                last_activity_at=_activity(record.get("lastActivityAt")))


def _activity(value) -> int:
    # bool is an int in Python; a record holding true here is not a timestamp.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if not math.isfinite(value) or value < 0 or value != int(value):
        return 0
    return int(value)
