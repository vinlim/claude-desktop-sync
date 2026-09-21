import os
import unittest
from pathlib import Path
from unittest import mock

from session_sync.fingerprint import fingerprint
from session_sync.scanner import scan_partition
from tests.fs_helpers import LONG_AGO_S, SECOND_NS, Sandbox, X, Y, write_record, write_tombstone

NOW_S = LONG_AGO_S + 3600
NOW_NS = NOW_S * SECOND_NS
NOW_MS = NOW_S * 1000


def scan(path, cache=None):
    return scan_partition(path, cache or {}, clock=lambda: NOW_NS)


class WhatCounts(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)

    def test_records_and_tombstones_are_read_and_everything_else_is_ignored(self):
        record = write_record(self.box.a, X, activity=55)
        write_tombstone(self.box.a, Y, deleted_at_ms=1234)
        for name in ("archived-sessions.idx", "scheduled-tasks.json", ".sync-1-local_x.json.part", "notes.txt"):
            (self.box.a / name).write_text("x")
        (self.box.a / "backlog").mkdir()
        (self.box.a / "local_dir.json").mkdir()

        result = scan(self.box.a)

        self.assertEqual(result.snapshot.key, str(self.box.a))
        self.assertEqual(result.snapshot.records, {X: fingerprint(X, record.read_bytes())})
        self.assertEqual(result.snapshot.records[X].last_activity_at, 55)
        self.assertEqual(result.snapshot.tombstones, {Y: 1234})

    def test_ids_use_the_apps_grammar_not_only_uuids(self):
        write_record(self.box.a, "abc_DEF-123")

        self.assertIn("abc_DEF-123", scan(self.box.a).snapshot.records)

    def test_a_symlink_named_like_a_record_is_not_a_record(self):
        target = write_record(self.box.b, X)
        (self.box.a / target.name).symlink_to(target)

        self.assertEqual(scan(self.box.a).snapshot.records, {})

    def test_a_temp_file_with_no_record_beside_it_is_an_orphan(self):
        write_record(self.box.a, X)
        (self.box.a / ("local_%s.json.tmp" % X)).write_text("{}")
        (self.box.a / ("local_%s.json.tmp" % Y)).write_text("{}")

        result = scan(self.box.a)

        self.assertEqual(result.snapshot.orphan_tmps, frozenset({Y}))
        self.assertEqual(set(result.tmps), {X, Y}, "the applier still needs to know about every temp file")

    def test_stamps_are_reported_for_what_the_applier_will_guard(self):
        record = write_record(self.box.a, X)
        tombstone = write_tombstone(self.box.a, Y, 1)

        result = scan(self.box.a)

        self.assertEqual(result.records[X], (os.lstat(record).st_mtime_ns, os.lstat(record).st_size))
        self.assertEqual(result.tombstones[Y], (os.lstat(tombstone).st_mtime_ns, os.lstat(tombstone).st_size))


class DeleteTimes(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)

    def test_unusable_content_falls_back_to_the_files_own_time(self):
        for name, content in {"empty": "", "garbage": "soon", "negative": "-5", "float": "1.5"}.items():
            with self.subTest(case=name):
                write_tombstone(self.box.a, X, 0, at_s=LONG_AGO_S, content=content)

                self.assertEqual(scan(self.box.a).snapshot.tombstones[X], LONG_AGO_S * 1000)

    def test_a_time_in_the_future_is_not_trusted(self):
        # a future-dated tombstone must not outrank real activity.
        write_tombstone(self.box.a, X, deleted_at_ms=NOW_MS + 10 ** 9, at_s=LONG_AGO_S)

        self.assertEqual(scan(self.box.a).snapshot.tombstones[X], LONG_AGO_S * 1000)

    def test_a_file_time_in_the_future_is_clamped_to_now(self):
        write_tombstone(self.box.a, X, 0, at_s=NOW_S + 999, content="garbage")

        self.assertEqual(scan(self.box.a).snapshot.tombstones[X], NOW_MS)


class Robustness(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)

    def test_a_record_that_vanishes_while_being_read_is_skipped(self):
        # the app renames files under the scan.
        write_record(self.box.a, X)
        write_record(self.box.a, Y)
        real = Path.read_bytes

        def vanishing(path):
            if X in path.name:
                raise FileNotFoundError(path)
            return real(path)

        with mock.patch.object(Path, "read_bytes", vanishing):
            result = scan(self.box.a)

        self.assertEqual(set(result.snapshot.records), {Y})

    def test_a_tombstone_that_vanishes_while_being_read_is_skipped(self):
        # The app removes a tombstone when a session is re-adopted.
        tombstone = write_tombstone(self.box.a, X, 5)
        listed = os.lstat(tombstone)
        # Present when listed, gone by the time its content and then its own time are read.
        with mock.patch.object(Path, "read_text", side_effect=FileNotFoundError("gone")), \
                mock.patch("session_sync.scanner.os.lstat", side_effect=[listed, FileNotFoundError("gone")]):
            result = scan(self.box.a)

        self.assertEqual(result.snapshot.tombstones, {})


