import os
import stat
import unittest
from unittest import mock

from session_sync.atomic import create_exclusive, write_atomic
from session_sync.scanner import TMP_NAME
from tests.fs_helpers import Sandbox, X


class WriteAtomic(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)
        self.target = self.box.a / ("local_%s.json" % X)

    def test_the_file_arrives_whole_private_and_with_the_given_time(self):
        write_atomic(self.target, b"payload", mtime_ns=1_234_567_890_123_456_789)

        info = os.lstat(self.target)
        self.assertEqual(self.target.read_bytes(), b"payload")
        self.assertEqual(stat.S_IMODE(info.st_mode), 0o600)
        self.assertEqual(info.st_mtime_ns, 1_234_567_890_123_456_789)
        self.assertEqual([p.name for p in self.box.a.iterdir()], [self.target.name])

    def test_a_failure_leaves_the_old_file_and_no_temp_file(self):
        self.target.write_bytes(b"old")

        with mock.patch("session_sync.atomic.os.replace", side_effect=OSError("disk says no")):
            with self.assertRaises(OSError):
                write_atomic(self.target, b"new")

        self.assertEqual(self.target.read_bytes(), b"old")
        self.assertEqual([p.name for p in self.box.a.iterdir()], [self.target.name])

    def test_the_temp_name_is_one_the_app_ignores_and_never_promotes(self):
        names = []
        real_replace = os.replace

        def spy(source, destination):
            names.append(os.path.basename(source))
            real_replace(source, destination)

        with mock.patch("session_sync.atomic.os.replace", side_effect=spy):
            write_atomic(self.target, b"x")

        self.assertTrue(names[0].startswith(".sync-"))
        self.assertIsNone(TMP_NAME.match(names[0]))
        self.assertFalse(names[0].startswith(("local_", "deleted_")))


class CreateExclusive(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)
        self.target = self.box.a / ("local_%s.json" % X)

    def test_a_missing_file_is_created_whole_with_a_single_link(self):
        create_exclusive(self.target, b"payload", mtime_ns=5_000_000_000)

        info = os.lstat(self.target)
        self.assertEqual(self.target.read_bytes(), b"payload")
        self.assertEqual((stat.S_IMODE(info.st_mode), info.st_mtime_ns, info.st_nlink), (0o600, 5_000_000_000, 1))
        self.assertEqual([p.name for p in self.box.a.iterdir()], [self.target.name])

    def test_an_existing_file_is_never_overwritten(self):
        self.target.write_bytes(b"the app wrote this")

        with self.assertRaises(FileExistsError):
            create_exclusive(self.target, b"ours")

        self.assertEqual(self.target.read_bytes(), b"the app wrote this")
        self.assertEqual([p.name for p in self.box.a.iterdir()], [self.target.name])


if __name__ == "__main__":
    unittest.main()
