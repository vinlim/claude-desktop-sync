"""Turns a run report into text. Pure, so the unattended log's rules can be tested (R12)."""
import hashlib
import re
from pathlib import Path
from typing import List, Tuple

from session_sync.applier import Outcome
from session_sync.model import Action, Problem
from session_sync.run import RunReport, label

EXPLANATIONS = {
    "live": "waiting: the running app may still hold this partition (it is the current login, or the login "
            "changed in the last two minutes). A later run syncs it",
    "tied": "both copies changed with no new activity on either. Choose one with --prefer PARTITION",
    "lost": "was here before and is gone without a delete marker, so it is not recreated here. "
            "--recreate ID lifts that",
    "unreadable": "this copy cannot be read or is not a valid session record, so nothing is done for this session",
    "future": "this copy's last activity is dated in the future, so it cannot be ordered against a delete or "
              "another copy. Nothing is done for this session until the clock catches up. Using the session, or "
              "deleting it under this login as well, settles it sooner",
}


def render(report: RunReport, verbose: bool = False, quiet: bool = False,
           previous_digest: str = "") -> Tuple[str, str]:
    """Returns the text and the digest of the standing problems, for the next quiet run."""
    standing = _standing_lines(report)
    digest = _digest(standing)
    if quiet:
        return _quiet_text(report, standing, digest, previous_digest), digest

    lines = ["%s  %4d records %4d delete markers%s" % (
        label(p.path), p.records, p.tombstones, "  (in use by the running app)" if p.live else "")
        for p in report.partitions]
    if verbose:
        shown = [o.action for o in report.done] if report.applied else report.planned
        lines += ["  %s" % _describe(action) for action in shown]
    lines += _count_lines(report)
    lines += _kept_lines(report)
    lines += standing
    lines += ["Not enrolled, so left alone: %s (%d records). Add it with --enroll PATH if it is yours to sync."
              % (path, count) for path, count in sorted(report.unenrolled.items())]
    if not report.planned and not standing:
        lines.append("In sync.")
    elif report.planned and not report.applied:
        lines.append("Dry run. Pass --apply to write.")
    return "\n".join(lines), digest


def _quiet_text(report: RunReport, standing: List[str], digest: str, previous_digest: str) -> str:
    lines = _count_lines(report) + _kept_lines(report) if report.done else []
    if digest != previous_digest:
        lines += standing or ["earlier problems cleared"]
    return "\n".join(lines)


def _count_lines(report: RunReport) -> List[str]:
    counted = [o.action for o in report.done] if report.applied else report.planned
    counts = {}
    for action in counted:
        key = "%s -> %s" % (_kind(action), label(Path(action.target)))
        counts[key] = counts.get(key, 0) + 1
    word = "done   " if report.applied else "planned"
    return ["%s %5d  %s" % (word, counts[key], key) for key in sorted(counts)]


def _kept_lines(report: RunReport) -> List[str]:
    """R8: whatever was replaced or retired with something unique in it, and where it went."""
    return ["kept the previous copy of %s from %s: %s" % (o.action.session_id, label(Path(o.action.target)), path)
            for o in report.done for path in o.kept]


def _standing_lines(report: RunReport) -> List[str]:
    lines = ["not done    %s: %s" % (_describe(o.action, arrow=False), o.problem) for o in report.failures]
    lines += [_explain(problem) for problem in report.problems]
    return lines


def _explain(problem: Problem) -> str:
    return "%s in %s: %s" % (problem.session_id, label(Path(problem.partition)), EXPLANATIONS[problem.kind])


def _kind(action: Action) -> str:
    """CreateRecord -> 'create record'."""
    return re.sub(r"(?<!^)([A-Z])", r" \1", type(action).__name__).lower()


def _describe(action: Action, arrow: bool = True) -> str:
    where = ("-> %s" if arrow else "in %s") % label(Path(action.target))
    return "%s  %s %s" % (_kind(action), action.session_id, where)


def _digest(lines: List[str]) -> str:
    return hashlib.sha256("\n".join(sorted(lines)).encode("utf-8")).hexdigest() if lines else ""