class OddRecords(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)

    def test_a_record_the_fingerprint_chokes_on_is_unreadable_and_the_scan_goes_on(self):
        write_record(self.box.a, X)
        write_record(self.box.a, Y)
        real = fingerprint

        def choking(session_id, data):
            if session_id == X:
                raise RuntimeError("something nobody predicted")
            return real(session_id, data)

        with mock.patch("session_sync.scanner.fingerprint", choking):
            result = scan(self.box.a)

        self.assertFalse(result.snapshot.records[X].readable)
        self.assertTrue(result.snapshot.records[Y].readable)

    def test_activity_dated_in_the_future_makes_the_copy_unusable_until_the_clock_catches_up(self):
        # Read as "now" it would outrank every tombstone and undo a real delete.
        write_record(self.box.a, X, activity=NOW_MS + 5_000)
        first = scan(self.box.a)
        again = scan(self.box.a, cache=first.cache)
        later = scan_partition(self.box.a, first.cache, clock=lambda: NOW_NS + 60 * SECOND_NS)

        for early in (first, again):
            self.assertTrue(early.snapshot.records[X].future_dated)
            self.assertFalse(early.snapshot.records[X].usable)
        self.assertEqual(first.cache[X][3], NOW_MS + 5_000, "the cache keeps the time as written")
        self.assertTrue(later.snapshot.records[X].usable)
        self.assertEqual(later.snapshot.records[X].last_activity_at, NOW_MS + 5_000)

    def test_a_record_the_app_saved_while_the_scan_was_running_is_not_future_dated(self):
        # A session in use is saved every few seconds with the current time. Judged against the
        # moment the scan began, a save that lands during the scan would look like the future.
        readings = []

        def clock():
            readings.append(1)
            return NOW_NS if len(readings) == 1 else NOW_NS + 2 * SECOND_NS

        write_record(self.box.a, X, activity=NOW_MS + 1_000)
        write_tombstone(self.box.a, Y, deleted_at_ms=NOW_MS + 1_000)

        result = scan_partition(self.box.a, {}, clock=clock)

        self.assertTrue(result.snapshot.records[X].usable)
        self.assertEqual(result.snapshot.tombstones[Y], NOW_MS + 1_000, "its own time, not the file's")

    def test_a_record_that_exists_but_cannot_be_read_is_unreadable_not_absent(self):
        path = write_record(self.box.a, X)
        os.chmod(path, 0o000)
        self.addCleanup(os.chmod, path, 0o600)

        locked = scan(self.box.a)
        os.chmod(path, 0o600)
        repaired = scan(self.box.a, cache=locked.cache)

        self.assertFalse(locked.snapshot.records[X].readable)
        self.assertIn(X, locked.records, "the applier still needs its stamp")
        self.assertTrue(repaired.snapshot.records[X].readable, "a repair changes neither time nor size")


class Cache(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)

    def test_an_unchanged_file_is_not_read_again(self):
        write_record(self.box.a, X)
        first = scan(self.box.a)

        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("must not re-read")):
            second = scan(self.box.a, cache=first.cache)

        self.assertEqual(second.snapshot.records, first.snapshot.records)

    def test_a_changed_file_is_read_again(self):
        write_record(self.box.a, X, title="one")
        first = scan(self.box.a)
        write_record(self.box.a, X, at_s=LONG_AGO_S + 60, title="two and longer")

        second = scan(self.box.a, cache=first.cache)

        self.assertNotEqual(second.snapshot.records[X], first.snapshot.records[X])

    def test_a_file_written_moments_ago_is_always_read_again(self):
        # Two saves inside one timestamp tick can share mtime and size.
        write_record(self.box.a, X, at_s=NOW_S - 1, title="aaa")
        first = scan(self.box.a)
        write_record(self.box.a, X, at_s=NOW_S - 1, title="bbb")

        second = scan(self.box.a, cache=first.cache)

        self.assertNotEqual(second.snapshot.records[X], first.snapshot.records[X])

    def test_the_cache_forgets_files_that_are_gone(self):
        path = write_record(self.box.a, X)
        first = scan(self.box.a)
        path.unlink()

        self.assertEqual(scan(self.box.a, cache=first.cache).cache, {})


if __name__ == "__main__":
    unittest.main()
