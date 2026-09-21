"""Loads and saves what the tool remembers between runs (DESIGN.md R10)."""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict

from session_sync.atomic import write_atomic
from session_sync.model import SyncState
from session_sync.scanner import CacheEntry

VERSION = 1


class StateUnusable(Exception):
    """The state file exists but cannot be trusted. Running on a guess could undo deletes."""


@dataclass
class StoredState:
    sync: SyncState = field(default_factory=SyncState)
    cache: Dict[str, Dict[str, CacheEntry]] = field(default_factory=dict)
    reported: str = ""  # digest of the standing problems last written to the unattended log
    last_success_ms: int = 0


def load_state(path: Path) -> StoredState:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return StoredState()
    try:
        return _decode(json.loads(text))
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise StateUnusable(
            "%s cannot be read (%s). Nothing was changed. Move the file aside, or run with --reset-state "
            "to start again from first contact: differing copies are then decided by lastActivityAt and "
            "the losing copy is kept." % (path, error)) from error


def save_state(path: Path, stored: StoredState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_atomic(path, json.dumps(_encode(stored), indent=1, sort_keys=True).encode("utf-8"))


def _encode(stored: StoredState) -> dict:
    sync = stored.sync
    return {
        "version": VERSION,
        "agreed": sync.agreed,
        "deleted": sorted(sync.deleted),
        "seen": {key: sorted(ids) for key, ids in sync.seen.items()},
        "placing": {key: sorted(ids) for key, ids in sync.placing.items()},
        "cache": {key: {sid: list(entry) for sid, entry in entries.items()} for key, entries in stored.cache.items()},
        "reported": stored.reported,
        "last_success_ms": stored.last_success_ms,
    }


def _decode(raw: dict) -> StoredState:
    if not isinstance(raw, dict):
        raise ValueError("not a JSON object")
    if raw.get("version") != VERSION:
        raise ValueError("written by format version %r, this tool reads %d" % (raw.get("version"), VERSION))
    sync = SyncState(
        agreed={str(sid): str(value) for sid, value in raw["agreed"].items()},
        deleted=set(raw["deleted"]),
        seen={key: set(ids) for key, ids in raw["seen"].items()},
        placing={key: set(ids) for key, ids in raw["placing"].items()})
    cache = {key: {sid: (int(e[0]), int(e[1]), e[2], int(e[3])) for sid, e in entries.items()}
             for key, entries in raw["cache"].items()}
    return StoredState(sync=sync, cache=cache, reported=str(raw["reported"]),
                       last_success_ms=int(raw["last_success_ms"]))
