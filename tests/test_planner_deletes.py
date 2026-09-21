import unittest

from session_sync.model import (CreateRecord, CreateTombstone, Problem, ReplaceRecord, RetireRecord, RetireTmp,
                                RetireTombstone)
from session_sync.planner import plan
from tests.helpers import NOW, X, copy, snapshot, state, unreadable

DELETED_AT = NOW - 1_000


def planned(snapshots, sync_state=None, live=()):
    return plan(snapshots, sync_state or state(), live=set(live))


class DeleteWins(unittest.TestCase):
    """R6."""

    def test_a_stale_copy_is_retired_and_then_the_tombstone_travels(self):
        result = planned([snapshot("A", {X: copy("v1", activity=DELETED_AT - 5)}),
                          snapshot("B", tombstones={X: DELETED_AT})],
                         state(agreed={X: "v1"}, seen={"A": {X}, "B": {X}}))

        self.assertEqual(result.actions, [RetireRecord(X, target="A"),
                                          CreateTombstone(X, source="B", target="A")])
        self.assertEqual(result.problems, [])

    def test_a_click_on_the_stale_copy_does_not_undo_the_delete(self):
        # the clicked copy has a newer file and even a new
        # state, but nobody used the session after it was deleted.
        result = planned([snapshot("A", {X: copy("clicked", activity=DELETED_AT - 5)}),
                          snapshot("B", tombstones={X: DELETED_AT})], state(agreed={X: "v1"}))

        self.assertEqual(result.actions[0], RetireRecord(X, target="A"))

    def test_a_live_copy_waits_and_gets_no_tombstone_beside_it(self):
        result = planned([snapshot("A", {X: copy("v1", activity=DELETED_AT - 5)}),
                          snapshot("B", tombstones={X: DELETED_AT})], live={"A"})

        self.assertEqual(result.actions, [])
        self.assertEqual(result.problems, [Problem("live", X, "A")])

    def test_activity_in_the_same_millisecond_as_the_delete_is_not_after_it(self):
        result = planned([snapshot("A", {X: copy("v1", activity=DELETED_AT)}),
                          snapshot("B", tombstones={X: DELETED_AT})])

        self.assertEqual(result.actions[0], RetireRecord(X, target="A"))

    def test_an_orphaned_temp_file_is_retired_so_the_app_cannot_promote_it(self):
        result = planned([snapshot("A", tombstones={X: DELETED_AT}, orphan_tmps={X}),
                          snapshot("B", tombstones={X: DELETED_AT})])

        self.assertEqual(result.actions, [RetireTmp(X, target="A")])

    def test_a_tombstone_with_no_record_anywhere_is_created_where_missing_even_if_live(self):
        result = planned([snapshot("A"), snapshot("B", tombstones={X: DELETED_AT})], live={"A"})

        self.assertEqual(result.actions, [CreateTombstone(X, source="B", target="A")])

    def test_an_unreadable_copy_freezes_a_delete_as_well(self):
        result = planned([snapshot("A", {X: unreadable()}), snapshot("B", tombstones={X: DELETED_AT})])

        self.assertEqual(result.actions, [])
        self.assertEqual(result.problems, [Problem("unreadable", X, "A")])


class RecordWins(unittest.TestCase):
    """R6."""

    def test_a_session_used_after_the_delete_comes_back_and_the_tombstone_is_retired(self):
        result = planned([snapshot("A", {X: copy("v2", activity=DELETED_AT + 5)}),
                          snapshot("B", tombstones={X: DELETED_AT})],
                         state(agreed={X: "v1"}, seen={"A": {X}, "B": {X}}))

        self.assertEqual(result.actions, [CreateRecord(X, source="A", target="B"),
                                          RetireTombstone(X, target="B")])
        self.assertEqual(result.problems, [], "a tombstone explains the absence, so this is not a lost record")

    def test_a_stale_copy_in_a_newly_enrolled_partition_does_not_undo_a_finished_delete(self):
        # Only time decides. A re-adopted session is stamped with the current time (F10),
        # so nothing has to remember that a delete finished, and nothing can remember it wrongly.
        result = planned([snapshot("A", tombstones={X: DELETED_AT}), snapshot("B", tombstones={X: DELETED_AT}),
                          snapshot("C", {X: copy("v1", activity=DELETED_AT - 500)})])

        self.assertEqual(result.actions, [RetireRecord(X, target="C"), CreateTombstone(X, source="A", target="C")])

    def test_a_stale_tombstone_in_a_live_partition_waits(self):
        result = planned([snapshot("A", {X: copy("v2", activity=DELETED_AT + 5)}),
                          snapshot("B", tombstones={X: DELETED_AT})], live={"B"})

        self.assertEqual(result.actions, [CreateRecord(X, source="A", target="B")])
        self.assertEqual(result.problems, [Problem("live", X, "B")])

    def test_differing_copies_still_converge_when_the_record_wins(self):
        result = planned([snapshot("A", {X: copy("v2", activity=DELETED_AT + 5), }, tombstones={X: DELETED_AT}),
                          snapshot("B", {X: copy("v1", activity=DELETED_AT - 5)})], state(agreed={X: "v1"}))

        self.assertEqual(result.actions, [ReplaceRecord(X, source="A", target="B", keep=False),
                                          RetireTombstone(X, target="A")])


if __name__ == "__main__":
    unittest.main()
