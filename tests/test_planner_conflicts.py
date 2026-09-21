import unittest

from session_sync.model import CreateRecord, Problem, ReplaceRecord
from session_sync.planner import plan
from tests.helpers import NOW, X, copy, snapshot, state, unreadable


def planned(snapshots, sync_state=None, live=(), prefer=None):
    return plan(snapshots, sync_state or state(), live=set(live), now_ms=NOW, prefer=prefer)


class BothSidesChanged(unittest.TestCase):
    """R4."""

    def test_the_copy_with_later_activity_wins_and_the_loser_is_kept(self):
        result = planned([snapshot("A", {X: copy("v2", activity=10)}),
                          snapshot("B", {X: copy("v3", activity=50)})], state(agreed={X: "v1"}))

        self.assertEqual(result.actions, [ReplaceRecord(X, source="B", target="A", keep=True)])

    def test_first_contact_with_differing_copies_is_decided_the_same_way(self):
        result = planned([snapshot("A", {X: copy("v1", activity=90)}),
                          snapshot("B", {X: copy("v2", activity=50)})])

        self.assertEqual(result.actions, [ReplaceRecord(X, source="A", target="B", keep=True)])

    def test_a_tie_is_left_alone_and_reported_for_every_copy(self):
        result = planned([snapshot("A", {X: copy("v2", activity=50)}),
                          snapshot("B", {X: copy("v3", activity=50)})], state(agreed={X: "v1"}))

        self.assertEqual(result.actions, [])
        self.assertEqual(result.problems, [Problem("tied", X, "A"), Problem("tied", X, "B")])

    def test_a_tie_blocks_creation_too_because_no_version_is_chosen(self):
        result = planned([snapshot("A", {X: copy("v2", activity=50)}),
                          snapshot("B", {X: copy("v3", activity=50)}), snapshot("C")])

        self.assertEqual(result.actions, [])

    def test_prefer_settles_a_tie_and_keeps_the_loser(self):
        result = planned([snapshot("A", {X: copy("v2", activity=50)}),
                          snapshot("B", {X: copy("v3", activity=50)})],
                         state(agreed={X: "v1"}), prefer="B")

        self.assertEqual(result.actions, [ReplaceRecord(X, source="B", target="A", keep=True)])
        self.assertEqual(result.problems, [])

    def test_prefer_does_not_override_a_clear_winner(self):
        result = planned([snapshot("A", {X: copy("v2", activity=90)}),
                          snapshot("B", {X: copy("v3", activity=50)})],
                         state(agreed={X: "v1"}), prefer="B")

        self.assertEqual(result.actions, [ReplaceRecord(X, source="A", target="B", keep=True)])

    def test_a_live_loser_is_never_overwritten(self):
        result = planned([snapshot("A", {X: copy("v2", activity=10)}),
                          snapshot("B", {X: copy("v3", activity=50)})],
                         state(agreed={X: "v1"}), live={"A"})

        self.assertEqual(result.actions, [])
        self.assertEqual(result.problems, [Problem("live", X, "A")])

    def test_among_three_versions_the_latest_activity_replaces_both_others(self):
        result = planned([snapshot("A", {X: copy("v2", activity=10)}),
                          snapshot("B", {X: copy("v3", activity=50)}),
                          snapshot("C", {X: copy("v1", activity=5)})], state(agreed={X: "v1"}))

        self.assertEqual(result.actions, [ReplaceRecord(X, source="B", target="A", keep=True),
                                          ReplaceRecord(X, source="B", target="C", keep=False)])

    def test_a_copy_already_equal_to_the_winner_is_left_alone(self):
        result = planned([snapshot("A", {X: copy("v3", activity=50)}),
                          snapshot("B", {X: copy("v3", activity=50)}),
                          snapshot("C", {X: copy("v2", activity=10)})], state(agreed={X: "v1"}))

        self.assertEqual(result.actions, [ReplaceRecord(X, source="A", target="C", keep=True)])


class UnreadableCopies(unittest.TestCase):
    """R11."""

    def test_an_unreadable_copy_freezes_the_session_everywhere(self):
        result = planned([snapshot("A", {X: unreadable()}), snapshot("B", {X: copy("v1")}), snapshot("C")])

        self.assertEqual(result.actions, [])
        self.assertEqual(result.problems, [Problem("unreadable", X, "A")])


class LostRecords(unittest.TestCase):
    """R7."""

    def test_a_record_seen_before_and_now_gone_without_a_tombstone_is_not_recreated(self):
        # The reviewer's P6: the app removes the record, then writes the tombstone later.
        result = planned([snapshot("A", {X: copy("v1")}), snapshot("B")],
                         state(agreed={X: "v1"}, seen={"A": {X}, "B": {X}}))

        self.assertEqual(result.actions, [])
        self.assertEqual(result.problems, [Problem("lost", X, "B")])

    def test_a_record_the_tool_was_about_to_place_counts_as_seen(self):
        result = planned([snapshot("A", {X: copy("v1")}), snapshot("B")], state(placing={"B": {X}}))

        self.assertEqual(result.actions, [])
        self.assertEqual(result.problems, [Problem("lost", X, "B")])

    def test_a_loss_in_one_partition_does_not_stop_creation_in_another(self):
        result = planned([snapshot("A", {X: copy("v1")}), snapshot("B"), snapshot("C")],
                         state(seen={"B": {X}}))

        self.assertEqual(result.actions, [CreateRecord(X, source="A", target="C")])
        self.assertEqual(result.problems, [Problem("lost", X, "B")])


if __name__ == "__main__":
    unittest.main()
