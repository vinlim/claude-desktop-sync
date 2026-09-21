import unittest

from session_sync.intent_log import IntentLog
from tests.fs_helpers import Sandbox, X, Y


class IntentLogTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)
        self.log = IntentLog(self.box.state_dir / "placing.log")

    def test_nothing_recorded_reads_as_nothing(self):
        self.assertEqual(self.log.read(), {})

    def test_what_was_recorded_is_read_back_per_partition(self):
        self.log.record("/p/a", X)
        self.log.record("/p/b", X)
        self.log.record("/p/b", Y)

        self.assertEqual(IntentLog(self.log.path).read(), {"/p/a": {X}, "/p/b": {X, Y}})

    def test_a_partition_path_with_spaces_survives(self):
        self.log.record("/Users/me/Library/Application Support/Claude/x/y", X)

        self.assertEqual(self.log.read(), {"/Users/me/Library/Application Support/Claude/x/y": {X}})

    def test_a_line_torn_by_a_crash_is_ignored(self):
        self.log.record("/p/a", X)
        with open(self.log.path, "a") as handle:
            handle.write("/p/a\t2222")  # no newline: the write never finished

        self.assertEqual(self.log.read(), {"/p/a": {X}})

    def test_clearing_forgets_everything_and_is_safe_to_repeat(self):
        self.log.record("/p/a", X)

        self.log.clear()
        self.log.clear()

        self.assertEqual(self.log.read(), {})


if __name__ == "__main__":
    unittest.main()
