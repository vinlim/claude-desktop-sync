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

    def app_running_as(self, account):
        """The app is open under this login, and the login changed long enough ago to have settled."""
        self.running = True
        self.box.logged_in_as(account)
        self.sync()
        self.clock_s += 130

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


class HardSequences(RunTest):
    def synced(self, **fields):
        write_record(self.box.a, X, activity=100, title="agreed", **fields)
        self.sync()
        self.assertTrue(self.in_sync())

    def test_a_clicked_stale_copy_does_not_overwrite_real_work(self):
        self.synced()
        write_record(self.box.b, X, at_s=LONG_AGO_S + 50, activity=500, title="real work under B",
                     cliSessionId="rotated")
        write_record(self.box.a, X, at_s=LONG_AGO_S + 90, activity=100, title="agreed", lastFocusedAt=777)

        self.sync()

        self.assertEqual(title_of(self.box.a, X), "real work under B")
        self.assertEqual(title_of(self.box.b, X), "real work under B")
        self.assertEqual(self.kept(), [], "the clicked copy held nothing beyond the agreed state")

    def test_a_copy_that_went_back_to_an_older_state_does_not_take_the_newer_work_with_it(self):
        # Work under B reaches A. Then A's file goes back in time: a restored backup, a promoted
        # temp file, or the previous login flushing stale memory over what the tool had placed.
        self.synced()
        write_record(self.box.b, X, at_s=LONG_AGO_S + 50, activity=900, title="real work", cliSessionId="rotated")
        self.sync()
        self.assertTrue(self.in_sync())
        write_record(self.box.a, X, at_s=LONG_AGO_S + 99, activity=100, title="agreed")

        self.sync()

        self.assertEqual(title_of(self.box.b, X), "real work")
        self.assertEqual(title_of(self.box.a, X), "real work")
        kept = [json.loads(p.read_text())["title"] for p in self.settings.kept_root.rglob("local_%s.json" % X)]
        self.assertEqual(kept, ["agreed"], "the copy that went back is kept too, never just dropped")

    def test_a_record_holding_half_an_emoji_does_not_stop_the_run(self):
        # The app cuts strings by UTF-16 code unit, so a record can hold a lone surrogate.
        path = self.box.a / ("local_%s.json" % X)
        path.write_bytes(b'{"sessionId": "local_%s", "title": "done \\ud83d", "lastActivityAt": 5}' % X.encode())
        write_record(self.box.a, Y)

        report = self.sync()

        self.assertEqual(report.failures, [])
        self.assertEqual((self.box.b / path.name).read_bytes(), path.read_bytes())
        self.assertTrue((self.box.b / ("local_%s.json" % Y)).exists())

    def test_real_work_on_both_sides_keeps_the_losing_copy(self):
        self.synced()
        write_record(self.box.a, X, at_s=LONG_AGO_S + 50, activity=300, title="work under A")
        write_record(self.box.b, X, at_s=LONG_AGO_S + 40, activity=900, title="later work under B")

        self.sync()

        self.assertEqual(title_of(self.box.a, X), "later work under B")
        kept = list(self.settings.kept_root.rglob("local_%s.json" % X))
        self.assertEqual([json.loads(p.read_text())["title"] for p in kept], ["work under A"])

    def test_a_click_does_not_undo_a_delete(self):
        self.synced()
        (self.box.b / ("local_%s.json" % X)).unlink()
        write_tombstone(self.box.b, X, deleted_at_ms=NOW_MS)
        write_record(self.box.a, X, at_s=NOW_S + 5, activity=100, title="agreed", lastFocusedAt=NOW_MS + 5000)

        self.sync()
        self.sync()

        self.assertEqual(self.names(self.box.a), ["deleted_%s" % X])
        self.assertEqual(self.names(self.box.b), ["deleted_%s" % X])
        self.assertEqual(self.kept(), ["local_%s.json" % X])
        remembered = load_state(self.settings.state_path).sync
        self.assertNotIn(X, remembered.agreed)
        self.assertEqual(remembered.seen, {str(self.box.a): set(), str(self.box.b): set()})

    def test_a_record_removed_before_its_tombstone_is_written_is_not_put_back(self):
        self.synced()
        self.app_running_as(ACCOUNT_B)
        (self.box.b / ("local_%s.json" % X)).unlink()

        report = self.sync()

        self.assertEqual(self.names(self.box.b), [])
        self.assertEqual([(p.kind, p.session_id) for p in report.problems], [("lost", X)])

        write_tombstone(self.box.b, X, deleted_at_ms=NOW_MS)
        self.sync()
        self.assertEqual(self.names(self.box.a), ["deleted_%s" % X])

    def finished_delete(self):
        self.synced()
        for side in (self.box.a, self.box.b):
            (side / ("local_%s.json" % X)).unlink()
            write_tombstone(side, X, deleted_at_ms=NOW_MS)
        self.sync()

    def re_adopt_under_a(self, at_ms):
        """What the app does (F10): drop its own tombstone, stamp the new record with the current time."""
        (self.box.a / ("deleted_%s" % X)).unlink()
        write_record(self.box.a, X, activity=at_ms, title="re-adopted")
        self.clock_s = max(self.clock_s, at_ms // 1000)

    def test_a_re_adopted_session_survives_the_old_tombstone(self):
        self.finished_delete()
        self.re_adopt_under_a(at_ms=NOW_MS + 60_000)

        self.sync()

        self.assertEqual(self.names(self.box.a), ["local_%s.json" % X])
        self.assertEqual(self.names(self.box.b), ["local_%s.json" % X])
        self.assertTrue(self.in_sync())

    def test_a_session_deleted_again_after_re_adoption_stays_deleted(self):
        # Re-adopted under A, then deleted under B before any run saw the re-adoption.
        self.finished_delete()
        self.re_adopt_under_a(at_ms=NOW_MS + 60_000)
        write_tombstone(self.box.b, X, deleted_at_ms=NOW_MS + 120_000)
        self.clock_s = NOW_S + 300

        self.sync()

        self.assertEqual(self.names(self.box.a), ["deleted_%s" % X])
        self.assertEqual(self.names(self.box.b), ["deleted_%s" % X])

    def test_a_third_partition_with_a_stale_copy_does_not_undo_a_finished_delete(self):
        self.finished_delete()
        third = self.box.partition("cccccccc-0000-4000-8000-000000000003", "cccccccc-0000-4000-8000-0000000000c3")
        write_record(third, X, activity=100, title="stale")
        enrol(self.settings.config_path, third)

        self.sync()

        self.assertEqual(self.names(third), ["deleted_%s" % X])
        self.assertEqual(self.names(self.box.a), ["deleted_%s" % X])

    def test_forgetting_the_sync_history_does_not_retire_a_re_adopted_session(self):
        self.finished_delete()
        self.re_adopt_under_a(at_ms=NOW_MS + 60_000)
        self.settings.state_path.unlink()

        self.sync()

        self.assertEqual(self.names(self.box.a), ["local_%s.json" % X])
        self.assertEqual(self.names(self.box.b), ["local_%s.json" % X])

    def test_forgetting_the_sync_history_does_not_bring_a_deleted_session_back(self):
        # The delete had reached only one side when the history was forgotten.
        self.synced()
        (self.box.b / ("local_%s.json" % X)).unlink()
        write_tombstone(self.box.b, X, deleted_at_ms=NOW_MS)
        self.settings.state_path.unlink()

        self.sync()

        self.assertEqual(self.names(self.box.a), ["deleted_%s" % X])
        self.assertEqual(self.names(self.box.b), ["deleted_%s" % X])

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


class PresenceSeenDuringARun(RunTest):
    """R7: what a run observed or created counts as present, even if it is gone by the final scan."""

    def app_acts_right_after_the_writes(self, act):
        from session_sync.applier import Applier
        real = Applier.apply

        def apply_then_app_acts(applier, plan):
            outcomes = real(applier, plan)
            act()
            return outcomes

        return mock.patch.object(Applier, "apply", apply_then_app_acts)

    def test_a_source_removed_before_the_final_scan_is_not_put_back(self):
        # The app deletes X under B just after the tool copied it to A: the record goes first,
        # the tombstone later, or never if writing it fails.
        write_record(self.box.a, Y)
        self.sync()
        write_record(self.box.b, X)
        with self.app_acts_right_after_the_writes((self.box.b / ("local_%s.json" % X)).unlink):
            self.sync()

        for _ in range(3):
            report = self.sync()
            self.assertNotIn("local_%s.json" % X, self.names(self.box.b))
            self.assertIn(("lost", X, str(self.box.b)), {(p.kind, p.session_id, p.partition) for p in report.problems})

    def test_a_record_the_tool_created_and_that_is_gone_again_is_not_created_twice(self):
        write_record(self.box.a, Y)
        self.sync()
        write_record(self.box.b, X)
        with self.app_acts_right_after_the_writes(lambda: (self.box.a / ("local_%s.json" % X)).unlink()):
            self.sync()

        for _ in range(3):
            self.sync()
            self.assertNotIn("local_%s.json" % X, self.names(self.box.a))

    def test_what_a_run_saw_is_remembered_even_if_the_run_never_finishes(self):
        write_record(self.box.a, Y)
        self.sync()
        write_record(self.box.b, X)
        with mock.patch("session_sync.run.Applier.apply", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.sync()
        (self.box.b / ("local_%s.json" % X)).unlink()
        write_record(self.box.a, X)  # as if the stopped run had got as far as copying it

        self.sync()

        self.assertNotIn("local_%s.json" % X, self.names(self.box.b))


class CreatesOfARunThatDidNotFinish(RunTest):
    """R7: a create is written down the moment it completes, whatever then happens to the run."""

    def a_run_creates_x_in_a_and_then(self, fails_with, at):
        write_record(self.box.a, Y)
        self.sync()
        write_record(self.box.b, X)
        with mock.patch(at, side_effect=fails_with):
            with self.assertRaises(type(fails_with) if not isinstance(fails_with, type) else fails_with):
                self.sync()
        self.assertIn("local_%s.json" % X, self.names(self.box.a), "the create itself completed")
        (self.box.a / ("local_%s.json" % X)).unlink()  # removed afterwards, with no tombstone

    def assert_never_created_again(self):
        for _ in range(3):
            report = self.sync()
            self.assertNotIn("local_%s.json" % X, self.names(self.box.a))
            self.assertIn(("lost", X, str(self.box.a)), {(p.kind, p.session_id, p.partition) for p in report.problems})

    def test_a_completed_create_is_not_repeated_after_the_run_was_interrupted(self):
        self.a_run_creates_x_in_a_and_then(KeyboardInterrupt, at="session_sync.run._remember_placements")

        self.assert_never_created_again()

    def test_a_completed_create_is_not_repeated_after_the_final_scan_failed(self):
        import session_sync.run as module
        real = module._scan_or_abort
        calls = []

        def second_scan_fails(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise RunAborted("the final scan could not read a partition")
            return real(*args, **kwargs)

        write_record(self.box.a, Y)
        self.sync()
        write_record(self.box.b, X)
        with mock.patch.object(module, "_scan_or_abort", second_scan_fails):
            with self.assertRaises(RunAborted):
                self.sync()
        (self.box.a / ("local_%s.json" % X)).unlink()

        self.assert_never_created_again()

    def test_recreate_lifts_the_hold_with_no_run_in_between(self):
        # Straight after the run that did not finish: nothing else may still claim the record was there.
        from session_sync.run import forget_presence
        self.a_run_creates_x_in_a_and_then(KeyboardInterrupt, at="session_sync.run._remember_placements")
        self.assertIn(("lost", X), {(p.kind, p.session_id) for p in self.sync(apply=False).problems})

        forget_presence(self.settings, X)
        self.sync()

        self.assertIn("local_%s.json" % X, self.names(self.box.a))

    def test_forgetting_the_sync_history_forgets_such_a_create_too(self):
        self.a_run_creates_x_in_a_and_then(KeyboardInterrupt, at="session_sync.run._remember_placements")
        self.settings.state_path.unlink()

        self.sync()

        self.assertIn("local_%s.json" % X, self.names(self.box.a), "first contact again: a missing record is copied")

    def test_a_failed_save_does_not_erase_what_was_already_known(self):
        # X was under B before, was deleted there, and was then used under A, so it goes back to B.
        # If writing that down fails, B's earlier presence must survive the undo.
        import session_sync.run as module
        write_record(self.box.a, X, activity=100)
        self.sync()
        (self.box.b / ("local_%s.json" % X)).unlink()
        write_tombstone(self.box.b, X, deleted_at_ms=self.clock_s * 1000)
        self.clock_s += 60
        write_record(self.box.a, X, at_s=LONG_AGO_S + 50, activity=self.clock_s * 1000, title="used after the delete")
        real = module.save_state
        saves = []

        def the_save_after_the_create_fails(path, stored):
            saves.append(1)
            if len(saves) == 1:
                raise PermissionError("state.json")
            real(path, stored)

        with mock.patch.object(module, "save_state", the_save_after_the_create_fails):
            self.sync()

        self.assertNotIn("local_%s.json" % X, self.names(self.box.b))
        self.assertIn(X, load_state(self.settings.state_path).sync.seen[str(self.box.b)])

    def test_a_create_that_cannot_be_written_down_is_undone_and_tried_again_later(self):
        import session_sync.run as module
        write_record(self.box.a, Y)
        self.sync()
        write_record(self.box.b, X)
        real = module.save_state
        saves = []

        def the_save_after_the_create_fails(path, stored):
            saves.append(1)
            if len(saves) == 2:  # the first is what the run saw, the second follows the create
                raise PermissionError("state.json")
            real(path, stored)

        with mock.patch.object(module, "save_state", the_save_after_the_create_fails):
            report = self.sync()

        self.assertIn("PermissionError", report.failures[0].problem)
        self.assertNotIn("local_%s.json" % X, self.names(self.box.a), "undone, so there is nothing to remember")
        self.assertNotIn(X, load_state(self.settings.state_path).sync.seen[str(self.box.a)])

        self.sync()
        self.assertIn("local_%s.json" % X, self.names(self.box.a))


class UntrustworthyTimes(RunTest):
    def test_activity_dated_in_the_future_decides_nothing(self):
        # A record saved while the clock ran ahead. Read as "now" it would outrank every
        # tombstone and undo a real delete.
        day_ms = 86_400_000
        write_record(self.box.a, X, activity=self.clock_s * 1000 + day_ms)
        write_tombstone(self.box.b, X, deleted_at_ms=self.clock_s * 1000 - 1000)

        report = self.sync()

        self.assertEqual(self.names(self.box.a), ["local_%s.json" % X])
        self.assertEqual(self.names(self.box.b), ["deleted_%s" % X])
        self.assertEqual([(p.kind, p.partition) for p in report.problems], [("future", str(self.box.a))])


    def test_a_session_in_use_is_never_reported_as_future_dated(self):
        ticks = iter(range(10_000))
        start_ms = self.clock_s * 1000
        write_record(self.box.a, X, activity=start_ms + 1_500)  # saved just after the run began

        report = sync(self.settings, apply=True, running=lambda: False,
                      now_ns=lambda: (start_ms + 1_000 * next(ticks)) * 1_000_000)

        self.assertEqual(report.problems, [])
        self.assertEqual(self.names(self.box.b), ["local_%s.json" % X])


class UnreadableFiles(RunTest):
    def test_a_copy_that_cannot_be_read_freezes_its_session_instead_of_reading_as_absent(self):
        # A is where the delete happened. B holds the newer copy but cannot be read.
        # Reading B as absent would retire C's copy and put a tombstone beside B's record.
        third = self.box.partition("cccccccc-0000-4000-8000-000000000003", "cccccccc-0000-4000-8000-0000000000c3")
        enrol(self.settings.config_path, third)
        write_tombstone(self.box.a, X, deleted_at_ms=500)
        locked = write_record(self.box.b, X, activity=900)
        write_record(third, X, activity=100)
        os.chmod(locked, 0o000)
        self.addCleanup(os.chmod, locked, 0o600)

        report = self.sync()

        self.assertEqual(self.names(third), ["local_%s.json" % X])
        self.assertEqual(self.names(self.box.b), ["local_%s.json" % X])
        self.assertEqual([(p.kind, p.partition) for p in report.problems], [("unreadable", str(self.box.b))])

        os.chmod(locked, 0o600)  # repaired without touching the file's time or size
        self.sync()
        self.assertEqual(self.names(self.box.a), ["local_%s.json" % X], "B's later activity now counts")

    def test_a_partition_whose_entries_cannot_be_inspected_stops_the_run_before_any_change(self):
        # Mode 0400: the directory can be listed but nothing in it can be stat'ed. Read as empty,
        # it would hide B's newer copy while C's is retired.
        third = self.box.partition("cccccccc-0000-4000-8000-000000000003", "cccccccc-0000-4000-8000-0000000000c3")
        self.settings.config_path.write_text(json.dumps(
            {"partitions": [str(self.box.a), str(third), str(self.box.b)]}))
        write_tombstone(self.box.a, X, deleted_at_ms=500)
        write_record(self.box.b, X, activity=900)
        write_record(third, X, activity=100)
        (self.box.b / ".sync-1-local_x.json.part").write_text("left by a run that was killed")
        os.chmod(self.box.b, 0o400)
        self.addCleanup(os.chmod, self.box.b, 0o700)

        with self.assertRaises(RunAborted) as raised:  # the sweep before the scan must not trip over it either
            self.sync()

        self.assertIn(str(self.box.b), str(raised.exception))
        self.assertEqual(self.names(third), ["local_%s.json" % X])
        self.assertEqual(self.names(self.box.a), ["deleted_%s" % X])

    def test_a_partition_that_cannot_be_listed_stops_the_run_cleanly(self):
        write_record(self.box.a, X)
        os.chmod(self.box.b, 0o000)
        self.addCleanup(os.chmod, self.box.b, 0o700)

        with self.assertRaises(RunAborted) as raised:
            self.sync()

        self.assertIn(str(self.box.b), str(raised.exception))


class ThreePartitions(RunTest):
    def test_a_second_change_after_partial_propagation_still_gets_through(self):
        third = self.box.partition("cccccccc-0000-4000-8000-000000000003", "cccccccc-0000-4000-8000-0000000000c3")
        enrol(self.settings.config_path, third)
        write_record(self.box.a, X, activity=100, title="v0")
        self.sync()
        self.app_running_as("cccccccc-0000-4000-8000-000000000003")
        write_record(self.box.a, X, at_s=LONG_AGO_S + 50, activity=500, title="first change")
        self.sync()
        self.assertEqual(title_of(self.box.b, X), "first change")
        self.assertEqual(title_of(third, X), "v0")

        write_record(self.box.a, X, at_s=LONG_AGO_S + 60, activity=500, title="renamed, no new activity")
        report = self.sync()

        self.assertEqual(title_of(self.box.b, X), "renamed, no new activity")
        self.assertEqual({p.kind for p in report.problems}, {"live"})

        self.running = False
        self.sync()
        self.assertEqual(title_of(third, X), "renamed, no new activity")
        self.assertTrue(self.in_sync())


class OrphanedTempFiles(RunTest):
    def test_a_temp_file_left_beside_a_finished_delete_is_moved_aside(self):
        # At startup the app promotes an orphaned temp file to a live record, tombstone or not.
        for side in (self.box.a, self.box.b):
            write_tombstone(side, X, deleted_at_ms=NOW_MS)
        (self.box.a / ("local_%s.json.tmp" % X)).write_text('{"half": "a save"}')

        self.sync()

        self.assertEqual(self.names(self.box.a), ["deleted_%s" % X])
        self.assertEqual(self.kept(), ["local_%s.json.tmp" % X])


class LoginChanges(RunTest):
    """R9. The numbers in the cadence test are the ones measured on a real install."""

    def test_work_reaches_the_other_partition_while_the_app_keeps_rewriting_its_config(self):
        write_record(self.box.a, X, activity=100, title="agreed")
        self.sync()
        self.app_running_as(ACCOUNT_B)

        for minute in range(1, 6):
            self.box.logged_in_as(ACCOUNT_B, at_s=self.clock_s)  # the app's own unrelated config write
            write_record(self.box.b, X, at_s=LONG_AGO_S + minute, activity=100 + minute, title="turn %d" % minute)
            self.clock_s += 50
            self.sync()

            self.assertEqual(title_of(self.box.a, X), "turn %d" % minute)

    def test_nothing_is_changed_anywhere_for_a_while_after_a_login_change_is_first_seen(self):
        write_record(self.box.a, X, activity=100, title="agreed")
        self.sync()
        self.app_running_as(ACCOUNT_A)
        write_record(self.box.a, X, at_s=LONG_AGO_S + 50, activity=500, title="last work under A")
        self.box.logged_in_as(ACCOUNT_B)  # the switch: A may still be flushing pending saves

        during = self.sync()
        self.assertEqual(title_of(self.box.b, X), "agreed")
        self.assertEqual({(p.kind, p.partition) for p in during.problems}, {("live", str(self.box.b))})

        self.clock_s += 130
        self.sync()
        self.assertEqual(title_of(self.box.b, X), "agreed", "B is the login now, so it waits for B to go idle")
        self.assertEqual(title_of(self.box.a, X), "last work under A")

    def test_the_first_run_ever_treats_the_login_as_just_changed(self):
        write_record(self.box.a, X, activity=100, title="older")
        write_record(self.box.b, X, activity=900, title="newer")
        self.running = True
        self.box.logged_in_as(ACCOUNT_B)

        first = self.sync()
        self.assertEqual(title_of(self.box.a, X), "older")
        self.assertIn(("live", str(self.box.a)), {(p.kind, p.partition) for p in first.problems})

        self.clock_s += 130
        self.sync()
        self.assertEqual(title_of(self.box.a, X), "newer")

    def test_a_dry_run_sees_a_login_change_without_recording_it(self):
        write_record(self.box.a, X)
        self.sync()
        self.app_running_as(ACCOUNT_A)
        self.box.logged_in_as(ACCOUNT_B)
        before = self.settings.state_path.read_bytes()

        self.sync(apply=False)

        self.assertEqual(self.settings.state_path.read_bytes(), before)


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

    def test_one_directory_listed_twice_would_be_synced_with_itself_so_the_run_stops(self):
        self.settings.config_path.write_text(json.dumps({"partitions": [str(self.box.a), str(self.box.a)]}))
        write_record(self.box.a, X)

        with self.assertRaises(RunAborted) as raised:
            self.sync()

        self.assertIn("same directory", str(raised.exception))

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

    def test_kept_copies_are_bounded_by_size_as_well_as_age(self):
        import session_sync.run as module
        for name, age_days in (("20260101-000000", 3), ("20260102-000000", 2), ("20260103-000000", 1)):
            folder = self.settings.kept_root / name
            folder.mkdir(parents=True)
            (folder / "local_big.json").write_bytes(b"x" * 1000)
            os.utime(folder, ns=((self.clock_s - age_days * 86400) * SECOND_NS,) * 2)
        write_record(self.box.a, X)

        with mock.patch.object(module, "KEPT_MAX_BYTES", 2500):
            self.sync()

        self.assertEqual(sorted(p.name for p in self.settings.kept_root.iterdir()),
                         ["20260102-000000", "20260103-000000"], "the oldest run goes first")

    def test_the_size_cap_never_removes_what_this_very_run_kept(self):
        import session_sync.run as module
        write_record(self.box.a, X, activity=100, title="agreed")
        self.sync()
        write_record(self.box.a, X, at_s=LONG_AGO_S + 50, activity=500, title="work under A")
        write_record(self.box.b, X, at_s=LONG_AGO_S + 60, activity=900, title="later work under B")

        with mock.patch.object(module, "KEPT_MAX_BYTES", 1):
            report = self.sync()

        kept = [outcome.kept for outcome in report.done if outcome.kept]
        self.assertEqual(len(kept), 1)
        self.assertTrue(kept[0].exists(), "the report names this path, so it must still be there")

    def test_a_run_stopped_in_the_middle_leaves_nothing_half_done_and_the_next_run_finishes(self):
        import session_sync.applier as module
        Z = "33333333-3333-4333-8333-333333333333"
        for sid in (X, Y, Z):
            write_record(self.box.a, sid)
        real = module.commit_create
        creates = []

        def stopped_during_the_second_create(temporary, destination):
            creates.append(destination)
            if len(creates) == 2:
                raise KeyboardInterrupt
            real(temporary, destination)

        with mock.patch.object(module, "commit_create", stopped_during_the_second_create):
            with self.assertRaises(KeyboardInterrupt):
                self.sync()
        self.assertEqual(self.names(self.box.b), ["local_%s.json" % X])

        self.assertEqual(load_state(self.settings.state_path).sync.seen[str(self.box.b)], {X},
                         "only the create that completed was written down")

        after = self.sync()

        self.assertEqual(after.problems, [])
        self.assertEqual(len(self.names(self.box.b)), 3)

    def test_stale_temp_files_are_swept_from_the_tools_own_folders_too(self):
        self.settings.kept_root.mkdir(parents=True)
        strays = [self.box.state_dir / ".sync-1-state.json.part", self.settings.kept_root / ".sync-1-x.part"]
        for stray in strays:
            stray.write_text("x")
            os.utime(stray, ns=((self.clock_s - 3600) * SECOND_NS,) * 2)

        self.sync()

        self.assertEqual([stray.exists() for stray in strays], [False, False])

    def test_an_idle_run_does_not_rewrite_the_state_file(self):
        write_record(self.box.a, X)
        self.sync()
        self.sync()
        before = os.lstat(self.settings.state_path).st_mtime_ns

        self.sync()

        self.assertEqual(os.lstat(self.settings.state_path).st_mtime_ns, before)

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
