import json
import os
import time
import unittest
from unittest import mock

from session_sync.enrolment import enrol
from session_sync.run import RunAborted, Settings, sync
from session_sync.state_store import load_state
from tests.fs_helpers import (ACCOUNT_A, ACCOUNT_B, LONG_AGO_S, SECOND_NS, Sandbox, X, Y, title_of, write_record,
                              write_tombstone)

NOW_S = LONG_AGO_S + 100_000
NOW_MS = NOW_S * 1000


class RunTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)
        self.settings = Settings(state_dir=self.box.state_dir)
        enrol(self.settings.config_path, self.box.a)
        enrol(self.settings.config_path, self.box.b)
        self.running = False
        self.clock_s = NOW_S

    def sync(self, apply=True, prefer=None):
        self.clock_s += 10
        return sync(self.settings, apply=apply, prefer=prefer, now_ns=lambda: self.clock_s * SECOND_NS,
                    running=lambda: self.running)

    def names(self, directory):
        return sorted(p.name for p in directory.iterdir())

    def kept(self):
        root = self.settings.kept_root
        return sorted(p.name for p in root.rglob("*") if p.is_file()) if root.exists() else []

    def in_sync(self):
        report = self.sync(apply=False)
        return not report.planned and not report.problems


class FirstContactAndSteadyState(RunTest):
    def test_each_side_receives_what_it_lacks_and_a_second_run_has_nothing_to_do(self):
        write_record(self.box.a, X)
        write_record(self.box.b, Y)

        first = self.sync()

        self.assertEqual(self.names(self.box.a), self.names(self.box.b))
        self.assertEqual([o.problem for o in first.outcomes], [None, None])
        self.assertTrue(self.in_sync())
        self.assertEqual(set(load_state(self.settings.state_path).sync.agreed), {X, Y})

    def test_a_dry_run_writes_nothing_anywhere(self):
        write_record(self.box.a, X)
        before = self.names(self.box.state_dir)

        report = self.sync(apply=False)

        self.assertEqual(len(report.planned), 1)
        self.assertEqual(self.names(self.box.b), [])
        self.assertEqual(self.names(self.box.state_dir), before)


class ReviewerScenarios(RunTest):
    def synced(self, **fields):
        write_record(self.box.a, X, activity=100, title="agreed", **fields)
        self.sync()
        self.assertTrue(self.in_sync())

    def test_p1_a_clicked_stale_copy_does_not_overwrite_real_work(self):
        self.synced()
        write_record(self.box.b, X, at_s=LONG_AGO_S + 50, activity=500, title="real work under B",
                     cliSessionId="rotated")
        write_record(self.box.a, X, at_s=LONG_AGO_S + 90, activity=100, title="agreed", lastFocusedAt=777)

        self.sync()

        self.assertEqual(title_of(self.box.a, X), "real work under B")
        self.assertEqual(title_of(self.box.b, X), "real work under B")
        self.assertEqual(self.kept(), [], "the clicked copy held nothing beyond the agreed state")

    def test_real_work_on_both_sides_keeps_the_losing_copy(self):
        self.synced()
        write_record(self.box.a, X, at_s=LONG_AGO_S + 50, activity=300, title="work under A")
        write_record(self.box.b, X, at_s=LONG_AGO_S + 40, activity=900, title="later work under B")

        self.sync()

        self.assertEqual(title_of(self.box.a, X), "later work under B")
        kept = list(self.settings.kept_root.rglob("local_%s.json" % X))
        self.assertEqual([json.loads(p.read_text())["title"] for p in kept], ["work under A"])

    def test_p9_a_click_does_not_undo_a_delete(self):
        self.synced()
        (self.box.b / ("local_%s.json" % X)).unlink()
        write_tombstone(self.box.b, X, deleted_at_ms=NOW_MS)
        write_record(self.box.a, X, at_s=NOW_S + 5, activity=100, title="agreed", lastFocusedAt=NOW_MS + 5000)

        self.sync()
        self.sync()

        self.assertEqual(self.names(self.box.a), ["deleted_%s" % X])
        self.assertEqual(self.names(self.box.b), ["deleted_%s" % X])
        self.assertEqual(self.kept(), ["local_%s.json" % X])
        self.assertIn(X, load_state(self.settings.state_path).sync.deleted)

    def test_p6_a_record_removed_before_its_tombstone_is_written_is_not_put_back(self):
        self.synced()
        self.running = True
        self.box.logged_in_as(ACCOUNT_B)
        (self.box.b / ("local_%s.json" % X)).unlink()

        report = self.sync()

        self.assertEqual(self.names(self.box.b), [])
        self.assertEqual([(p.kind, p.session_id) for p in report.problems], [("lost", X)])

        write_tombstone(self.box.b, X, deleted_at_ms=NOW_MS)
        self.sync()
        self.assertEqual(self.names(self.box.a), ["deleted_%s" % X])

    def test_a_re_imported_session_survives_the_old_tombstone(self):
        self.synced()
        for side in (self.box.a, self.box.b):
            (side / ("local_%s.json" % X)).unlink()
            write_tombstone(side, X, deleted_at_ms=NOW_MS)
        self.sync()
        (self.box.a / ("deleted_%s" % X)).unlink()
        write_record(self.box.a, X, activity=50, title="re-imported, with old timestamps")

        self.sync()

        self.assertEqual(self.names(self.box.a), ["local_%s.json" % X])
        self.assertEqual(self.names(self.box.b), ["local_%s.json" % X])
        self.assertTrue(self.in_sync())

    def test_prefer_settles_a_tie(self):
        self.synced()
        write_record(self.box.a, X, at_s=LONG_AGO_S + 50, activity=100, title="renamed under A")
        write_record(self.box.b, X, at_s=LONG_AGO_S + 60, activity=100, isArchived=True, title="agreed")

        tied = self.sync()
        self.assertEqual({p.kind for p in tied.problems}, {"tied"})
        self.assertEqual(title_of(self.box.b, X), "agreed")

        self.sync(prefer=str(self.box.a))
        self.assertEqual(title_of(self.box.b, X), "renamed under A")
        self.assertEqual(len(self.kept()), 1)


