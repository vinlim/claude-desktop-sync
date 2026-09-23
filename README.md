# claude-desktop-sync

Keeps the Code sidebar of the Claude desktop app the same under every account you log in with on one Mac.

This is an unofficial tool. It is not made, endorsed or supported by Anthropic. It reads and writes files inside the desktop app's private data directory, so read [Risks and limits](#risks-and-limits) before you use it, and keep a backup.

## The problem

If you use two Claude subscription accounts in the desktop app, each login has its own Code sidebar. Log out of one account and into the other, and the session list you were working from is gone, although nothing was deleted.

The conversations themselves are shared. Claude Code stores each transcript under `~/.claude/projects/`, in a folder named after the working directory, and no account is involved. That is why `claude --resume` in a terminal shows every session whichever account you are logged in with.

The desktop app keeps a second thing on top of those transcripts: a sidebar index, one small JSON record per session, holding a pointer to the transcript plus the title, the archived flag, the worktree and the pull request. It stores that index in one directory per login:

```
~/Library/Application Support/Claude/claude-code-sessions/<accountUuid>/<orgUuid>/
```

Two obvious shortcuts do not work. The app refuses symlinked session directories, so the two logins cannot share one directory. The app's own picker for terminal sessions can adopt a transcript into the sidebar, but it refuses any session already recorded under another login on the same machine.

That leaves one way to share a sidebar: copy the index records between the per-login directories. This tool does that, and nothing else. It never touches a transcript, and it does not log you in or out.

## How it works

You enrol the per-login directories you want kept the same. Each run scans them, decides what to do for each session, and copies records byte for byte. A record the tool places is always a verbatim copy of a file the app wrote.

The decisions do not use file times. The app rewrites a record whenever you click a session, so the newest file is often a copy with no new state. The tool compares content, and remembers the state both sides last agreed on:

- **One side changed.** The changed copy replaces the unchanged one, unless its activity is older than what it would replace. A copy can also change by going back in time, for example after a restored backup.
- **Both sides changed.** The copy whose record shows later activity wins. The app moves `lastActivityAt` only on real activity: sending a message, a turn starting, a permission answer. If activity is equal and the contents differ, the session is left alone and reported, and you choose with `--prefer`.
- **Deletes.** The app leaves a delete marker when you delete a session. If no copy was used after the delete, the other copies are retired and the marker travels. A session you re-adopt later wins over an old marker, because the app stamps it with the current time.
- **Lost records.** A record that was present before and is gone without a delete marker is never put back on its own. The app removes a record before it writes the marker, and a run can land in between.

Nothing unique is destroyed. A replaced copy is kept unless it is strictly superseded, and a retired record is always kept, under `~/.local/state/claude-desktop-session-sync/kept/`. The report names the path.

The tool stays out of the running app's way. The app holds the current login's sessions in memory and rewrites them from memory, so in that directory the tool only adds files that are missing and never replaces or removes one. For two minutes after a login change it treats every directory that way, because the app records the new login before it has finished saving the old one. It reads the time of the change from the app's own log, so a run hours after the switch does not wait. Each guard is checked again immediately before the write.

The full contract, with the facts about the app it relies on and where each was verified, is in [DESIGN.md](DESIGN.md).

## Key features

- **Dry run by default.** Nothing is written without `--apply`.
- **Explicit enrolment.** Only directories you enrol are read or written. Any other account's directory is reported and left alone.
- **Content decides, file times do not.** A click never counts as a change.
- **Conflicts keep the loser.** Later activity wins, the replaced copy is kept, and a true tie is reported and left for you.
- **Deletes stay deleted**, and a re-adopted session survives an old delete marker.
- **Safe beside the running app.** It never replaces a file in a directory the app may hold, never overwrites on create, and re-checks every guard at the moment of the write.
- **Crash safe.** Writes are staged and renamed into place, each create is written down the moment it completes, and a stopped run cleans up after itself.
- **Backups built in.** One command saves the enrolled directories, the first sync takes a backup by itself, and going back to one is a dry run until you say `--apply`.
- **Bounded.** Kept copies are pruned after 30 days and above 500 MB. The newest 10 backups are kept. The log is capped.
- **Optional background agent** that runs whenever an enrolled directory changes.
- **No dependencies.** Python 3.9 or later, standard library only. The Python that ships with the macOS developer tools is enough.

## Requirements

- macOS with the Claude desktop app
- Python 3.9 or later

## Install

```bash
git clone https://github.com/vinlim/claude-desktop-sync.git ~/.local/share/claude-desktop-session-sync
mkdir -p ~/.local/bin
ln -s ~/.local/share/claude-desktop-session-sync/claude-desktop-session-sync ~/.local/bin/claude-desktop-session-sync
```

Make sure `~/.local/bin` is on your `PATH`. The command is `claude-desktop-session-sync`.

## Use

1. Log in to each account in the desktop app at least once and start a Code session there, so its directory exists. Then list the candidates.

   ```bash
   claude-desktop-session-sync --list
   ```

2. Enrol each directory that is yours to sync. Switching accounts also leaves empty folders that pair one account with the other's org. They hold no sessions and are not listed.

   ```bash
   claude-desktop-session-sync --enroll "<path from --list>" --enroll "<the other path>"
   ```

3. Look at what a run would do. This writes nothing.

   ```bash
   claude-desktop-session-sync --verbose
   ```

4. Apply it.

   ```bash
   claude-desktop-session-sync --apply
   ```

   The first run saves a backup of the enrolled directories before it writes anything, and prints its name. If you logged in less than two minutes ago, or the app's log no longer holds the line for your login (it rotates every few days), it creates missing records and defers replacing any; run it again two minutes later.

