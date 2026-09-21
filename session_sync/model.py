"""The vocabulary shared by the planner and the code around it. No I/O here."""
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Mapping, Optional, Set, Union


@dataclass(frozen=True)
class Copy:
    """One partition's copy of a record, reduced to what a decision needs."""

    state_hash: Optional[str]  # None: the file is not a record the app would accept
    last_activity_at: int = 0

    @property
    def readable(self) -> bool:
        return self.state_hash is not None


@dataclass(frozen=True)
class Snapshot:
    """What one enrolled partition holds at scan time."""

    key: str
    records: Mapping[str, Copy]
    tombstones: Mapping[str, int]  # session id -> delete time, epoch ms
    orphan_tmps: FrozenSet[str] = frozenset()  # local_<id>.json.tmp with no record beside it


@dataclass
class SyncState:
    """What the tool remembers between runs (DESIGN.md, Model)."""

    agreed: Dict[str, str] = field(default_factory=dict)
    seen: Dict[str, Set[str]] = field(default_factory=dict)
    placing: Dict[str, Set[str]] = field(default_factory=dict)
    placed: Dict[str, Dict[str, str]] = field(default_factory=dict)  # partition -> id -> hash the tool put there


@dataclass(frozen=True)
class CreateRecord:
    session_id: str
    source: str
    target: str


@dataclass(frozen=True)
class ReplaceRecord:
    session_id: str
    source: str
    target: str
    keep: bool  # the replaced copy holds state found nowhere else


@dataclass(frozen=True)
class RetireRecord:
    session_id: str
    target: str


@dataclass(frozen=True)
class RetireTmp:
    session_id: str
    target: str


@dataclass(frozen=True)
class CreateTombstone:
    session_id: str
    source: str
    target: str


@dataclass(frozen=True)
class RetireTombstone:
    session_id: str
    target: str


Action = Union[CreateRecord, ReplaceRecord, RetireRecord, RetireTmp, CreateTombstone, RetireTombstone]


@dataclass(frozen=True)
class Problem:
    """Something left alone on purpose. kind: live, tied, lost or unreadable."""

    kind: str
    session_id: str
    partition: str


@dataclass
class Plan:
    actions: List[Action] = field(default_factory=list)
    problems: List[Problem] = field(default_factory=list)
