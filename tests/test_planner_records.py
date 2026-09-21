import unittest

from session_sync.model import CreateRecord, Problem, ReplaceRecord
from session_sync.planner import plan
from tests.helpers import NOW, X, copy, snapshot, state


def planned(snapshots, sync_state=None, live=(), prefer=None):
    return plan(snapshots, sync_state or state(), live=set(live), now_ms=NOW, prefer=prefer)


class NewRecords(unittest.TestCase):
    """R5."""

    def test_a_record_held_by_one_partition_is_created_in_the_other(self):
        result = planned([snapshot("A", {X: copy("v1")}), snapshot("B")])

        self.assertEqual(result.actions, [CreateRecord(X, source="A", target="B")])
        self.assertEqual(result.problems, [])

    def test_creation_is_allowed_in_a_live_partition(self):
        result = planned([snapshot("A", {X: copy("v1")}), snapshot("B")], live={"B"})

        self.assertEqual(result.actions, [CreateRecord(X, source="A", target="B")])

    def test_identical_copies_need_nothing(self):
        result = planned([snapshot("A", {X: copy("v1")}), snapshot("B", {X: copy("v1")})])

        self.assertEqual((result.actions, result.problems), ([], []))


class OneSideChanged(unittest.TestCase):
    """R3."""

    def test_the_changed_side_replaces_the_side_still_at_the_agreed_state(self):
        result = planned([snapshot("A", {X: copy("v1")}), snapshot("B", {X: copy("v2")})],
                         state(agreed={X: "v1"}))

        self.assertEqual(result.actions, [ReplaceRecord(X, source="B", target="A", keep=False)])

    def test_mtime_plays_no_part_so_a_clicked_stale_copy_never_wins(self):
        # The reviewer's B1: work under B, then only click X under A. A click
        # changes no normalised state, so A still equals the agreed hash.
        result = planned([snapshot("A", {X: copy("v1", activity=10)}),
                          snapshot("B", {X: copy("v2", activity=50)})],
                         state(agreed={X: "v1"}))

        self.assertEqual(result.actions, [ReplaceRecord(X, source="B", target="A", keep=False)])

    def test_a_live_partition_is_never_overwritten(self):
        result = planned([snapshot("A", {X: copy("v1")}), snapshot("B", {X: copy("v2")})],
                         state(agreed={X: "v1"}), live={"A"})

        self.assertEqual(result.actions, [])
        self.assertEqual(result.problems, [Problem("live", X, "A")])

    def test_with_three_partitions_every_unchanged_copy_is_replaced(self):
        result = planned([snapshot("A", {X: copy("v1")}), snapshot("B", {X: copy("v2")}),
                          snapshot("C", {X: copy("v1")})], state(agreed={X: "v1"}))

        self.assertEqual(result.actions, [ReplaceRecord(X, source="B", target="A", keep=False),
                                          ReplaceRecord(X, source="B", target="C", keep=False)])


if __name__ == "__main__":
    unittest.main()
