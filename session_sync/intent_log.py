"""A journal of creates: one line before each, one after it completed (DESIGN.md R7, R10).

If a run does not finish, the next one must know which records came to exist. One that is
missing again was removed by the app in between, and must not be put back. The line written
before a create cannot say whether the create happened, so only the line written after it
counts as proof. A create with no such line may never have happened, and is retried.
"""
import os
from pathlib import Path
from typing import Dict, NamedTuple, Set

INTEND, DONE = "intend", "done"


class Journal(NamedTuple):
    pending: Dict[str, Set[str]]  # partition -> ids with no proof that the create completed
    completed: Dict[str, Set[str]]  # partition -> ids whose create is known to have completed


class IntentLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def intend(self, partition: str, session_id: str) -> None:
        self._append(INTEND, partition, session_id)

    def done(self, partition: str, session_id: str) -> None:
        self._append(DONE, partition, session_id)

    def read(self) -> Journal:
        try:
            text = self.path.read_text()
        except FileNotFoundError:
            return Journal({}, {})
        intended: Dict[str, Set[str]] = {}
        completed: Dict[str, Set[str]] = {}
        for line in text.split("\n")[:-1]:  # a last piece with no newline is a write a crash cut short
            verb, _, rest = line.partition("\t")
            partition, _, session_id = rest.rpartition("\t")
            if partition and session_id and verb in (INTEND, DONE):
                (completed if verb == DONE else intended).setdefault(partition, set()).add(session_id)
        pending = {partition: ids - completed.get(partition, set()) for partition, ids in intended.items()}
        return Journal({partition: ids for partition, ids in pending.items() if ids}, completed)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def _append(self, verb: str, partition: str, session_id: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "w") as handle:
            handle.write("%s\t%s\t%s\n" % (verb, partition, session_id))
            handle.flush()
            os.fsync(handle.fileno())
