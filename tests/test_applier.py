import os
import stat
import unittest
from unittest import mock

from session_sync.applier import Applier
from session_sync.model import (CreateRecord, CreateTombstone, Plan, ReplaceRecord, RetireRecord, RetireTmp,
                                RetireTombstone)
from session_sync.scanner import scan_partition
from tests.fs_helpers import LONG_AGO_S, SECOND_NS, Sandbox, X, Y, title_of, write_record, write_tombstone

NOW_NS = (LONG_AGO_S + 3600) * SECOND_NS


class ApplierTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)
        self.kept = self.box.state_dir / "kept" / "run"
        self.live = set()
        self.A, self.B = str(self.box.a), str(self.box.b)

    def apply(self, *actions):
        """Scans now, so tests can change files afterwards to stand in for the app."""
        self.applier = self.prepare()
        return self.applier.apply(Plan(actions=list(actions)))

    def prepare(self):
        scans = {str(p): scan_partition(p, {}, clock=lambda: NOW_NS) for p in (self.box.a, self.box.b)}
        return Applier(scans, is_live=lambda partition: str(partition) in self.live, kept_dir=self.kept)

    def problems(self, outcomes):
        return [outcome.problem for outcome in outcomes]

    def kept_files(self):
        return sorted(str(p.relative_to(self.kept)) for p in self.kept.rglob("*") if p.is_file()) if self.kept.exists() else []

    def names(self, directory):
        return sorted(p.name for p in directory.iterdir())


class CreatingARecord(ApplierTest):
    def test_the_copy_is_verbatim_private_and_keeps_the_sources_time(self):
        source = write_record(self.box.a, X, at_s=LONG_AGO_S + 7)

        outcomes = self.apply(CreateRecord(X, source=self.A, target=self.B))

        copied = self.box.b / source.name
        self.assertEqual(self.problems(outcomes), [None])
        self.assertEqual(copied.read_bytes(), source.read_bytes())
        self.assertEqual(os.lstat(copied).st_mtime_ns, (LONG_AGO_S + 7) * SECOND_NS)
        self.assertEqual(stat.S_IMODE(os.lstat(copied).st_mode), 0o600)
        self.assertEqual(os.lstat(copied).st_nlink, 1)
        self.assertEqual(self.names(self.box.b), [source.name])

    def test_a_file_that_appeared_at_the_target_is_never_overwritten(self):
        write_record(self.box.a, X, title="from A")
        applier = self.prepare()
        write_record(self.box.b, X, title="the app made this meanwhile")

        outcomes = applier.apply(Plan(actions=[CreateRecord(X, source=self.A, target=self.B)]))

        self.assertIn("appeared", outcomes[0].problem)
        self.assertEqual(title_of(self.box.b, X), "the app made this meanwhile")

    def test_a_source_that_changed_after_the_scan_is_not_copied(self):
        write_record(self.box.a, X, title="planned")
        applier = self.prepare()
        write_record(self.box.a, X, at_s=LONG_AGO_S + 9, title="rewritten by the app")

        outcomes = applier.apply(Plan(actions=[CreateRecord(X, source=self.A, target=self.B)]))

        self.assertIn("source changed", outcomes[0].problem)
        self.assertEqual(self.names(self.box.b), [])

    def test_bytes_that_are_not_the_version_the_planner_chose_are_not_copied(self):
        # A cache entry can match a file's time and size and still describe other bytes.
        path = write_record(self.box.a, X, title="what is really on disk")
        info = os.lstat(path)
        poisoned = {X: (info.st_mtime_ns, info.st_size, "hash-of-some-other-version", 100)}
        scans = {self.A: scan_partition(self.box.a, poisoned, clock=lambda: NOW_NS),
                 self.B: scan_partition(self.box.b, {}, clock=lambda: NOW_NS)}
        applier = Applier(scans, is_live=lambda partition: False, kept_dir=self.kept)

        outcomes = applier.apply(Plan(actions=[CreateRecord(X, source=self.A, target=self.B)]))

        self.assertIn("source changed", outcomes[0].problem)
        self.assertEqual(self.names(self.box.b), [])


