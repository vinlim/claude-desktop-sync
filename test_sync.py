import importlib.machinery
import importlib.util
import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).parent
loader = importlib.machinery.SourceFileLoader("sync", str(HERE / "claude-desktop-session-sync"))
spec = importlib.util.spec_from_loader("sync", loader)
sync = importlib.util.module_from_spec(spec)
loader.exec_module(sync)

ACCOUNT_A = "aaaaaaaa-0000-4000-8000-000000000001"
ORG_A = "aaaaaaaa-0000-4000-8000-0000000000a1"
ACCOUNT_B = "bbbbbbbb-0000-4000-8000-000000000002"
ORG_B = "bbbbbbbb-0000-4000-8000-0000000000b2"
S1 = "11111111-1111-4111-8111-111111111111"
S2 = "22222222-2222-4222-8222-222222222222"

SECOND = 1_000_000_000


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "Claude"
        self.retired = Path(self.tmp.name) / "retired"
        self.a = self.partition(ACCOUNT_A, ORG_A)
        self.b = self.partition(ACCOUNT_B, ORG_B)

    def tearDown(self):
        self.tmp.cleanup()

    def partition(self, account, org):
        path = self.root / sync.SESSIONS_DIR / account / org
        path.mkdir(parents=True, mode=0o700)
        return path

    def record(self, directory, session_id, at_seconds, title="t", session_field=None):
        path = directory / ("local_%s.json" % session_id)
        body = {"sessionId": session_field or "local_" + session_id, "title": title,
                "createdAt": 1, "lastActivityAt": at_seconds}
        path.write_text(json.dumps(body))
        os.chmod(path, 0o600)
        os.utime(path, ns=(at_seconds * SECOND, at_seconds * SECOND))
        return path

    def tombstone(self, directory, session_id, at_seconds):
        path = directory / ("deleted_%s" % session_id)
        path.write_text(str(at_seconds * 1000))
        return path

    def logged_in_as(self, account):
        (self.root / "config.json").write_text(json.dumps({"lastKnownAccountUuid": account}))

    def sync(self, apply=True, running=False, quiet=False):
        out = io.StringIO()
        code = sync.run([self.root], apply=apply, verbose=True, quiet=quiet, running=running,
                        retired_root=self.retired, out=out)
        return code, out.getvalue()

    def title(self, directory, session_id):
        return json.loads((directory / ("local_%s.json" % session_id)).read_text())["title"]

    def test_a_missing_record_is_copied_with_its_timestamp_and_private_mode(self):
        source = self.record(self.a, S1, 100)
        self.record(self.b, S2, 50)

        code, _ = self.sync()

        copied = self.b / source.name
        self.assertEqual(code, 0)
        self.assertEqual(copied.read_bytes(), source.read_bytes())
        self.assertEqual(os.lstat(copied).st_mtime_ns, 100 * SECOND)
        self.assertEqual(stat.S_IMODE(os.lstat(copied).st_mode), 0o600)
        self.assertTrue((self.a / ("local_%s.json" % S2)).exists(), "the copy runs both ways")

    def test_a_second_run_finds_nothing_to_do(self):
        self.record(self.a, S1, 100)
        self.record(self.b, S2, 50)
        self.sync()

        _, output = self.sync()

        self.assertIn("In sync.", output)

    def test_the_newest_record_replaces_an_older_copy(self):
        self.record(self.a, S1, 100, title="old")
        self.record(self.b, S1, 200, title="new")

        self.sync()

        self.assertEqual(self.title(self.a, S1), "new")
        self.assertEqual(os.lstat(self.a / ("local_%s.json" % S1)).st_mtime_ns, 200 * SECOND)

    def test_a_dry_run_writes_nothing(self):
        self.record(self.a, S1, 100)
        self.record(self.b, S2, 50)

        _, output = self.sync(apply=False)

        self.assertFalse((self.b / ("local_%s.json" % S1)).exists())
        self.assertIn("Dry run", output)

    def test_a_live_partition_gains_missing_records_but_keeps_its_own_versions(self):
        self.logged_in_as(ACCOUNT_B)
        self.record(self.a, S1, 200, title="newer elsewhere")
        self.record(self.b, S1, 100, title="held in memory by the app")
        self.record(self.a, S2, 100, title="new to the live side")

        _, output = self.sync(running=True)

        self.assertEqual(self.title(self.b, S1), "held in memory by the app")
        self.assertEqual(self.title(self.b, S2), "new to the live side")
        self.assertIn("this partition is live", output)

    def test_the_other_partition_is_not_live_while_the_app_runs(self):
        self.logged_in_as(ACCOUNT_B)
        self.record(self.a, S1, 100, title="old")
        self.record(self.b, S1, 200, title="new")

        self.sync(running=True)

        self.assertEqual(self.title(self.a, S1), "new")

    def test_an_unreadable_login_makes_every_partition_live(self):
        self.record(self.a, S1, 100, title="old")
        self.record(self.b, S1, 200, title="new")

        self.sync(running=True)

        self.assertEqual(self.title(self.a, S1), "old")

    def test_a_delete_retires_the_stale_copy_and_does_not_come_back(self):
        self.record(self.a, S1, 100)
        self.tombstone(self.b, S1, 150)
        self.record(self.b, S2, 50)

        self.sync()

        self.assertFalse((self.a / ("local_%s.json" % S1)).exists())
        self.assertFalse((self.b / ("local_%s.json" % S1)).exists(), "a deleted session must not be resurrected")
        self.assertTrue((self.a / ("deleted_%s" % S1)).exists(), "the tombstone travels")
        self.assertEqual((self.a / ("deleted_%s" % S1)).read_text(), "150000")
        kept = list(self.retired.rglob("local_%s.json" % S1))
        self.assertEqual(len(kept), 1, "a retired record is moved aside, never destroyed")

    def test_a_record_written_after_its_tombstone_wins(self):
        self.record(self.a, S1, 300, title="recreated")
        self.tombstone(self.b, S1, 150)
        self.record(self.b, S2, 50)

        self.sync()

        self.assertEqual(self.title(self.b, S1), "recreated")

    def test_a_delete_is_not_applied_to_a_live_partition(self):
        self.logged_in_as(ACCOUNT_A)
        self.record(self.a, S1, 100)
        self.tombstone(self.b, S1, 150)

        _, output = self.sync(running=True)

        self.assertTrue((self.a / ("local_%s.json" % S1)).exists())
        self.assertFalse((self.b / ("local_%s.json" % S1)).exists())
        self.assertIn("deleted elsewhere", output)

    def test_empty_cross_pairs_and_other_files_are_left_alone(self):
        cross = self.partition(ACCOUNT_A, ORG_B)
        (cross / "scheduled-tasks.json").write_text("{}")
        self.record(self.a, S1, 100)
        (self.a / "scheduled-tasks.json").write_text('{"tasks": []}')
        (self.a / "archived-sessions.idx").write_text("x")
        (self.a / "backlog").mkdir()
        (self.a / ("local_%s.json.tmp" % S2)).write_text("{}")
        self.record(self.b, S2, 50)

        self.sync()

        self.assertEqual(sorted(p.name for p in cross.iterdir()), ["scheduled-tasks.json"])
        self.assertEqual(sorted(p.name for p in self.b.iterdir()),
                         ["local_%s.json" % S1, "local_%s.json" % S2])

    def test_a_symlinked_partition_is_ignored(self):
        self.record(self.a, S1, 100)
        self.record(self.b, S2, 50)
        link = self.root / sync.SESSIONS_DIR / ACCOUNT_B / "cccccccc-0000-4000-8000-0000000000c3"
        link.symlink_to(self.a)

        self.sync()

        labels = [p.label for p in sync.discover([self.root])]
        self.assertEqual(len(labels), 2)

    def test_a_torn_or_foreign_record_is_not_propagated(self):
        (self.a / ("local_%s.json" % S1)).write_text("{not json")
        self.record(self.a, S2, 100, session_field="local_" + S1)
        self.record(self.b, "33333333-3333-4333-8333-333333333333", 50)

        code, output = self.sync()

        self.assertEqual(code, 1)
        self.assertFalse((self.b / ("local_%s.json" % S1)).exists())
        self.assertFalse((self.b / ("local_%s.json" % S2)).exists())
        self.assertIn("not valid JSON", output)
        self.assertIn("does not describe this session", output)

    def test_a_target_the_app_rewrote_after_planning_is_not_overwritten(self):
        self.record(self.a, S1, 200, title="newer")
        self.record(self.b, S1, 100, title="older")
        partitions = sync.discover([self.root])
        actions, _ = sync.plan(partitions, set())
        self.record(self.b, S1, 300, title="app saved meanwhile")

        problems = [sync.apply_copy(action) for action in actions if action.kind == "copy"]

        self.assertEqual(problems, ["target changed since planning"])
        self.assertEqual(self.title(self.b, S1), "app saved meanwhile")

    def test_quiet_mode_reports_writes_and_stays_silent_otherwise(self):
        self.logged_in_as(ACCOUNT_B)
        self.record(self.a, S1, 200, title="newer elsewhere")
        self.record(self.b, S1, 100, title="live")
        self.record(self.a, S2, 100)

        _, first = self.sync(running=True, quiet=True)
        _, second = self.sync(running=True, quiet=True)

        self.assertIn("copy -> %s" % ("bbbbbbbb/bbbbbbbb"), first)
        self.assertEqual(second, "", "a standing skip must not fill an unattended log")

    def test_the_agent_watches_every_partition_and_writes_quietly(self):
        self.record(self.a, S1, 100)
        self.record(self.b, S2, 50)

        plist = sync.agent_plist([self.root], explicit_roots=True)

        self.assertEqual(sorted(plist["WatchPaths"]), sorted([str(self.a), str(self.b)]))
        self.assertEqual(plist["ProgramArguments"][:1], ["/usr/bin/python3"])
        self.assertIn("--apply", plist["ProgramArguments"])
        self.assertIn("--quiet", plist["ProgramArguments"])
        self.assertEqual(plist["ProgramArguments"][-2:], ["--root", str(self.root)])

    def test_no_temporary_files_are_left_behind(self):
        self.record(self.a, S1, 100)
        self.tombstone(self.a, S2, 10)
        self.record(self.b, S2, 5)

        self.sync()

        leftovers = [p.name for p in list(self.a.iterdir()) + list(self.b.iterdir()) if p.name.startswith(".sync-")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
