import unittest

from session_sync.intent_log import IntentLog
from tests.fs_helpers import Sandbox, X, Y


class IntentLogTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)
        self.log = IntentLog(self.box.state_dir / "placing.log")

    def test_nothing_recorded_reads_as_nothing(self):
        journal = self.log.read()

        self.assertEqual((journal.pending, journal.completed), ({}, {}))

    def test_a_create_is_pending_until_it_is_marked_done(self):
        self.log.intend("/p/a", X)
        self.log.intend("/p/b", X)
        self.log.intend("/p/b", Y)
        self.log.done("/p/b", X)

        journal = IntentLog(self.log.path).read()

        self.assertEqual(journal.pending, {"/p/a": {X}, "/p/b": {Y}})
        self.assertEqual(journal.completed, {"/p/b": {X}})

    def test_a_partition_path_with_spaces_survives(self):
        path = "/Users/me/Library/Application Support/Claude/x/y"
        self.log.intend(path, X)
        self.log.done(path, X)

        self.assertEqual(self.log.read().completed, {path: {X}})

    def test_a_line_torn_by_a_crash_is_ignored(self):
        self.log.intend("/p/a", X)
        with open(self.log.path, "a") as handle:
            handle.write("done\t/p/a\t1111")  # no newline: the write never finished

        journal = self.log.read()

        self.assertEqual((journal.pending, journal.completed), ({"/p/a": {X}}, {}))

    def test_a_done_line_with_no_intent_still_counts(self):
        # The intent line and the done line are separate writes; only the second says it happened.
        self.log.done("/p/a", X)

        self.assertEqual(self.log.read().completed, {"/p/a": {X}})

    def test_clearing_forgets_everything_and_is_safe_to_repeat(self):
        self.log.intend("/p/a", X)

        self.log.clear()
        self.log.clear()

        self.assertEqual(self.log.read().pending, {})


if __name__ == "__main__":
    unittest.main()
