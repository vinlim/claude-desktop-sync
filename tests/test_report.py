import unittest
from pathlib import Path

from session_sync.applier import Outcome
from session_sync.model import CreateRecord, Problem, RetireRecord
from session_sync.report import render
from session_sync.run import PartitionSummary, RunReport

A = Path("/data/Claude/claude-code-sessions/aaaaaaaa-0000-4000-8000-000000000001/aaaaaaaa-0000-4000-8000-0000000000a1")
B = Path("/data/Claude/claude-code-sessions/bbbbbbbb-0000-4000-8000-000000000002/bbbbbbbb-0000-4000-8000-0000000000b2")
X = "11111111-1111-4111-8111-111111111111"


def report(applied=False, planned=(), problems=(), outcomes=(), unenrolled=None, live_b=False):
    return RunReport(applied=applied,
                     partitions=[PartitionSummary(A, 3, 1, False), PartitionSummary(B, 2, 0, live_b)],
                     planned=list(planned), problems=list(problems), unenrolled=unenrolled or {},
                     outcomes=list(outcomes))


class NormalOutput(unittest.TestCase):
    def test_nothing_to_do_says_so(self):
        text, _ = render(report())

        self.assertIn("aaaaaaaa/aaaaaaaa     3 records    1 delete markers", text)
        self.assertIn("In sync.", text)

    def test_a_dry_run_counts_what_it_would_do_and_how_to_do_it(self):
        text, _ = render(report(planned=[CreateRecord(X, str(A), str(B)), CreateRecord("y", str(A), str(B))],
                                live_b=True))

        self.assertIn("planned     2  create record -> bbbbbbbb/bbbbbbbb", text)
        self.assertIn("(in use by the running app)", text)
        self.assertIn("Dry run. Pass --apply to write.", text)
        self.assertNotIn(X, text, "ids are listed only with --verbose")

    def test_verbose_lists_every_action(self):
        text, _ = render(report(planned=[CreateRecord(X, str(A), str(B))]), verbose=True)

        self.assertIn("create record  %s -> bbbbbbbb/bbbbbbbb" % X, text)

    def test_an_applied_run_separates_done_from_not_done_with_the_reason(self):
        done = Outcome(CreateRecord(X, str(A), str(B)), None)
        refused = Outcome(RetireRecord("y", str(A)), "the partition is live")

        text, _ = render(report(applied=True, planned=[done.action, refused.action], outcomes=[done, refused]))

        self.assertIn("done        1  create record -> bbbbbbbb/bbbbbbbb", text)
        self.assertIn("not done    retire record  y in aaaaaaaa/aaaaaaaa: the partition is live", text)
        self.assertNotIn("Dry run", text)

    def test_each_kind_of_problem_explains_what_to_do(self):
        problems = [Problem("live", X, str(B)), Problem("tied", X, str(A)), Problem("lost", X, str(B)),
                    Problem("unreadable", X, str(A))]

        text, _ = render(report(problems=problems))

        self.assertIn("waiting", text)
        self.assertIn("--prefer", text)
        self.assertIn("not recreated", text)
        self.assertIn("not a valid session record", text)
        self.assertNotIn("In sync.", text)

    def test_an_unenrolled_partition_with_records_is_pointed_out(self):
        other = A.parent / "cccccccc-0000-4000-8000-0000000000c3"

        text, _ = render(report(unenrolled={other: 7}))

        self.assertIn("Not enrolled, so left alone: %s (7 records)" % other, text)
        self.assertIn("--enroll", text)


class QuietOutput(unittest.TestCase):
    """R12: an unattended log records changes, and a standing problem only once."""

    def test_silence_when_nothing_happened(self):
        text, _ = render(report(applied=True), quiet=True)

        self.assertEqual(text, "")

    def test_writes_are_always_logged(self):
        done = Outcome(CreateRecord(X, str(A), str(B)), None)

        text, _ = render(report(applied=True, outcomes=[done]), quiet=True)

        self.assertIn("create record", text)

    def test_a_standing_problem_is_logged_once_until_the_problem_set_changes(self):
        standing = report(applied=True, problems=[Problem("live", X, str(B))])

        first, digest = render(standing, quiet=True, previous_digest="")
        second, same = render(standing, quiet=True, previous_digest=digest)
        cleared, empty = render(report(applied=True), quiet=True, previous_digest=digest)

        self.assertIn("waiting", first)
        self.assertEqual((second, same), ("", digest))
        self.assertIn("cleared", cleared)
        self.assertEqual(empty, "")

    def test_a_repeating_failure_is_logged_once_too(self):
        refused = Outcome(RetireRecord("y", str(A)), "PermissionError: no")
        failing = report(applied=True, outcomes=[refused])

        first, digest = render(failing, quiet=True, previous_digest="")
        second, _ = render(failing, quiet=True, previous_digest=digest)

        self.assertIn("PermissionError", first)
        self.assertEqual(second, "")


if __name__ == "__main__":
    unittest.main()
