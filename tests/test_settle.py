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
    def test_a_tombstone_everywhere_and_a_record_nowhere_is_a_finished_delete(self):
        after = settle(state(agreed={X: "v1"}, seen={"A": {X}, "B": {X}}),
                       [snapshot("A", tombstones={X: NOW}), snapshot("B", tombstones={X: NOW})])

        self.assertEqual(after.deleted, {X})
        self.assertEqual(after.agreed, {})
        self.assertEqual(after.seen, {"A": set(), "B": set()},
                         "a finished delete must not make a later re-creation look lost")

    def test_a_delete_that_has_not_reached_every_partition_is_not_finished(self):
        after = settle(state(), [snapshot("A", {X: copy("v1")}), snapshot("B", tombstones={X: NOW})])

        self.assertEqual(after.deleted, set())

    def test_a_re_creation_is_complete_only_when_no_tombstone_remains(self):
        everywhere = [snapshot("A", {X: copy("v1")}), snapshot("B", {X: copy("v1")}, tombstones={X: NOW})]
        cleared = [snapshot("A", {X: copy("v1")}), snapshot("B", {X: copy("v1")})]

        self.assertEqual(settle(state(deleted={X}), everywhere).deleted, {X},
                         "with a stale tombstone left, forgetting the delete would retire the new record next run")
        self.assertEqual(settle(state(deleted={X}), cleared).deleted, set())


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
