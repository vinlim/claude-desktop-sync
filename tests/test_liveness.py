import os
import re
import subprocess
import unittest
from types import SimpleNamespace

from session_sync.liveness import APP_PROCESS, app_running, is_live, last_known_account
from tests.fs_helpers import ACCOUNT_A, ACCOUNT_B, LONG_AGO_S, SECOND_NS, Sandbox

NOW_S = LONG_AGO_S + 100_000
NOW_NS = NOW_S * SECOND_NS


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
        # pgrep exits 2 or 3 on its own errors.
        for code in (2, 3, 127, -9):
            with self.subTest(exit_code=code):
                self.assertTrue(app_running(run=pgrep_exits(code)))

    def test_a_pgrep_that_cannot_be_run_is_read_as_running(self):
        for error in (FileNotFoundError("pgrep"), subprocess.TimeoutExpired("pgrep", 5), PermissionError("pgrep")):
            with self.subTest(error=type(error).__name__):
                self.assertTrue(app_running(run=pgrep_raises(error)))


class AppProcessPattern(unittest.TestCase):
    """A pattern that matched nothing would exit 1, which reads as 'not running'."""

    def test_it_matches_the_main_binary_with_or_without_arguments(self):
        for command in ("/Applications/Claude.app/Contents/MacOS/Claude",
                        "/Applications/Claude.app/Contents/MacOS/Claude --user-data-dir=/tmp/second",
                        "/Users/me/Applications/Claude.app/Contents/MacOS/Claude"):
            with self.subTest(command=command):
                self.assertIsNotNone(re.search(APP_PROCESS, command))

    def test_it_matches_neither_helpers_nor_the_command_line_tool(self):
        for command in (
                "/Applications/Claude.app/Contents/Frameworks/Claude Helper (Renderer).app/Contents/MacOS/"
                "Claude Helper (Renderer) --type=renderer",
                "/Applications/Claude.app/Contents/Frameworks/Claude Helper.app/Contents/MacOS/Claude Helper",
                "/Users/me/Library/Application Support/Claude/claude-code/2.1.275/claude.app/Contents/MacOS/claude",
                "/Users/me/.local/bin/claude --resume",
                "/Applications/Claude.app/Contents/MacOS/ClaudeUpdater"):
            with self.subTest(command=command):
                self.assertIsNone(re.search(APP_PROCESS, command))

    def test_the_real_pgrep_accepts_the_pattern(self):
        # Exit 2 or 3 would mean pgrep itself rejected the expression.
        code = subprocess.run(["pgrep", "-f", APP_PROCESS], capture_output=True).returncode

        self.assertIn(code, (0, 1))


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
        self.box.logged_in_as(ACCOUNT_A, at_s=NOW_S - 1)

        self.assertFalse(is_live(self.box.a, running=False, now_ns=NOW_NS))

    def test_only_the_logged_in_accounts_partition_is_live(self):
        self.box.logged_in_as(ACCOUNT_B)

        self.assertFalse(is_live(self.box.a, running=True, now_ns=NOW_NS))
        self.assertTrue(is_live(self.box.b, running=True, now_ns=NOW_NS))

    def test_an_unreadable_login_makes_every_partition_live(self):
        self.assertTrue(is_live(self.box.a, running=True, now_ns=NOW_NS))
        self.assertTrue(is_live(self.box.b, running=True, now_ns=NOW_NS))

    def test_a_config_that_exists_but_cannot_be_parsed_makes_every_partition_live(self):
        self.box.logged_in_as(ACCOUNT_B)
        config = self.box.root / "config.json"
        long_ago = os.lstat(config).st_mtime_ns
        config.write_text("{ torn")
        os.utime(config, ns=(long_ago, long_ago))

        self.assertTrue(is_live(self.box.a, running=True, now_ns=NOW_NS))

    def test_right_after_the_app_wrote_its_config_every_partition_is_live(self):
        # F9: the login flips before the previous login's pending saves are flushed, so for a
        # moment the app still writes into the partition that no longer looks like its own.
        self.box.logged_in_as(ACCOUNT_B, at_s=NOW_S - 30)

        self.assertTrue(is_live(self.box.a, running=True, now_ns=NOW_NS))

    def test_the_grace_period_ends(self):
        self.box.logged_in_as(ACCOUNT_B, at_s=NOW_S - 121)

        self.assertFalse(is_live(self.box.a, running=True, now_ns=NOW_NS))

    def test_a_config_whose_time_cannot_be_read_makes_every_partition_live(self):
        from unittest import mock
        self.box.logged_in_as(ACCOUNT_B)

        with mock.patch("session_sync.liveness.os.lstat", side_effect=PermissionError("no")):
            self.assertTrue(is_live(self.box.a, running=True, now_ns=NOW_NS))

    def test_a_config_dated_in_the_future_is_treated_as_just_written(self):
        self.box.logged_in_as(ACCOUNT_B, at_s=NOW_S + 500)

        self.assertTrue(is_live(self.box.a, running=True, now_ns=NOW_NS))


if __name__ == "__main__":
    unittest.main()
