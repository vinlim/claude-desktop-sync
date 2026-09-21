import json
import os
import stat
import unittest
import zipfile
from unittest import mock

from session_sync import backups
from session_sync.backups import BackupFailed, BackupUnusable, RestoreRefused
from tests.fs_helpers import LONG_AGO_S, SECOND_NS, Sandbox, X, Y, write_record, write_tombstone

Z = "33333333-3333-4333-8333-333333333333"
NOW_NS = (LONG_AGO_S + 3600) * SECOND_NS


class BackupsTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)
        self.folders = [self.box.a, self.box.b]
        self.state = self.box.state_dir / "state.json"
        self.root = self.box.state_dir / "backups"
        self.clock_s = LONG_AGO_S + 3600

    def take(self, reason="asked for", note="", folders=None):
        self.clock_s += 60
        return backups.take(folders or self.folders, self.state, self.root, reason=reason, note=note,
                            now_ns=lambda: self.clock_s * SECOND_NS)

    def contents(self, folder):
        return {p.name: (p.read_bytes(), os.lstat(p).st_mtime_ns) for p in sorted(folder.iterdir())}


class TakingABackup(BackupsTest):
    def test_it_holds_every_record_marker_and_temp_file_of_every_folder_and_the_tools_state(self):
        record = write_record(self.box.a, X, at_s=LONG_AGO_S + 7)
        write_tombstone(self.box.a, Y, deleted_at_ms=5)
        (self.box.b / ("local_%s.json.tmp" % Z)).write_text("half a save")
        self.box.state_dir.mkdir(parents=True, exist_ok=True)
        self.state.write_text('{"the": "state"}')

        taken = self.take(note="before tidying")

        with zipfile.ZipFile(taken.path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            self.assertEqual(archive.read("partitions/0/%s" % record.name), record.read_bytes())
            self.assertEqual(archive.read("partitions/1/local_%s.json.tmp" % Z), b"half a save")
            self.assertEqual(archive.read("state/state.json"), b'{"the": "state"}')
        self.assertEqual([entry["path"] for entry in manifest["partitions"]], [str(self.box.a), str(self.box.b)])
        self.assertEqual(sorted(manifest["partitions"][0]["files"]), ["deleted_%s" % Y, record.name])
        self.assertEqual(manifest["partitions"][0]["files"][record.name]["mtime_ns"], (LONG_AGO_S + 7) * SECOND_NS)
        self.assertEqual((taken.reason, taken.note), ("asked for", "before tidying"))
        self.assertEqual(taken.counts, ((str(self.box.a), 1, 1), (str(self.box.b), 0, 0)))

    def test_the_archive_is_private_and_appears_whole(self):
        write_record(self.box.a, X)

        taken = self.take()

        self.assertEqual(stat.S_IMODE(os.lstat(taken.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.lstat(self.root).st_mode), 0o700)
        self.assertEqual([p.name for p in self.root.iterdir()], [taken.path.name])

    def test_files_the_tool_would_never_write_are_left_out(self):
        write_record(self.box.a, X)
        (self.box.a / "notes.txt").write_text("not the tool's business")
        (self.box.a / ".sync-99-local_x.json.part").write_text("a staged file")

        taken = self.take()

        with zipfile.ZipFile(taken.path) as archive:
            self.assertEqual([name for name in archive.namelist() if name.startswith("partitions/")],
                             ["partitions/0/local_%s.json" % X])

    def test_a_file_that_vanishes_before_it_is_read_is_left_out(self):
        write_record(self.box.a, X)
        gone = write_record(self.box.a, Y)
        real = backups._read_stable

        def vanishing(path):
            if path == gone:
                gone.unlink()
            return real(path)

        with mock.patch.object(backups, "_read_stable", vanishing):
            taken = self.take()

        self.assertEqual(taken.counts[0], (str(self.box.a), 1, 0))

    def test_a_file_that_cannot_be_read_is_left_out_and_a_restore_never_removes_it(self):
        # Unreadable is not absent: the sync freezes such a session, and a restore leaves the file alone.
        write_record(self.box.a, X)
        locked = write_record(self.box.a, Y)
        os.chmod(locked, 0)
        self.addCleanup(os.chmod, locked, 0o600)

        taken = self.take()
        plan = backups.plan_restore(taken.path, self.folders)

        self.assertEqual((taken.counts[0], taken.unreadable), ((str(self.box.a), 1, 0), 1))
        self.assertEqual(plan.removals[str(self.box.a)], [])
        self.assertEqual(backups.listing(self.root)[0].unreadable, 1)

    def test_a_file_that_keeps_changing_fails_the_backup_and_leaves_nothing_behind(self):
        busy = write_record(self.box.a, X)
        ticks = []

        def restless(descriptor):
            ticks.append(1)
            return len(ticks), 10  # a new time at every look, as if the app never stopped writing

        with mock.patch.object(backups, "_stamp", restless):
            with self.assertRaises(BackupFailed) as failed:
                self.take()

        self.assertIn(busy.name, str(failed.exception))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_two_backups_in_one_second_get_different_names(self):
        write_record(self.box.a, X)
        first = self.take()
        self.clock_s -= 60

        second = self.take()

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(len(list(self.root.iterdir())), 2)


class ListingAndPruning(BackupsTest):
    def test_the_list_is_newest_first_with_counts_reason_and_note(self):
        write_record(self.box.a, X)
        old = self.take(reason="before the first sync")
        write_record(self.box.b, Y)
        new = self.take(note="second")

        listed = backups.listing(self.root)

        self.assertEqual([entry.id for entry in listed], [new.id, old.id])
        self.assertEqual(listed[0], new)
        self.assertEqual((listed[1].reason, listed[1].counts[1]), ("before the first sync", (str(self.box.b), 0, 0)))

    def test_an_archive_that_cannot_be_read_is_listed_as_such(self):
        write_record(self.box.a, X)
        good = self.take()
        (self.root / "19700101-000000.zip").write_bytes(b"not a zip")

        listed = backups.listing(self.root)

        self.assertEqual([(entry.id, entry.usable) for entry in listed], [(good.id, True), ("19700101-000000", False)])

    def test_no_folder_yet_is_an_empty_list(self):
        self.assertEqual(backups.listing(self.root), [])

    def test_only_the_newest_are_kept_and_the_one_just_taken_always_is(self):
        write_record(self.box.a, X)
        taken = [self.take() for _ in range(4)]

        with mock.patch.object(backups, "KEEP", 2):
            fifth = self.take()

        self.assertEqual(sorted(p.name for p in self.root.iterdir()), sorted([taken[3].path.name, fifth.path.name]))


class PlanningARestore(BackupsTest):
    def test_the_plan_names_what_would_be_written_and_what_would_be_removed(self):
        changed = write_record(self.box.a, X, title="as it was")
        deleted = write_record(self.box.a, Y)
        untouched = write_record(self.box.b, X)
        taken = self.take()
        write_record(self.box.a, X, at_s=LONG_AGO_S + 9, title="renamed since")
        deleted.unlink()
        marker = write_tombstone(self.box.a, Y, deleted_at_ms=5)
        newer = write_record(self.box.b, Z)

        plan = backups.plan_restore(taken.path, self.folders)

        self.assertEqual(plan.writes, {str(self.box.a): [changed.name, deleted.name], str(self.box.b): []})
        self.assertEqual(plan.removals, {str(self.box.a): [marker.name], str(self.box.b): [newer.name]})
        self.assertEqual(plan.unchanged, {str(self.box.a): 0, str(self.box.b): 1})
        self.assertTrue(untouched.exists())

    def test_a_backup_of_other_folders_is_refused(self):
        write_record(self.box.a, X)
        taken = self.take(folders=[self.box.a])

        with self.assertRaises(RestoreRefused) as refused:
            backups.plan_restore(taken.path, self.folders)

        self.assertIn(str(self.box.b), str(refused.exception))

    def test_a_damaged_member_is_refused(self):
        write_record(self.box.a, X)
        taken = self.take()
        self.rewrite(taken.path, {"partitions/0/local_%s.json" % X: b"something else"})

        with self.assertRaises(BackupUnusable):
            backups.plan_restore(taken.path, self.folders)

    def test_a_name_the_tool_would_never_write_is_refused(self):
        # The archive is sound in every other way: the member exists and matches its checksum.
        # Only the name rule stands between it and a file written outside the partition.
        record = write_record(self.box.a, X)
        taken = self.take()
        with zipfile.ZipFile(taken.path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        files = manifest["partitions"][0]["files"]
        files["../../escaped.json"] = files.pop(record.name)
        self.rewrite(taken.path, {"manifest.json": json.dumps(manifest).encode(),
                                  "partitions/0/../../escaped.json": record.read_bytes()})

        with self.assertRaises(BackupUnusable) as refused:
            backups.plan_restore(taken.path, self.folders)

        self.assertIn("never write", str(refused.exception))
        self.assertFalse((self.box.a.parent.parent / "escaped.json").exists())

    def test_a_backup_written_by_a_newer_format_is_refused(self):
        write_record(self.box.a, X)
        taken = self.take()
        with zipfile.ZipFile(taken.path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
        manifest["format"] = backups.FORMAT + 1
        self.rewrite(taken.path, {"manifest.json": json.dumps(manifest).encode()})

        with self.assertRaises(BackupUnusable):
            backups.plan_restore(taken.path, self.folders)

    def rewrite(self, path, replaced):
        with zipfile.ZipFile(path) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        members.update(replaced)
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in members.items():
                archive.writestr(name, data)


class Restoring(BackupsTest):
    def test_every_folder_ends_up_as_it_was_and_the_state_is_put_back(self):
        write_record(self.box.a, X, at_s=LONG_AGO_S + 7, title="as it was")
        write_record(self.box.a, Y)
        write_tombstone(self.box.b, Z, deleted_at_ms=5)
        self.box.state_dir.mkdir(parents=True, exist_ok=True)
        self.state.write_text('{"the": "state then"}')
        before = [self.contents(folder) for folder in self.folders]
        taken = self.take()
        write_record(self.box.a, X, at_s=LONG_AGO_S + 9, title="renamed since")
        (self.box.a / ("local_%s.json" % Y)).unlink()
        write_record(self.box.b, Y)
        self.state.write_text('{"the": "state now"}')

        backups.apply_restore(backups.plan_restore(taken.path, self.folders), self.state)

        self.assertEqual([self.contents(folder) for folder in self.folders], before)
        self.assertEqual(self.state.read_text(), '{"the": "state then"}')
        self.assertEqual(stat.S_IMODE(os.lstat(self.box.a / ("local_%s.json" % Y)).st_mode), 0o600)

    def test_files_the_tool_would_never_write_survive(self):
        write_record(self.box.a, X)
        taken = self.take()
        stranger = self.box.a / "notes.txt"
        stranger.write_text("not the tool's business")

        backups.apply_restore(backups.plan_restore(taken.path, self.folders), self.state)

        self.assertEqual(stranger.read_text(), "not the tool's business")

    def test_a_backup_from_before_the_first_sync_moves_the_later_state_aside(self):
        write_record(self.box.a, X)
        taken = self.take()
        self.state.write_text('{"the": "state now"}')

        backups.apply_restore(backups.plan_restore(taken.path, self.folders), self.state)

        self.assertFalse(self.state.exists())
        aside = [p for p in self.box.state_dir.iterdir() if p.name.startswith("state.json.before-restore-")]
        self.assertEqual([p.read_text() for p in aside], ['{"the": "state now"}'])

    def test_a_second_restore_finds_nothing_left_to_do(self):
        write_record(self.box.a, X, title="as it was")
        taken = self.take()
        write_record(self.box.a, X, at_s=LONG_AGO_S + 9, title="renamed since")
        backups.apply_restore(backups.plan_restore(taken.path, self.folders), self.state)

        again = backups.plan_restore(taken.path, self.folders)

        self.assertEqual((again.writes, again.removals), ({str(self.box.a): [], str(self.box.b): []},) * 2)

    def test_a_member_that_changed_after_the_plan_was_made_stops_the_restore_before_the_folders_change(self):
        # Two files to write and the second one is bad: the first must not have been written either.
        for sid in (X, Y):
            write_record(self.box.a, sid, title="as it was")
        taken = self.take()
        for sid in (X, Y):
            write_record(self.box.a, sid, at_s=LONG_AGO_S + 9, title="renamed since")
        plan = backups.plan_restore(taken.path, self.folders)
        present = self.contents(self.box.a)
        with zipfile.ZipFile(taken.path, "a") as archive:
            archive.writestr("partitions/0/local_%s.json" % Y, b"swapped")

        with self.assertRaises(BackupUnusable):
            backups.apply_restore(plan, self.state)

        self.assertEqual(self.contents(self.box.a), present)


if __name__ == "__main__":
    unittest.main()