class ReplacingARecord(ApplierTest):
    def test_the_target_takes_the_sources_bytes(self):
        write_record(self.box.a, X, title="new")
        write_record(self.box.b, X, title="old")

        outcomes = self.apply(ReplaceRecord(X, source=self.A, target=self.B, keep=False))

        self.assertEqual(self.problems(outcomes), [None])
        self.assertEqual(title_of(self.box.b, X), "new")
        self.assertEqual(self.kept_files(), [], "a copy equal to the agreed state holds nothing unique")

    def test_a_copy_with_unique_state_is_kept_before_it_is_replaced(self):
        write_record(self.box.a, X, title="winner")
        loser = write_record(self.box.b, X, title="loser").read_bytes()

        self.apply(ReplaceRecord(X, source=self.A, target=self.B, keep=True))

        kept = self.kept / ("%s_%s" % (self.box.b.parent.name, self.box.b.name)) / ("local_%s.json" % X)
        self.assertEqual(kept.read_bytes(), loser)
        self.assertEqual(stat.S_IMODE(os.lstat(kept).st_mode), 0o600)

    def test_a_partition_that_became_live_is_left_alone(self):
        # liveness is asked again at the moment of the write.
        write_record(self.box.a, X, title="new")
        write_record(self.box.b, X, title="old")
        applier = self.prepare()
        self.live.add(self.B)

        outcomes = applier.apply(Plan(actions=[ReplaceRecord(X, source=self.A, target=self.B, keep=True)]))

        self.assertIn("live", outcomes[0].problem)
        self.assertEqual(title_of(self.box.b, X), "old")
        self.assertEqual(self.kept_files(), [])

    def test_a_target_the_app_rewrote_after_the_scan_is_left_alone(self):
        write_record(self.box.a, X, title="new")
        write_record(self.box.b, X, title="old")
        applier = self.prepare()
        write_record(self.box.b, X, at_s=LONG_AGO_S + 9, title="app saved meanwhile")

        outcomes = applier.apply(Plan(actions=[ReplaceRecord(X, source=self.A, target=self.B, keep=True)]))

        self.assertIn("target changed", outcomes[0].problem)
        self.assertEqual(title_of(self.box.b, X), "app saved meanwhile")


class GuardTiming(ApplierTest):
    def test_the_last_guard_runs_after_the_new_bytes_are_staged(self):
        # Writing and syncing the staged file takes milliseconds. A guard before that
        # would miss a save the app makes meanwhile.
        write_record(self.box.a, X, title="new")
        write_record(self.box.b, X, title="old")
        applier = self.prepare()
        import session_sync.applier as module
        real_stage = module.stage

        def app_saves_while_we_stage(destination, data, mtime_ns=None):
            staged = real_stage(destination, data, mtime_ns)
            write_record(self.box.b, X, at_s=LONG_AGO_S + 9, title="app saved meanwhile")
            return staged

        with mock.patch.object(module, "stage", app_saves_while_we_stage):
            outcomes = applier.apply(Plan(actions=[ReplaceRecord(X, source=self.A, target=self.B, keep=False)]))

        self.assertIn("target changed", outcomes[0].problem)
        self.assertEqual(title_of(self.box.b, X), "app saved meanwhile")
        self.assertEqual(self.names(self.box.b), ["local_%s.json" % X], "the staged file is discarded")

    def test_a_file_that_appears_between_the_check_and_the_link_is_still_never_overwritten(self):
        write_record(self.box.a, X, title="from A")
        applier = self.prepare()
        import session_sync.applier as module
        real_stage = module.stage

        def app_creates_it_while_we_stage(destination, data, mtime_ns=None):
            staged = real_stage(destination, data, mtime_ns)
            write_record(self.box.b, X, title="the app made this meanwhile")
            return staged

        with mock.patch.object(module, "stage", app_creates_it_while_we_stage):
            outcomes = applier.apply(Plan(actions=[CreateRecord(X, source=self.A, target=self.B)]))

        self.assertIn("appeared", outcomes[0].problem)
        self.assertEqual(title_of(self.box.b, X), "the app made this meanwhile")
        self.assertEqual(self.names(self.box.b), ["local_%s.json" % X])

    def test_being_stopped_in_the_middle_of_the_last_guard_leaves_no_staged_file(self):
        # The guard runs pgrep, so a SIGTERM from launchd can land right there.
        write_record(self.box.a, X, title="new")
        write_record(self.box.b, X, title="old")
        scans = {str(p): scan_partition(p, {}, clock=lambda: NOW_NS) for p in (self.box.a, self.box.b)}
        calls = []

        def stopped_on_the_second_probe(partition):
            calls.append(partition)
            if len(calls) == 2:
                raise SystemExit(143)
            return False

        applier = Applier(scans, is_live=stopped_on_the_second_probe, kept_dir=self.kept)

        with self.assertRaises(SystemExit):
            applier.apply(Plan(actions=[ReplaceRecord(X, source=self.A, target=self.B, keep=False)]))

        self.assertEqual(self.names(self.box.b), ["local_%s.json" % X])
        self.assertEqual(title_of(self.box.b, X), "old")

    def test_a_refused_action_writes_nothing_so_a_watched_directory_does_not_refire(self):
        write_record(self.box.a, X)
        write_record(self.box.a, Y, title="new")
        applier = self.prepare()
        write_record(self.box.b, X, title="appeared")
        write_record(self.box.b, Y, at_s=LONG_AGO_S + 9, title="changed")
        before = os.lstat(self.box.b).st_mtime_ns

        outcomes = applier.apply(Plan(actions=[CreateRecord(X, source=self.A, target=self.B),
                                               ReplaceRecord(Y, source=self.A, target=self.B, keep=True)]))

        self.assertTrue(all(outcome.problem for outcome in outcomes))
        self.assertEqual(os.lstat(self.box.b).st_mtime_ns, before)
        self.assertEqual(self.kept_files(), [])


