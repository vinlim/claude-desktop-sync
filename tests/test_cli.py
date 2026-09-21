import fcntl
import io
import os
import signal
import unittest
from pathlib import Path

from session_sync.cli import Environment, install_sigterm_handler, main
from session_sync.run import Settings
from session_sync.state_store import load_state
from tests.fs_helpers import ACCOUNT_B, LONG_AGO_S, SECOND_NS, Sandbox, X, Y, title_of, write_record


class CliTest(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)
        self.settings = Settings(state_dir=self.box.state_dir)
        self.running = False
        self.clock_s = LONG_AGO_S + 100_000
        self.plist = self.box.base / "LaunchAgents" / "agent.plist"
        self.launchctl_calls = []
        self.agent_is_loaded = True

    def launchctl(self, command, **kwargs):
        """Stands in for launchd, so no test can touch the real one."""
        from types import SimpleNamespace
        self.launchctl_calls.append(command[1])
        return SimpleNamespace(returncode=0 if command[1] != "print" or self.agent_is_loaded else 113, stderr="")

    def pretend_the_agent_is_installed(self):
        self.plist.parent.mkdir(parents=True, exist_ok=True)
        self.plist.write_bytes(b"plist")

    def run_cli(self, *argv):
        self.clock_s += 10
        out = io.StringIO()
        env = Environment(settings=self.settings, out=out, now_ns=lambda: self.clock_s * SECOND_NS,
                          running=lambda: self.running, sessions_dir=self.box.root / "claude-code-sessions",
                          agent_plist=self.plist, launchctl=self.launchctl, quiet_out=out, lock_wait_s=0.2)
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


