import os
import re
import subprocess
import time
import unittest
from types import SimpleNamespace

from session_sync.liveness import APP_BINARY, app_running, is_live, last_known_account, login_dated_by_app, observe_logins
from tests.fs_helpers import ACCOUNT_A, ACCOUNT_B, LONG_AGO_S, SECOND_NS, Sandbox

NOW_S = LONG_AGO_S + 100_000
NOW_MS = NOW_S * 1000
MAIN = "/Applications/Claude.app/Contents/MacOS/Claude"
HELPERS = ("/Applications/Claude.app/Contents/Frameworks/Claude Helper (Renderer).app/Contents/MacOS/Claude Helper (Renderer)",
           "/Applications/Claude.app/Contents/Frameworks/Claude Helper.app/Contents/MacOS/Claude Helper",
           "/Users/me/Library/Application Support/Claude/claude-code/2.1.275/claude.app/Contents/MacOS/claude",
           "/Users/me/.local/bin/claude",
           "/Applications/Claude.app/Contents/MacOS/ClaudeUpdater")


def ps_lists(*paths, code=0):
    return lambda *args, **kwargs: SimpleNamespace(returncode=code, stdout="\n".join(paths) + "\n")


def ps_raises(error):
    def run(*args, **kwargs):
        raise error
    return run


class AppRunning(unittest.TestCase):
    """The process list is read by executable path. pgrep cannot see the main process's
    arguments on every macOS, and a miss there read as 'not running'."""

    def test_the_main_binary_in_the_list_means_running(self):
        self.assertTrue(app_running(run=ps_lists(*HELPERS, MAIN)))
        self.assertTrue(app_running(run=ps_lists("/Users/me/Applications/Claude.app/Contents/MacOS/Claude")))

    def test_helpers_and_the_command_line_tool_alone_do_not(self):
        self.assertFalse(app_running(run=ps_lists(*HELPERS)))
        self.assertFalse(app_running(run=ps_lists()))

    def test_a_listing_that_failed_is_read_as_running(self):
        for code in (1, 2, 127, -9):
            with self.subTest(exit_code=code):
                self.assertTrue(app_running(run=ps_lists(*HELPERS, code=code)))

    def test_a_ps_that_cannot_be_run_is_read_as_running(self):
        for error in (FileNotFoundError("ps"), subprocess.TimeoutExpired("ps", 5), PermissionError("ps")):
            with self.subTest(error=type(error).__name__):
                self.assertTrue(app_running(run=ps_raises(error)))

    def test_the_real_ps_accepts_the_flags(self):
        self.assertEqual(subprocess.run(["ps", "-axo", "comm="], capture_output=True).returncode, 0)

    def test_the_pattern_names_the_main_binary_only(self):
        self.assertIsNotNone(APP_BINARY.search(MAIN))
        for path in HELPERS:
            with self.subTest(path=path):
                self.assertIsNone(APP_BINARY.search(path))


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

    def test_a_change_is_dated_by_the_apps_log_when_the_log_names_that_login(self):
        # The app writes a line for every login change. The tool's own first sighting can be
        # hours late, and a grace counted from it holds up every write for two minutes.
        logins = {self.root: (ACCOUNT_A, 1000)}
        self.box.logged_in_as(ACCOUNT_B)
        log = self.app_log(login(NOW_S - 3600, ACCOUNT_A, ACCOUNT_B))

        observe_logins([self.box.a], logins, now_ms=NOW_MS, app_log=log)

        self.assertEqual(logins, {self.root: (ACCOUNT_B, (NOW_S - 3600) * 1000)})

    def test_the_last_login_line_counts_and_a_logout_line_is_not_a_login(self):
        logins = {}
        self.box.logged_in_as(ACCOUNT_B)
        log = self.app_log(login(NOW_S - 7200, ACCOUNT_A, ACCOUNT_B), logout(NOW_S - 5000, ACCOUNT_B),
                           login(NOW_S - 4000, ACCOUNT_B, ACCOUNT_B), logout(NOW_S - 100, ACCOUNT_B))

        observe_logins([self.box.a], logins, now_ms=NOW_MS, app_log=log)

        self.assertEqual(logins, {self.root: (ACCOUNT_B, (NOW_S - 4000) * 1000)})

    def test_a_log_that_does_not_date_this_login_falls_back_to_now(self):
        cases = {
            "names another login": [login(NOW_S - 3600, ACCOUNT_B, ACCOUNT_A)],
            "only a logout": [logout(NOW_S - 3600, ACCOUNT_A)],
            "no login line": ["2026-09-24 00:00:00 [info] something else"],
            "empty": [],
            "garbled stamp": ["garbage [info] [account] Login-state transition (loggedOut: true \u2192 false, uuid: x \u2192 %s), clearing" % ACCOUNT_B],
        }
        for name, lines in cases.items():
            with self.subTest(case=name):
                logins = {self.root: (ACCOUNT_A, 1000)}
                self.box.logged_in_as(ACCOUNT_B)
                observe_logins([self.box.a], logins, now_ms=NOW_MS, app_log=self.app_log(*lines))
                self.assertEqual(logins, {self.root: (ACCOUNT_B, NOW_MS)})
        with self.subTest(case="missing file"):
            logins = {self.root: (ACCOUNT_A, 1000)}
            observe_logins([self.box.a], logins, now_ms=NOW_MS, app_log=self.box.base / "no-such.log")
            self.assertEqual(logins, {self.root: (ACCOUNT_B, NOW_MS)})
        with self.subTest(case="no log given"):
            logins = {self.root: (ACCOUNT_A, 1000)}
            observe_logins([self.box.a], logins, now_ms=NOW_MS, app_log=None)
            self.assertEqual(logins, {self.root: (ACCOUNT_B, NOW_MS)})

    def test_a_login_the_log_dates_in_the_future_is_dated_now(self):
        logins = {}
        self.box.logged_in_as(ACCOUNT_B)

        observe_logins([self.box.a], logins, now_ms=NOW_MS, app_log=self.app_log(login(NOW_S + 600, ACCOUNT_A, ACCOUNT_B)))

        self.assertEqual(logins, {self.root: (ACCOUNT_B, NOW_MS)})

    def test_only_the_end_of_a_large_log_is_read(self):
        # The log runs to megabytes; the login line is near the end.
        self.box.logged_in_as(ACCOUNT_B)
        filler = ["2026-09-24 00:00:00 [info] filler " + "x" * 200] * 6000
        log = self.app_log(*filler, login(NOW_S - 3600, ACCOUNT_A, ACCOUNT_B))
        self.assertGreater(log.stat().st_size, 1_000_000)

        self.assertEqual(login_dated_by_app(log, ACCOUNT_B, NOW_MS), (NOW_S - 3600) * 1000)

    def app_log(self, *lines):
        path = self.box.base / "Logs" / "main.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
        return path


def login(at_s, from_account, to_account):
    return "%s [info] [account] Login-state transition (loggedOut: true \u2192 false, uuid: %s \u2192 %s), clearing oauth cache" % (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at_s)), from_account, to_account.upper())


def logout(at_s, from_account):
    return "%s [info] [account] Login-state transition (loggedOut: false \u2192 true, uuid: %s \u2192 <none>), clearing oauth cache" % (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(at_s)), from_account)


if __name__ == "__main__":
    unittest.main()
