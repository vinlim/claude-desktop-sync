import unittest

from session_sync.settle import settle
from tests.helpers import NOW, X, Y, copy, snapshot, state, unreadable


class Agreement(unittest.TestCase):
    def test_identical_copies_everywhere_become_the_agreed_state(self):
        after = settle(state(), [snapshot("A", {X: copy("v2")}), snapshot("B", {X: copy("v2")})])

        self.assertEqual(after.agreed, {X: "v2"})

    def test_differing_copies_leave_the_agreed_state_where_it_was(self):
        after = settle(state(agreed={X: "v1"}), [snapshot("A", {X: copy("v1")}), snapshot("B", {X: copy("v2")})])

        self.assertEqual(after.agreed, {X: "v1"})

    def test_a_partition_without_the_record_means_no_agreement_yet(self):
        after = settle(state(), [snapshot("A", {X: copy("v1")}), snapshot("B")])

        self.assertEqual(after.agreed, {})

    def test_an_unreadable_copy_means_no_agreement(self):
        after = settle(state(), [snapshot("A", {X: unreadable()}), snapshot("B", {X: unreadable()})])

        self.assertEqual(after.agreed, {})

    def test_the_given_state_is_not_modified(self):
        before = state(agreed={X: "v1"})

        settle(before, [snapshot("A", {X: copy("v2")}), snapshot("B", {X: copy("v2")})])

        self.assertEqual(before.agreed, {X: "v1"})


class FinishedDeletes(unittest.TestCase):
    def test_a_finished_delete_is_forgotten_so_a_later_re_adoption_does_not_look_lost(self):
        after = settle(state(agreed={X: "v1"}, seen={"A": {X}, "B": {X}}),
                       [snapshot("A", tombstones={X: NOW}), snapshot("B", tombstones={X: NOW})])

        self.assertEqual(after.agreed, {})
        self.assertEqual(after.seen, {"A": set(), "B": set()})

    def test_a_delete_still_on_its_way_keeps_what_is_known(self):
        after = settle(state(agreed={X: "v1"}, seen={"A": {X}, "B": {X}}),
                       [snapshot("A", {X: copy("v1")}), snapshot("B", tombstones={X: NOW})])

        self.assertEqual(after.agreed, {X: "v1"})
        self.assertEqual(after.seen, {"A": {X}, "B": {X}})


class RememberedPlacements(unittest.TestCase):
    """R3: a version the tool placed counts as unchanged only while it is still exactly that."""

    def test_a_placement_waiting_for_the_rest_to_catch_up_is_remembered(self):
        after = settle(state(agreed={X: "v0"}, placed={"B": {X: "v1"}}),
                       [snapshot("A", {X: copy("v1")}), snapshot("B", {X: copy("v1")}), snapshot("C", {X: copy("v0")})])

        self.assertEqual(after.placed, {"B": {X: "v1"}})

    def test_a_placement_is_forgotten_once_it_is_no_longer_needed_or_no_longer_true(self):
        cases = {
            "everyone agrees now": [snapshot("A", {X: copy("v1")}), snapshot("B", {X: copy("v1")})],
            "the app changed the copy": [snapshot("A", {X: copy("v1")}), snapshot("B", {X: copy("edited")})],
            "the copy is gone": [snapshot("A", {X: copy("v1")}), snapshot("B")],
        }
        for name, snapshots in cases.items():
            with self.subTest(case=name):
                after = settle(state(agreed={X: "v0"}, placed={"B": {X: "v1"}}), snapshots)

                self.assertEqual(after.placed, {})


class Presence(unittest.TestCase):
    def test_observed_records_are_remembered_per_partition(self):
        after = settle(state(seen={"A": {X}}), [snapshot("A", {X: copy("v1"), Y: copy("v1")}), snapshot("B")])

        self.assertEqual(after.seen, {"A": {X, Y}, "B": set()})

    def test_a_record_missing_from_one_partition_stays_remembered_there(self):
        after = settle(state(seen={"A": {X}, "B": {X}}), [snapshot("A", {X: copy("v1")}), snapshot("B")])

        self.assertEqual(after.seen["B"], {X})

    def test_a_record_gone_from_every_partition_is_forgotten(self):
        after = settle(state(seen={"A": {X}, "B": {X}}), [snapshot("A"), snapshot("B")])

        self.assertEqual(after.seen, {"A": set(), "B": set()})

    def test_placing_intents_last_one_run(self):
        after = settle(state(placing={"B": {X}}), [snapshot("A", {X: copy("v1")}), snapshot("B")])

        self.assertEqual(after.placing, {})


if __name__ == "__main__":
    unittest.main()