class KeptCopies(ApplierTest):
    def test_the_outcome_says_where_the_replaced_copy_went(self):
        write_record(self.box.a, X, title="winner")
        write_record(self.box.b, X, title="loser")
        write_record(self.box.a, Y)

        replaced, created = self.apply(ReplaceRecord(X, source=self.A, target=self.B, keep=True),
                                       CreateRecord(Y, source=self.A, target=self.B))

        self.assertIn('"loser"', replaced.kept.read_text())
        self.assertIsNone(created.kept)

    def test_two_kept_versions_of_one_file_never_overwrite_each_other(self):
        write_record(self.box.a, X, title="winner")
        write_record(self.box.b, X, title="first loser")
        self.apply(ReplaceRecord(X, source=self.A, target=self.B, keep=True))
        write_record(self.box.b, X, at_s=LONG_AGO_S + 9, title="second loser")

        self.apply(ReplaceRecord(X, source=self.A, target=self.B, keep=True))

        kept = [p.read_text() for p in sorted(self.kept.rglob("local_*")) if p.is_file()]
        self.assertEqual(len(kept), 2)
        self.assertTrue(any("first loser" in text for text in kept) and any("second loser" in text for text in kept))


class RetiringARecord(ApplierTest):
    def test_the_record_and_its_temp_sibling_are_moved_aside_not_destroyed(self):
        record = write_record(self.box.a, X).read_bytes()
        (self.box.a / ("local_%s.json.tmp" % X)).write_text("half a save")

        outcomes = self.apply(RetireRecord(X, target=self.A))

        self.assertEqual(self.problems(outcomes), [None])
        self.assertEqual(self.names(self.box.a), [])
        prefix = "%s_%s/" % (self.box.a.parent.name, self.box.a.name)
        self.assertEqual(self.kept_files(), [prefix + "local_%s.json" % X, prefix + "local_%s.json.tmp" % X])
        self.assertEqual((self.kept / prefix / ("local_%s.json" % X)).read_bytes(), record)

    def test_a_temp_file_that_appears_while_the_record_is_being_kept_stops_the_retirement(self):
        # An app save cut short leaves local_X.json.tmp. Left alone beside a retired record,
        # the app would promote it at its next start, tombstone or not.
        write_record(self.box.a, X)
        applier = self.prepare()
        real_keep = applier._keep

        def keep_while_the_app_starts_a_save(partition, path):
            kept = real_keep(partition, path)
            (self.box.a / ("local_%s.json.tmp" % X)).write_text("a save cut short")
            return kept

        applier._keep = keep_while_the_app_starts_a_save

        outcomes = applier.apply(Plan(actions=[RetireRecord(X, target=self.A)]))

        self.assertIn("temp file appeared", outcomes[0].problem)
        self.assertEqual(self.names(self.box.a), ["local_%s.json" % X, "local_%s.json.tmp" % X])

    def test_live_or_changed_records_stay(self):
        write_record(self.box.a, X)
        applier = self.prepare()
        self.live.add(self.A)
        self.assertIn("live", applier.apply(Plan(actions=[RetireRecord(X, target=self.A)]))[0].problem)

        self.live.clear()
        write_record(self.box.a, X, at_s=LONG_AGO_S + 9, title="app saved meanwhile")
        self.assertIn("target changed", applier.apply(Plan(actions=[RetireRecord(X, target=self.A)]))[0].problem)
        self.assertEqual(self.names(self.box.a), ["local_%s.json" % X])

    def test_an_orphaned_temp_file_is_moved_aside(self):
        (self.box.a / ("local_%s.json.tmp" % X)).write_text("orphan")

        self.apply(RetireTmp(X, target=self.A))

        self.assertEqual(self.names(self.box.a), [])
        self.assertEqual(len(self.kept_files()), 1)