5. Make the app read the result. The app reads a login's directory only when that login initialises, so quit and reopen the app, or log out and in.

From then on, run `claude-desktop-session-sync --apply` before you switch accounts. Or install the agent, which runs whenever an enrolled directory changes:

```bash
claude-desktop-session-sync --install-agent
```

### Backups and going back

```bash
claude-desktop-session-sync --backup --note "before tidying"
claude-desktop-session-sync --backups
claude-desktop-session-sync --restore 20260922-101500            # shows what would change
claude-desktop-session-sync --restore 20260922-101500 --apply    # quit the desktop app first
```

A backup holds every record, delete marker and temp file of the enrolled directories, plus the tool's sync history, in one zip under `~/.local/state/claude-desktop-session-sync/backups/`. Taking one only reads the app's files, so it is safe while the app runs. The newest 10 are kept.

A restore rolls every enrolled directory back together, along with the sync history. One account restored alone would be overwritten from the other at the next sync. A restore refuses while the desktop app is running, because the app would write its own copy of the sessions over the restored files. It checks every file in the archive against its checksum before it writes, and it saves the present first, so a restore can itself be undone with the id it prints. It refuses when a file cannot be read, because that saved present would not hold it. If a file cannot be written or removed, the restore stops there and says which one; running the same restore again finishes it.

A restore brings back sidebar entries. It cannot bring back a conversation whose transcript the app deleted along with the session. It leaves alone what this tool never syncs: scheduled tasks, the backlog, the archived-sessions index and the app's worktree registry.

### Commands

| Command | What it does |
|---|---|
| (no flags) | Dry run: shows what would change |
| `--apply` | Writes the changes |
| `--verbose` | Lists every action |
| `--status` | Enrolled directories, the agent, the last clean run, standing problems |
| `--list` | Enrolled directories and candidates |
| `--enroll PATH`, `--unenroll PATH` | Adds or removes a directory. Waits for a run in flight to finish first |
| `--prefer PARTITION` | Settles tied conflicts in favour of one directory. Add `--session ID` to settle one session |
| `--recreate ID` | Lets the next run put back a session reported as gone without a delete marker |
| `--backup` | Saves the enrolled directories and the sync history. Add `--note "text"` to find it by later |
| `--backups` | Lists the backups |
| `--restore ID` | Goes back to a backup. A dry run unless `--apply` is given. The desktop app must be quit |
| `--install-agent`, `--uninstall-agent` | Adds or removes the launchd agent |
| `--reset-state` | Forgets the sync history. The old file is kept |
| `--quiet` | For unattended runs: logs writes, and each standing problem once |

`PARTITION` is a path, or the short label a run prints, such as `1a2b3c4d/9f8e7d6c`.

### Reading the output

```
1a2b3c4d/9f8e7d6c   212 records   40 delete markers
5e6f7a8b/0c1d2e3f   215 records   40 delete markers  (in use by the running app)
planned     3  create record -> 1a2b3c4d/9f8e7d6c
Dry run. Pass --apply to write.
```

A line that starts with a session id is something the tool left alone on purpose, with the reason: the directory is in use, the copies are tied, the record was lost without a delete marker, a copy cannot be read, or a copy's last activity is dated in the future. `In sync.` means there was nothing to do.

## Where it keeps things

Everything the tool writes for itself is in `~/.local/state/claude-desktop-session-sync/`:

| File | Purpose |
|---|---|
| `config.json` | The enrolled directories |
| `state.json` | What each side last agreed on, and the login last seen |
| `kept/` | Copies that were replaced or retired |
| `backups/` | Backups taken with `--backup`, before the first sync, and before each restore |
| `agent.log` | The unattended log |

To remove the tool, run `--uninstall-agent`, then delete that directory, the clone and the symlink. The records it copied stay where they are and are ordinary app records.

## What it does not sync

Cowork sessions, scheduled tasks, the app's backlog and its archived-sessions index are left alone. Transcripts are shared already.

## Risks and limits

- **It depends on how the app stores things.** The layout and behaviour it relies on were checked against desktop app 2.2553.1. An app update can change them. After a major update, do a dry run and read it before you apply.
- **It is new.** It has a test suite and its rules have been reviewed, but it has had little use outside its author's machine. Take a backup with `--backup` before anything you are unsure about.
- **Native multi-account mode.** If the app lets you switch accounts without logging out, it keeps the previous account's running sessions in memory. Copied records with the same ids confuse that. This tool is for switching by logging out and in. If you get a native account switcher, stop using it.
- **Remote Control links** belong to the account that opened them. Archiving or deleting such a session from the other account asks the server to clean up a link that account does not own. The app ignores the refusal.
- **A change that moves no activity**, such as a rename, can be undone by a copy that went back in time with the same activity. The replaced copy is kept and the report says where.
- **Deleting an imported session** leaves its transcript on disk while the other account's copy still claims it. That is the app's behaviour.
- **Two app instances at once** can both change one session. The later activity wins and the other copy is kept.
- **Using several accounts** is between you and Anthropic's terms. This tool only copies files that are already on your disk.

## Tests

```bash
cd ~/.local/share/claude-desktop-session-sync
python3 -m unittest discover -s tests -t .
```

The decision rules are pure functions in `session_sync/planner.py` and `session_sync/settle.py`, tested without touching the disk. Everything else runs against throwaway directories. No test touches the real app or launchd.

## Author

[Vin Lim](https://astralab.co/authors/vin-lim)

If it saves you time, you can support it on [Ko-fi](https://ko-fi.com/vinlim).

## License

[0BSD](LICENSE). Use it for anything, with or without credit. It comes with no warranty, and the author accepts no liability for what it does, including to your data.

Claude is a trademark of Anthropic. This project is not affiliated with Anthropic.
