import os
import re
import subprocess
import unittest
from types import SimpleNamespace

from session_sync.liveness import APP_PROCESS, app_running, is_live, last_known_account, observe_logins
from tests.fs_helpers import ACCOUNT_A, ACCOUNT_B, LONG_AGO_S, SECOND_NS, Sandbox

NOW_S = LONG_AGO_S + 100_000
NOW_MS = NOW_S * 1000


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
        self.root = str(self.box.root)

    def live(self, partition, logins, running=True):
        return is_live(partition, running=running, now_ms=NOW_MS, logins=logins)

    def settled(self, account):
        return {self.root: (account, NOW_MS - 10 * 60 * 1000)}

    def test_nothing_is_live_when_the_app_is_not_running(self):
        self.box.logged_in_as(ACCOUNT_A)

        self.assertFalse(self.live(self.box.a, {}, running=False))

    def test_only_the_logged_in_accounts_partition_is_live_once_the_login_has_settled(self):
        self.box.logged_in_as(ACCOUNT_B)

        self.assertFalse(self.live(self.box.a, self.settled(ACCOUNT_B)))
        self.assertTrue(self.live(self.box.b, self.settled(ACCOUNT_B)))

    def test_the_apps_frequent_config_writes_do_not_make_the_other_partition_live(self):
        # The app rewrites its config about once a minute for reasons that have nothing to do
        # with the login, so the file's time says nothing about a login change.
        self.box.logged_in_as(ACCOUNT_B, at_s=NOW_S - 1)

        self.assertFalse(self.live(self.box.a, self.settled(ACCOUNT_B)))

    def test_a_login_change_seen_recently_makes_every_partition_live(self):
        # F9: the login flips before the previous login's pending saves are flushed.
        self.box.logged_in_as(ACCOUNT_B)

        self.assertTrue(self.live(self.box.a, {self.root: (ACCOUNT_B, NOW_MS - 30_000)}))
        self.assertFalse(self.live(self.box.a, {self.root: (ACCOUNT_B, NOW_MS - 121_000)}))

    def test_a_login_the_tool_has_not_recorded_yet_makes_every_partition_live(self):
        # The first run ever, or the login flipping in the middle of a run.
        self.box.logged_in_as(ACCOUNT_B)

        self.assertTrue(self.live(self.box.a, {}))
        self.assertTrue(self.live(self.box.a, self.settled(ACCOUNT_A)))

    def test_an_unreadable_login_makes_every_partition_live(self):
        self.assertTrue(self.live(self.box.a, self.settled(ACCOUNT_B)))
        self.box.logged_in_as(ACCOUNT_B)
        (self.box.root / "config.json").write_text("{ torn")
        self.assertTrue(self.live(self.box.a, self.settled(ACCOUNT_B)))

    def test_a_config_write_in_flight_makes_every_partition_live(self):
        # The app records a change in a journal first and commits the config file afterwards.
        self.box.logged_in_as(ACCOUNT_B)
        (self.box.root / "config.json.journal").write_text("{}")

        self.assertTrue(self.live(self.box.a, self.settled(ACCOUNT_B)))


class ObservingTheLogin(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)
        self.root = str(self.box.root)

    def test_the_first_sighting_and_every_change_are_dated_now(self):
        logins = {}
        self.box.logged_in_as(ACCOUNT_A)
        observe_logins([self.box.a, self.box.b], logins, now_ms=1000)
        self.assertEqual(logins, {self.root: (ACCOUNT_A, 1000)})

        observe_logins([self.box.a, self.box.b], logins, now_ms=5000)
        self.assertEqual(logins, {self.root: (ACCOUNT_A, 1000)}, "the same login keeps its date")

        self.box.logged_in_as(ACCOUNT_B)
        observe_logins([self.box.a, self.box.b], logins, now_ms=9000)
        self.assertEqual(logins, {self.root: (ACCOUNT_B, 9000)})

    def test_an_unreadable_login_leaves_the_memory_alone(self):
        logins = {self.root: (ACCOUNT_A, 1000)}

        observe_logins([self.box.a], logins, now_ms=9000)

        self.assertEqual(logins, {self.root: (ACCOUNT_A, 1000)})


if __name__ == "__main__":
    unittest.main()