class Tombstones(ApplierTest):
    def test_a_tombstone_is_copied_verbatim_with_its_time(self):
        source = write_tombstone(self.box.b, X, 1234, at_s=LONG_AGO_S + 3)

        outcomes = self.apply(CreateTombstone(X, source=self.B, target=self.A))

        copied = self.box.a / source.name
        self.assertEqual(self.problems(outcomes), [None])
        self.assertEqual(copied.read_text(), "1234")
        self.assertEqual(os.lstat(copied).st_mtime_ns, (LONG_AGO_S + 3) * SECOND_NS)

    def test_a_tombstone_already_there_is_fine(self):
        write_tombstone(self.box.b, X, 1234)
        applier = self.prepare()
        write_tombstone(self.box.a, X, 9999)

        outcomes = applier.apply(Plan(actions=[CreateTombstone(X, source=self.B, target=self.A)]))

        self.assertEqual(self.problems(outcomes), [None])
        self.assertEqual((self.box.a / ("deleted_%s" % X)).read_text(), "9999")

    def test_a_stale_tombstone_is_moved_aside_unless_live(self):
        write_tombstone(self.box.b, X, 1234)

        self.apply(RetireTombstone(X, target=self.B))
        self.assertEqual(self.names(self.box.b), [])
        self.assertEqual(len(self.kept_files()), 1)

        write_tombstone(self.box.b, Y, 1234)
        applier = self.prepare()
        self.live.add(self.B)
        self.assertIn("live", applier.apply(Plan(actions=[RetireTombstone(Y, target=self.B)]))[0].problem)


class Ordering(ApplierTest):
    def test_a_failed_step_stops_the_rest_of_that_session_only(self):
        # Record wins: the tombstone may go only once the record is safely in place.
        write_record(self.box.a, X)
        write_record(self.box.a, Y)
        write_tombstone(self.box.b, X, 1234)
        applier = self.prepare()
        write_record(self.box.b, X, title="appeared")

        outcomes = applier.apply(Plan(actions=[CreateRecord(X, source=self.A, target=self.B),
                                               RetireTombstone(X, target=self.B),
                                               CreateRecord(Y, source=self.A, target=self.B)]))

        self.assertIn("appeared", outcomes[0].problem)
        self.assertIn("earlier step", outcomes[1].problem)
        self.assertIsNone(outcomes[2].problem)
        self.assertTrue((self.box.b / ("deleted_%s" % X)).exists())

    def test_an_unexpected_os_error_is_reported_and_the_run_continues(self):
        write_record(self.box.a, X)
        write_record(self.box.a, Y)
        applier = self.prepare()
        os.chmod(self.box.b, 0o500)
        self.addCleanup(os.chmod, self.box.b, 0o700)

        outcomes = applier.apply(Plan(actions=[CreateRecord(X, source=self.A, target=self.B),
                                               CreateRecord(Y, source=self.A, target=self.B)]))

        self.assertTrue(all(outcome.problem and "PermissionError" in outcome.problem for outcome in outcomes))


if __name__ == "__main__":
    unittest.main()
