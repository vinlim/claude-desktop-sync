import json
import unittest

from session_sync.enrolment import EnrolmentError, enrol, load_enrolled, unenrol, unenrolled_with_records
from tests.fs_helpers import ACCOUNT_A, ORG_B, Sandbox, X, write_record


class Enrolment(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)
        self.config = self.box.state_dir / "config.json"

    def test_enrolled_partitions_are_remembered_in_order_without_duplicates(self):
        enrol(self.config, self.box.a)
        enrol(self.config, self.box.b)
        enrol(self.config, self.box.a)

        self.assertEqual(load_enrolled(self.config), [self.box.a, self.box.b])

    def test_nothing_enrolled_yet_is_an_empty_list(self):
        self.assertEqual(load_enrolled(self.config), [])

    def test_unenrolling_forgets_one_partition(self):
        enrol(self.config, self.box.a)
        enrol(self.config, self.box.b)

        unenrol(self.config, self.box.a)

        self.assertEqual(load_enrolled(self.config), [self.box.b])

    def test_only_a_real_session_directory_can_be_enrolled(self):
        link = self.box.root / "claude-code-sessions" / ACCOUNT_A / ORG_B
        link.symlink_to(self.box.a)
        elsewhere = self.box.base / "somewhere" / "else"
        elsewhere.mkdir(parents=True)
        not_uuid = self.box.root / "claude-code-sessions" / ACCOUNT_A / "skills-plugin"
        not_uuid.mkdir()
        cases = {"a symlink": link, "the wrong shape": elsewhere, "a non-uuid folder": not_uuid,
                 "missing": self.box.a.parent / "cccccccc-0000-4000-8000-0000000000c3"}
        for name, path in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(EnrolmentError):
                    enrol(self.config, path)
        self.assertEqual(load_enrolled(self.config), [])

    def test_a_relative_or_tilde_path_is_stored_absolute(self):
        enrol(self.config, self.box.a)

        stored = json.loads(self.config.read_text())["partitions"]
        self.assertEqual(stored, [str(self.box.a)])

    def test_a_config_that_cannot_be_read_stops_the_run(self):
        self.config.parent.mkdir(parents=True)
        for content in ("{", "[]", '{"partitions": "nope"}'):
            with self.subTest(content=content):
                self.config.write_text(content)
                with self.assertRaises(EnrolmentError):
                    load_enrolled(self.config)

    def test_directories_with_records_that_are_not_enrolled_are_found(self):
        # The reviewer's M3: a third login must be noticed, never merged on its own.
        third = self.box.partition("cccccccc-0000-4000-8000-000000000003", "cccccccc-0000-4000-8000-0000000000c3")
        write_record(third, X)
        self.box.partition(ACCOUNT_A, ORG_B)  # an empty cross-pair
        write_record(self.box.a, X)

        found = unenrolled_with_records([self.box.a, self.box.b])

        self.assertEqual(found, {third: 1})


if __name__ == "__main__":
    unittest.main()
