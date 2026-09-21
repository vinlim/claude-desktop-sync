import os
import stat
import unittest

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
        scans = {str(p): scan_partition(p, {}, now_ns=NOW_NS) for p in (self.box.a, self.box.b)}
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
        scans = {self.A: scan_partition(self.box.a, poisoned, now_ns=NOW_NS),
                 self.B: scan_partition(self.box.b, {}, now_ns=NOW_NS)}
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
        # The reviewer's M2(a): liveness is asked again at the moment of the write.
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