class StandingProblems(CliTest):
    def test_a_standing_abort_is_logged_once_with_a_timestamp(self):
        _, first = self.run_cli("--apply", "--quiet")
        _, second = self.run_cli("--apply", "--quiet")

        self.assertRegex(first, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\n")
        self.assertIn("at least two", first)
        self.assertEqual(second, "")

    def test_the_same_abort_is_logged_again_after_a_run_that_worked(self):
        self.run_cli("--enroll", str(self.box.a))
        _, first = self.run_cli("--apply", "--quiet")
        self.run_cli("--enroll", str(self.box.b))
        self.run_cli("--apply", "--quiet")
        self.run_cli("--unenroll", str(self.box.b))

        _, again = self.run_cli("--apply", "--quiet")

        self.assertIn("1 partition(s) enrolled", first)
        self.assertEqual(again.splitlines()[1:], first.splitlines()[1:], "word for word the abort that was logged before")

    def test_a_lost_record_can_be_recreated_for_one_session(self):
        self.enrol_both()
        write_record(self.box.a, X)
        self.run_cli("--apply")
        (self.box.b / ("local_%s.json" % X)).unlink()
        self.assertIn("not recreated", self.run_cli("--apply")[1])

        code, text = self.run_cli("--recreate", X)
        self.run_cli("--apply")

        self.assertEqual(code, 0)
        self.assertIn(X, text)
        self.assertEqual(title_of(self.box.b, X), "t")

    def test_session_without_prefer_is_refused_instead_of_being_ignored(self):
        self.enrol_both()

        code, text = self.run_cli("--apply", "--session", X)

        self.assertEqual(code, 2)
        self.assertIn("--prefer", text)

    def test_changing_the_sync_history_waits_for_a_run_in_flight_and_gives_up_politely(self):
        # A run saves its state at the end and would overwrite the change.
        self.enrol_both()
        write_record(self.box.a, X)
        self.run_cli("--apply")
        before = self.settings.state_path.read_bytes()
        with open(self.settings.lock_path, "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)

            recreate = self.run_cli("--recreate", X)
            reset = self.run_cli("--reset-state")

        for code, text in (recreate, reset):
            self.assertEqual(code, 2)
            self.assertIn("sync is running", text)
        self.assertEqual(self.settings.state_path.read_bytes(), before)

    def test_prefer_can_settle_one_session_and_leave_the_other_tied(self):
        self.enrol_both()
        for sid in (X, Y):
            write_record(self.box.a, sid, activity=100, title="agreed")
        self.run_cli("--apply")
        for sid in (X, Y):
            write_record(self.box.a, sid, at_s=LONG_AGO_S + 50, activity=100, title="renamed under A")
            write_record(self.box.b, sid, at_s=LONG_AGO_S + 60, activity=100, title="agreed", isArchived=True)

        self.run_cli("--apply", "--prefer", str(self.box.a), "--session", X)

        self.assertEqual(title_of(self.box.b, X), "renamed under A")
        self.assertEqual(title_of(self.box.b, Y), "agreed")


class UnattendedLog(CliTest):
    def test_a_quiet_run_appends_to_its_own_log_file(self):
        # The tool writes and caps this file itself, so nothing depends on how launchd opened it.
        self.enrol_both()
        write_record(self.box.a, X)
        out = io.StringIO()
        env = Environment(settings=self.settings, out=out, now_ns=lambda: self.clock_s * SECOND_NS,
                          running=lambda: False, sessions_dir=self.box.root / "claude-code-sessions",
                          agent_plist=self.plist, launchctl=self.launchctl)

        main(["--apply", "--quiet"], env)

        self.assertEqual(out.getvalue(), "")
        self.assertIn("create record", self.settings.log_path.read_text())


class AgentUpkeep(CliTest):
    def test_unenrolling_below_two_partitions_removes_the_agent(self):
        self.enrol_both()
        self.pretend_the_agent_is_installed()

        _, text = self.run_cli("--unenroll", str(self.box.b))

        self.assertFalse(self.plist.exists())
        self.assertIn("bootout", self.launchctl_calls)
        self.assertIn("agent was removed", text)

    def test_status_tells_an_agent_that_is_loaded_from_one_that_only_has_a_file(self):
        self.enrol_both()
        self.pretend_the_agent_is_installed()
        self.assertIn("background agent: installed and loaded", self.run_cli("--status")[1])

        self.agent_is_loaded = False

        self.assertIn("installed but not loaded", self.run_cli("--status")[1])

    def test_status_warns_about_an_installed_agent_that_has_never_had_a_clean_run(self):
        self.enrol_both()
        self.pretend_the_agent_is_installed()

        text = self.run_cli("--status")[1]

        self.assertIn("last clean run: never", text)
        self.assertIn("has never had a clean run", text)

    def test_status_warns_when_an_installed_agent_has_not_had_a_clean_run_for_a_while(self):
        self.enrol_both()
        write_record(self.box.a, X)
        self.run_cli("--apply")
        self.pretend_the_agent_is_installed()
        self.clock_s += 7200

        self.assertIn("no clean run for", self.run_cli("--status")[1])


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
        self.assertIn("lastActivityAt", text, "the message says how the next run will decide")

    def test_status_reports_the_last_clean_run_and_standing_problems(self):
        self.enrol_both()
        write_record(self.box.a, X)
        self.assertIn("never", self.run_cli("--status")[1])

        self.run_cli("--apply")
        code, text = self.run_cli("--status")

        self.assertEqual(code, 0)
        self.assertIn("last clean run: 10 seconds ago", text)
        self.assertIn("background agent: not installed", text)

    def test_sigterm_in_the_middle_of_a_write_leaves_no_staged_file_behind(self):
        # launchd stops an agent with SIGTERM. The child below stages a file, then waits inside the rename.
        import subprocess
        import sys
        import time as real_time
        target = self.box.a / ("local_%s.json" % X)
        child = subprocess.Popen([sys.executable, "-c", (
            "import os, sys, time\n"
            "sys.path.insert(0, %r)\n"
            "from pathlib import Path\n"
            "import session_sync.atomic as atomic\n"
            "from session_sync.cli import install_sigterm_handler\n"
            "install_sigterm_handler()\n"
            "atomic.os.replace = lambda *a: time.sleep(30)\n"
            "atomic.write_atomic(Path(%r), b'payload')\n") % (str(Path(__file__).resolve().parent.parent), str(target))])
        self.addCleanup(child.kill)
        deadline = real_time.time() + 10
        while not any(p.name.startswith(".sync-") for p in self.box.a.iterdir()):
            self.assertLess(real_time.time(), deadline, "the child never staged its file")
            real_time.sleep(0.02)

        child.send_signal(signal.SIGTERM)

        self.assertEqual(child.wait(timeout=10), 143)
        self.assertEqual(list(self.box.a.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