class LivePartitions(RunTest):
    def test_the_live_partition_gains_new_records_but_keeps_its_own_versions(self):
        write_record(self.box.a, X, activity=100, title="agreed")
        self.sync()
        self.running = True
        self.box.logged_in_as(ACCOUNT_B)
        write_record(self.box.a, X, at_s=LONG_AGO_S + 50, activity=900, title="changed under A")
        write_record(self.box.a, Y, title="new under A")

        report = self.sync()

        self.assertEqual(title_of(self.box.b, X), "agreed")
        self.assertEqual(title_of(self.box.b, Y), "new under A")
        self.assertEqual([(p.kind, p.session_id) for p in report.problems], [("live", X)])

    def test_liveness_is_probed_again_at_the_moment_of_each_guarded_write(self):
        write_record(self.box.a, X, activity=100, title="agreed")
        self.sync()
        write_record(self.box.a, X, at_s=LONG_AGO_S + 50, activity=900, title="changed under A")
        self.box.logged_in_as(ACCOUNT_B)
        probes = []

        def app_starts_after_the_plan():
            probes.append(1)
            return len(probes) > 1

        report = sync(self.settings, apply=True, now_ns=lambda: (NOW_S + 500) * SECOND_NS,
                      running=app_starts_after_the_plan)

        self.assertEqual(title_of(self.box.b, X), "agreed")
        self.assertIn("live", report.outcomes[0].problem)


class SafetyNets(RunTest):
    def test_too_few_or_missing_partitions_stop_the_run(self):
        write_record(self.box.a, X)
        os.rename(self.box.b, self.box.b.parent / "moved-away")

        with self.assertRaises(RunAborted) as raised:
            self.sync()

        self.assertIn(str(self.box.b), str(raised.exception))
        self.assertEqual(self.names(self.box.a), ["local_%s.json" % X])

    def test_one_enrolled_partition_is_not_enough(self):
        from session_sync.enrolment import unenrol
        unenrol(self.settings.config_path, self.box.b)

        with self.assertRaises(RunAborted):
            self.sync()

    def test_an_unusable_state_file_stops_the_run_before_any_write(self):
        write_record(self.box.a, X)
        self.settings.state_path.write_text("{")

        with self.assertRaises(RunAborted) as raised:
            self.sync()

        self.assertIn("--reset-state", str(raised.exception))
        self.assertEqual(self.names(self.box.b), [])

    def test_intents_are_on_disk_before_the_first_write(self):
        write_record(self.box.a, X)
        with mock.patch("session_sync.run.Applier.apply", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.sync()

        self.assertEqual(load_state(self.settings.state_path).sync.placing, {str(self.box.b): {X}})

    def test_an_unenrolled_partition_with_records_is_reported_and_untouched(self):
        third = self.box.partition("cccccccc-0000-4000-8000-000000000003", "cccccccc-0000-4000-8000-0000000000c3")
        write_record(third, Y)
        write_record(self.box.a, X)

        report = self.sync()

        self.assertEqual(report.unenrolled, {third: 1})
        self.assertEqual(self.names(third), ["local_%s.json" % Y])
        self.assertNotIn("local_%s.json" % Y, self.names(self.box.a))

    def test_stale_temp_files_are_swept_and_fresh_ones_left(self):
        stale = self.box.a / ".sync-1-local_x.json.part"
        fresh = self.box.a / ".sync-2-local_y.json.part"
        for path, age_s in ((stale, 3600), (fresh, 5)):
            path.write_text("x")
            os.utime(path, ns=((self.clock_s - age_s) * SECOND_NS,) * 2)

        self.sync()

        self.assertEqual(self.names(self.box.a), [fresh.name])

    def test_a_clean_run_records_its_time_and_prunes_old_kept_copies(self):
        old = self.settings.kept_root / "20200101-000000" / "x"
        old.mkdir(parents=True)
        (old / "local_old.json").write_text("{}")
        os.utime(old.parent, ns=((self.clock_s - 40 * 86400) * SECOND_NS,) * 2)
        write_record(self.box.a, X)

        self.sync()

        self.assertFalse(old.parent.exists())
        self.assertEqual(load_state(self.settings.state_path).last_success_ms, self.clock_s * 1000)

    def test_a_run_with_failures_neither_prunes_nor_claims_success(self):
        old = self.settings.kept_root / "20200101-000000"
        old.mkdir(parents=True)
        os.utime(old, ns=((self.clock_s - 40 * 86400) * SECOND_NS,) * 2)
        write_record(self.box.a, X)
        os.chmod(self.box.b, 0o500)
        self.addCleanup(os.chmod, self.box.b, 0o700)

        report = self.sync()

        self.assertTrue(report.failures)
        self.assertTrue(old.exists())
        self.assertEqual(load_state(self.settings.state_path).last_success_ms, 0)


if __name__ == "__main__":
    unittest.main()
