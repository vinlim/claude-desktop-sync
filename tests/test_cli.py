import fcntl
import io
import os
import signal
import unittest

from session_sync.cli import Environment, install_sigterm_handler, main
from session_sync.run import Settings
from session_sync.state_store import load_state
from tests.fs_helpers import ACCOUNT_B, LONG_AGO_S, SECOND_NS, Sandbox, X, title_of, write_record


class CliTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)
        self.settings = Settings(state_dir=self.box.state_dir)
        self.running = False
        self.clock_s = LONG_AGO_S + 100_000

    def run_cli(self, *argv):
        self.clock_s += 10
        out = io.StringIO()
        env = Environment(settings=self.settings, out=out, now_ns=lambda: self.clock_s * SECOND_NS,
                          running=lambda: self.running, sessions_dir=self.box.root / "claude-code-sessions",
                          agent_plist=self.box.base / "LaunchAgents" / "agent.plist")
        return main(list(argv), env), out.getvalue()

    def enrol_both(self):
        self.assertEqual(self.run_cli("--enroll", str(self.box.a), "--enroll", str(self.box.b))[0], 0)


class Enrolling(CliTest):
    def test_list_shows_what_is_enrolled_and_what_could_be(self):
        write_record(self.box.b, X)
        self.run_cli("--enroll", str(self.box.a))

        code, text = self.run_cli("--list")

        self.assertEqual(code, 0)
        self.assertIn("enrolled      %s" % self.box.a, text)
        self.assertIn("not enrolled  %s (1 records)" % self.box.b, text)

    def test_a_bad_path_is_refused_with_the_reason(self):
        code, text = self.run_cli("--enroll", str(self.box.base))

        self.assertEqual(code, 2)
        self.assertIn("not shaped", text)

    def test_unenrolling_works(self):
        self.enrol_both()

        self.run_cli("--unenroll", str(self.box.a))

        self.assertNotIn("enrolled      %s" % self.box.a, self.run_cli("--list")[1])


class Running(CliTest):
    def test_the_default_is_a_dry_run_and_apply_writes(self):
        self.enrol_both()
        write_record(self.box.a, X)

        dry_code, dry_text = self.run_cli()
        self.assertEqual(dry_code, 0)
        self.assertIn("Dry run", dry_text)
        self.assertEqual(list(self.box.b.iterdir()), [])

        code, text = self.run_cli("--apply")
        self.assertEqual(code, 0)
        self.assertIn("done        1  create record", text)
        self.assertEqual(title_of(self.box.b, X), "t")

    def test_an_aborted_run_exits_2_and_says_why(self):
        code, text = self.run_cli("--apply")

        self.assertEqual(code, 2)
        self.assertIn("at least two", text)

    def test_failures_exit_1(self):
        self.enrol_both()
        write_record(self.box.a, X)
        os.chmod(self.box.b, 0o500)
        self.addCleanup(os.chmod, self.box.b, 0o700)

        code, text = self.run_cli("--apply")

        self.assertEqual(code, 1)
        self.assertIn("not done", text)

    def test_quiet_mode_stamps_what_it_logs_and_remembers_what_it_reported(self):
        self.enrol_both()
        write_record(self.box.a, X, activity=100)
        self.run_cli("--apply")
        self.running = True
        self.box.logged_in_as(ACCOUNT_B)
        write_record(self.box.a, X, at_s=LONG_AGO_S + 50, activity=900, title="changed")

        _, first = self.run_cli("--apply", "--quiet")
        _, second = self.run_cli("--apply", "--quiet")

        self.assertRegex(first, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\n")
        self.assertIn("waiting", first)
        self.assertEqual(second, "")
        self.assertNotEqual(load_state(self.settings.state_path).reported, "")

    def test_a_second_run_backs_off_while_one_holds_the_lock(self):
        self.enrol_both()
        write_record(self.box.a, X)
        self.box.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self.settings.lock_path, "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)

            code, text = self.run_cli("--apply")

        self.assertEqual(code, 0)
        self.assertIn("Another sync is running", text)
        self.assertEqual(list(self.box.b.iterdir()), [])


class Housekeeping(CliTest):
    def test_reset_state_keeps_the_old_file_aside(self):
        self.enrol_both()
        write_record(self.box.a, X)
        self.run_cli("--apply")

        code, text = self.run_cli("--reset-state")

        self.assertEqual(code, 0)
        self.assertFalse(self.settings.state_path.exists())
        self.assertEqual(len(list(self.box.state_dir.glob("state.json.before-reset-*"))), 1)
        self.assertIn("kept as", text)

    def test_status_reports_the_last_clean_run_and_standing_problems(self):
        self.enrol_both()
        write_record(self.box.a, X)
        self.assertIn("never", self.run_cli("--status")[1])

        self.run_cli("--apply")
        code, text = self.run_cli("--status")

        self.assertEqual(code, 0)
        self.assertIn("last clean run: 10 seconds ago", text)
        self.assertIn("background agent: not installed", text)

    def test_sigterm_unwinds_so_temp_files_are_cleaned_up(self):
        previous = signal.getsignal(signal.SIGTERM)
        self.addCleanup(signal.signal, signal.SIGTERM, previous)
        install_sigterm_handler()

        with self.assertRaises(SystemExit) as raised:
            os.kill(os.getpid(), signal.SIGTERM)
            signal.pause()

        self.assertEqual(raised.exception.code, 143)


if __name__ == "__main__":
    unittest.main()
