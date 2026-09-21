"""Records each create just before it happens (DESIGN.md R7, R10).

If a run dies, the next one must know which records were about to exist: one that is
missing again was removed by the app in between, and must not be put back.
"""
import os
from pathlib import Path
from typing import Dict, Set


class IntentLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def record(self, partition: str, session_id: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write("%s\t%s\n" % (partition, session_id))
            handle.flush()
            os.fsync(handle.fileno())

    def read(self) -> Dict[str, Set[str]]:
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return {}
        intents: Dict[str, Set[str]] = {}
        for line in text.split("\n")[:-1]:  # a last piece with no newline is a write a crash cut short
            partition, _, session_id = line.rpartition("\t")
            if partition and session_id:
                intents.setdefault(partition, set()).add(session_id)
        return intents

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
