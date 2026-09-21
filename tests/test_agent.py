import unittest
from pathlib import Path
from types import SimpleNamespace

from session_sync.agent import AgentError, agent_definition, cap_log, choose_python
from tests.fs_helpers import Sandbox


def runner(results):
    """results: interpreter path -> exit code, or an exception to raise."""
    def run(command, **kwargs):
        outcome = results[command[0]]
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(returncode=outcome)
    return run


class ChoosePython(unittest.TestCase):
    def test_the_first_interpreter_that_runs_and_is_new_enough_is_used(self):
        chosen = choose_python(["/usr/bin/python3", "/opt/python3"],
                               run=runner({"/usr/bin/python3": 0, "/opt/python3": 0}))

        self.assertEqual(chosen, "/usr/bin/python3")

    def test_an_interpreter_that_is_missing_or_too_old_is_skipped(self):
        # /usr/bin/python3 can be a stub that only offers to install developer tools.
        chosen = choose_python(["/usr/bin/python3", "/old/python3", "/opt/python3"],
                               run=runner({"/usr/bin/python3": FileNotFoundError(), "/old/python3": 1,
                                           "/opt/python3": 0}))

        self.assertEqual(chosen, "/opt/python3")

    def test_no_usable_interpreter_is_an_error_that_says_what_was_tried(self):
        with self.assertRaises(AgentError) as raised:
            choose_python(["/usr/bin/python3"], run=runner({"/usr/bin/python3": 1}))

        self.assertIn("/usr/bin/python3", str(raised.exception))


class Definition(unittest.TestCase):
    def test_the_agent_applies_quietly_and_watches_exactly_the_enrolled_partitions(self):
        partitions = [Path("/data/a"), Path("/data/b")]

        definition = agent_definition("/usr/bin/python3", Path("/tool/entry"), partitions, Path("/state/agent.log"))

        self.assertEqual(definition["ProgramArguments"], ["/usr/bin/python3", "/tool/entry", "--apply", "--quiet"])
        self.assertEqual(definition["WatchPaths"], ["/data/a", "/data/b"])
        self.assertEqual((definition["StandardOutPath"], definition["StandardErrorPath"]),
                         ("/state/agent.log", "/state/agent.log"))
        self.assertGreaterEqual(definition["ThrottleInterval"], 10)
        self.assertTrue(definition["StartInterval"] > 0 and definition["RunAtLoad"])


class CapLog(unittest.TestCase):
    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)
        self.log = self.box.base / "agent.log"

    def test_a_log_over_the_cap_keeps_its_newest_whole_lines(self):
        self.log.write_text("".join("line %04d\n" % n for n in range(1000)))

        cap_log(self.log, max_bytes=2000, keep_bytes=500)

        kept = self.log.read_text().splitlines()
        self.assertEqual(kept[-1], "line 0999")
        self.assertTrue(all(line.startswith("line ") and len(line) == 9 for line in kept), "no torn first line")
        self.assertLessEqual(self.log.stat().st_size, 500)

    def test_a_small_or_missing_log_is_left_alone(self):
        cap_log(self.log, max_bytes=2000, keep_bytes=500)
        self.log.write_text("short\n")

        cap_log(self.log, max_bytes=2000, keep_bytes=500)

        self.assertEqual(self.log.read_text(), "short\n")


if __name__ == "__main__":
    unittest.main()
