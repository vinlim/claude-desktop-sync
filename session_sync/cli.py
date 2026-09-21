"""Command line entry: parses arguments, takes the lock, prints the report."""
import argparse
import fcntl
import signal
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, TextIO

from session_sync import agent
from session_sync.enrolment import EnrolmentError, SESSIONS_DIR, enrol, load_enrolled, unenrol, unenrolled_with_records
from session_sync.liveness import app_running
from session_sync.report import render
from session_sync.run import RunAborted, Settings, remember_reported, sync
from session_sync.state_store import StateUnusable, load_state

DEFAULT_STATE_DIR = Path.home() / ".local" / "state" / "claude-desktop-session-sync"
DEFAULT_SESSIONS_DIR = Path.home() / "Library" / "Application Support" / "Claude" / SESSIONS_DIR
ENTRY_SCRIPT = Path(__file__).resolve().parent.parent / "claude-desktop-session-sync"

DESCRIPTION = """Keep the Claude desktop app's Code sidebar the same under every account.

Copies sidebar records between the enrolled per-account directories. Dry run by default.
Rules and guarantees: see DESIGN.md next to this tool."""


@dataclass
class Environment:
    settings: Settings = field(default_factory=lambda: Settings(state_dir=DEFAULT_STATE_DIR))
    out: TextIO = sys.stdout
    now_ns: Callable[[], int] = time.time_ns
    running: Callable[[], bool] = app_running
    sessions_dir: Path = DEFAULT_SESSIONS_DIR
    agent_plist: Path = agent.PLIST


def install_sigterm_handler() -> None:
    """launchd stops agents with SIGTERM. Exiting through SystemExit lets temp-file cleanup run."""
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))


def main(argv: List[str], env: Optional[Environment] = None) -> int:
    if env is None:
        env = Environment()
        install_sigterm_handler()
    args = _parser().parse_args(argv)
    say = lambda text: print(text, file=env.out)  # noqa: E731

    try:
        if args.enroll or args.unenroll:
            return _change_enrolment(args, env, say)
        if args.list:
            return _list(env, say)
        if args.reset_state:
            return _reset_state(env, say)
        if args.status:
            return _status(env, say)
        if args.install_agent:
            return _install_agent(env, say)
        if args.uninstall_agent:
            say("Removed the agent." if agent.uninstall(env.agent_plist) else "No agent was installed.")
            return 0
        return _run(args, env, say)
    except (RunAborted, EnrolmentError, agent.AgentError) as error:
        say(str(error))
        return 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude-desktop-session-sync", description=DESCRIPTION,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write changes (default is a dry run)")
    parser.add_argument("--verbose", action="store_true", help="list every action")
    parser.add_argument("--quiet", action="store_true", help="print only writes, and standing problems once")
    parser.add_argument("--prefer", metavar="PARTITION", help="settle tied conflicts in favour of this partition")
    parser.add_argument("--list", action="store_true", help="show enrolled partitions and candidates")
    parser.add_argument("--enroll", action="append", metavar="PATH", help="allow a partition to be synced")
    parser.add_argument("--unenroll", action="append", metavar="PATH", help="stop syncing a partition")
    parser.add_argument("--status", action="store_true", help="last clean run, agent, standing problems")
    parser.add_argument("--reset-state", action="store_true", help="forget sync history (the old file is kept)")
    parser.add_argument("--install-agent", action="store_true", help="sync in the background on every change")
    parser.add_argument("--uninstall-agent", action="store_true", help="remove the background agent")
    return parser


def _run(args, env: Environment, say) -> int:
    env.settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.quiet:
        agent.cap_log(env.settings.log_path)
    with open(env.settings.lock_path, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if not args.quiet:
                say("Another sync is running.")
            return 0
        report = sync(env.settings, apply=args.apply, prefer=args.prefer, now_ns=env.now_ns, running=env.running)
        previous = _reported(env) if args.quiet else ""
        text, digest = render(report, verbose=args.verbose, quiet=args.quiet, previous_digest=previous)
        if args.quiet and args.apply:
            remember_reported(env.settings, digest)
    if text:
        say("%s\n%s" % (_timestamp(env), text) if args.quiet else text)
    return 1 if report.failures else 0


def _reported(env: Environment) -> str:
    try:
        return load_state(env.settings.state_path).reported
    except StateUnusable:
        return ""


def _change_enrolment(args, env: Environment, say) -> int:
    for path in args.enroll or []:
        enrol(env.settings.config_path, Path(path))
    for path in args.unenroll or []:
        unenrol(env.settings.config_path, Path(path))
    enrolled = load_enrolled(env.settings.config_path)
    if agent.is_installed(env.agent_plist) and len(enrolled) >= 2:
        agent.install(ENTRY_SCRIPT, enrolled, env.settings.log_path, env.agent_plist)
        say("The background agent now watches the new set.")
    return _list(env, say)


def _list(env: Environment, say) -> int:
    enrolled = load_enrolled(env.settings.config_path)
    for partition in enrolled:
        say("enrolled      %s" % partition)
    for partition, count in sorted(unenrolled_with_records(enrolled, also_under=[env.sessions_dir]).items()):
        say("not enrolled  %s (%d records)" % (partition, count))
    if not enrolled:
        say("Nothing enrolled. Enrol each account's directory with --enroll PATH.")
    return 0


def _reset_state(env: Environment, say) -> int:
    path = env.settings.state_path
    if not path.exists():
        say("There is no sync history to forget.")
        return 0
    aside = path.with_name("%s.before-reset-%s" % (path.name, time.strftime("%Y%m%d-%H%M%S",
                                                                           time.localtime(env.now_ns() // 10 ** 9))))
    path.rename(aside)
    say("Sync history forgotten. The old file is kept as %s" % aside)
    return 0


def _status(env: Environment, say) -> int:
    enrolled = load_enrolled(env.settings.config_path)
    say("enrolled partitions: %d" % len(enrolled))
    say("background agent: %s" % ("installed" if agent.is_installed(env.agent_plist) else "not installed"))
    try:
        last = load_state(env.settings.state_path).last_success_ms
    except StateUnusable as error:
        say(str(error))
        return 2
    say("last clean run: %s" % (_ago(env.now_ns() // 1_000_000 - last) if last else "never"))
    if len(enrolled) >= 2:
        report = sync(env.settings, apply=False, now_ns=env.now_ns, running=env.running)
        say(render(report)[0])
    return 0


def _install_agent(env: Environment, say) -> int:
    enrolled = load_enrolled(env.settings.config_path)
    if len(enrolled) < 2:
        raise EnrolmentError("Enrol at least two partitions before installing the agent.")
    env.settings.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    agent.install(ENTRY_SCRIPT, enrolled, env.settings.log_path, env.agent_plist)
    say("Installed %s\nIt logs to %s" % (env.agent_plist, env.settings.log_path))
    return 0


def _timestamp(env: Environment) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(env.now_ns() // 10 ** 9))


def _ago(milliseconds: int) -> str:
    seconds = max(0, milliseconds // 1000)
    for size, unit in ((86400, "days"), (3600, "hours"), (60, "minutes")):
        if seconds >= 2 * size:
            return "%d %s ago" % (seconds // size, unit)
    return "%d seconds ago" % seconds
