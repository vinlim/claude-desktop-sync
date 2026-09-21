import subprocess
import unittest
from types import SimpleNamespace

from session_sync.liveness import app_running, is_live, last_known_account
from tests.fs_helpers import ACCOUNT_A, ACCOUNT_B, Sandbox


def pgrep_exits(code):
    return lambda *args, **kwargs: SimpleNamespace(returncode=code)


def pgrep_raises(error):
    def run(*args, **kwargs):
        raise error
    return run


class AppRunning(unittest.TestCase):
    def test_a_match_means_running_and_no_match_means_not_running(self):
        self.assertTrue(app_running(run=pgrep_exits(0)))
        self.assertFalse(app_running(run=pgrep_exits(1)))

    def test_anything_else_is_read_as_running(self):
        # The reviewer's M2(c): pgrep exits 2 or 3 on its own errors.
        for code in (2, 3, 127, -9):
            with self.subTest(exit_code=code):
                self.assertTrue(app_running(run=pgrep_exits(code)))

    def test_a_pgrep_that_cannot_be_run_is_read_as_running(self):
        for error in (FileNotFoundError("pgrep"), subprocess.TimeoutExpired("pgrep", 5), PermissionError("pgrep")):
            with self.subTest(error=type(error).__name__):
                self.assertTrue(app_running(run=pgrep_raises(error)))


class LastKnownAccount(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)

    def test_it_is_read_from_the_apps_config(self):
        self.box.logged_in_as(ACCOUNT_A.upper())

        self.assertEqual(last_known_account(self.box.root), ACCOUNT_A)

    def test_what_cannot_be_read_is_none(self):
        config = self.box.root / "config.json"
        for name, content in {"missing": None, "torn": "{", "not an object": "[1]", "no key": "{}",
                              "not a string": '{"lastKnownAccountUuid": 5}'}.items():
            with self.subTest(case=name):
                self.box.root.mkdir(parents=True, exist_ok=True)
                if content is None:
                    if config.exists():
                        config.unlink()
                else:
                    config.write_text(content)

                self.assertIsNone(last_known_account(self.box.root))


class IsLive(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)

    def test_nothing_is_live_when_the_app_is_not_running(self):
        self.box.logged_in_as(ACCOUNT_A)

        self.assertFalse(is_live(self.box.a, running=False))

    def test_only_the_logged_in_accounts_partition_is_live(self):
        self.box.logged_in_as(ACCOUNT_B)

        self.assertFalse(is_live(self.box.a, running=True))
        self.assertTrue(is_live(self.box.b, running=True))

    def test_an_unreadable_login_makes_every_partition_live(self):
        self.assertTrue(is_live(self.box.a, running=True))
        self.assertTrue(is_live(self.box.b, running=True))


if __name__ == "__main__":
    unittest.main()
