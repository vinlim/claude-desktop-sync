# claude-desktop-session-sync: design

The contract this tool is built and reviewed against. Rules are numbered so tests and review findings can cite them.

## Purpose

The Claude desktop app keeps its Code sidebar as one index per login. One person using two subscription accounts sees a different sidebar under each. This tool keeps the sidebars identical by copying index records between the per-login directories. Transcripts are shared already and are never touched.

## Facts about the app that the design relies on

Verified against desktop app 2.2553.1 (main-process JavaScript) on 2026-09-21. Re-verify after a major app update: every rule below leans on at least one of these.

| # | Fact | Where verified |
|---|------|----------------|
| F1 | A sidebar index lives at `<userData>/claude-code-sessions/<accountUuid>/<orgUuid>/`. The path is built from the login; symlinked components are refused (`O_NOFOLLOW`, `lstat` walk). | `getStorageDir`, `Ci`, `xi` |
| F2 | A record is `local_<id>.json`. The loader checks no account, and it points at a transcript through `cliSessionId`. Records can still hold account-linked data: `bridgeSessionIds`, `remoteMcpServersConfig`, `envScopeId`, `scheduledTaskId`, and optionally `emailAddress` (none on disk at the time of writing). | loader `uS`; key census over 794 records |
| F3 | A delete removes `local_<id>.json.tmp` and `local_<id>.json` first, then writes `deleted_<id>` files holding the delete time in epoch ms. Work happens between the two steps, longer for imported sessions. | session delete routine, `ax` |
| F4 | Tombstone ids are filtered by sessions loaded in memory for the current login only, so a copy under another login does not suppress a tombstone. | `cliSessionIdsClaimedByLoadedSessions({localOnly:true})` |
| F5 | The app reads a partition when a login initialises, has no directory watcher, and rewrites a record from memory on every save. Memory wins over disk for a loaded session. The loader never reads tombstones: a record beside `deleted_<id>` still loads. | `loadSessionRecords`, `writeSessionToDisk` |
| F6 | Making a session visible saves its record with a new `lastFocusedAt`, so file mtime says nothing about which copy has newer state. At load each login stamps its own `errorAt` on a side session that never started. On every focus the app also replaces `remoteMcpServersConfig` with the connectors of the login in use (`replaceRemoteMcpServers`; entries hold name, url, uuid and tools), so with two accounts a plain click changes that field. Which tools the user enabled is a separate key, `enabledMcpTools`. `lastActivityAt` moves only on real activity (19 assignment sites: sending, turn start, frames from the CLI, permission answers, rewind and clear, a session closing on its own). | `setSessionVisibility`, `loadSessionRecords`, assignment census |
| F7 | Saves are write-temp-then-rename, with a direct-write fallback on some errors. At startup the app promotes an orphaned `local_*.json.tmp` younger than 30 days to a live record, with no tombstone check. | `fS`, persistence writer |
| F8 | The app's own picker for CLI transcripts excludes, and refuses to adopt, any session id recorded under any login on the install. Copying records is the only way to share them. | `list`, `resolveForResume`, `lx` |
| F9 | When the login changes the app records `lastKnownAccountUuid` before the previous login's pending saves are flushed. The store writes `config.json.journal` synchronously and commits `config.json` afterwards, so for a moment the file still names the old login. The value is not cleared at logout. The app also rewrites `config.json` about once a minute for unrelated reasons (measured: 10 writes in 15 minutes), so the file's time says nothing about a login change. | `Io.set(Qbe, …)`, `doInitialize`, the store's persist and commit, stat sampling |
| F10 | Only `adoptCliSession` removes tombstones (`cx`), under the current login only. The record it creates is `local_<cliId>.json` with `createdAt`, `lastActivityAt` and `indexedAt` all set to the current time. | `adoptCliSession` |
| F11 | The app ignores files in a partition that do not start with `local_` or `deleted_`. Record readers use plain `open`, so a file that briefly has two hard links is read normally. | every reader filters on the prefix; `nx`, `lx` |
| F12 | With native multi-account mode (more than one signed-in account), a login change parks running sessions: they leave the loaded set and keep saving into their own partition. Logout followed by login does not park. | `onAccountOrgChanged`, `parked`, `storageDirFor` |
| F13 | Bridge ids in a record are sent to the Remote Control API with the current login's token when such a session is archived or deleted, not at load. | `cleanupRemoteBridgeSessions` callers |

## Model

- **Partition**: one enrolled directory (F1). Its **root** is the directory three levels up.
- **Record**: `local_<id>.json`, id grammar `[A-Za-z0-9_-]+`. **Tombstone**: `deleted_<id>`.
- **Normalised hash**: SHA-256 of the record parsed as JSON, minus the volatile top-level keys `lastFocusedAt`, `errorAt` and `remoteMcpServersConfig` (F6), serialised with sorted keys as ASCII. Used only to decide whether two copies hold the same state. A record that is not a JSON object, whose `sessionId` is not `local_<id>`, or that the fingerprint cannot process for any reason, is **unreadable**.
- **Activity**: the record's `lastActivityAt`, read as 0 when missing or malformed. A copy whose activity is later than the clock, read after the file was read, is **future-dated**: the time cannot be ordered against a delete or another copy, and read as "now" it would outrank every tombstone. The clock is read after the file because the app stamps a session in use with the current time every few seconds, and a save that lands during a scan would otherwise look like the future. The scan cache keeps the time as written, so the copy becomes usable when the clock catches up.
- A copy is **usable** when it is readable and not future-dated.
- **State** (`state.json`). Per session id: `agreed`, the normalised hash at which every enrolled partition last held the same state. Per partition: `seen`, every id the tool has observed there at any scan or created there, forgotten only when no partition holds the record any more; `placed`, the hash the tool last put there for an id, remembered while that copy is untouched and not yet agreed. The file names the normalisation its hashes were made with. When a release changes the volatile keys, `agreed`, `placed` and the scan cache are dropped on load and `seen` stays: agreement re-forms from content on the next run, and a pair that differs at that moment is decided by activity or reported as tied. Per app data root: the login last seen there and when the tool first saw it (R9). What was ever in a partition is recorded in `seen` and nowhere else, so nothing can disagree with it.
- A copy is **still as synced** when its hash equals `agreed` or the partition's `placed` hash.

## Rules

**R1 Enrolment.** Only partitions listed in `config.json` are read or written. A path qualifies if it is a real directory shaped `<root>/claude-code-sessions/<uuid>/<uuid>` with no symlinked component below the root. Fewer than two enrolled partitions, a missing one, or one directory enrolled under two spellings aborts the run. A directory with records that is not enrolled is reported and never touched.

**R2 Copies are verbatim.** Every record the tool places is a byte-for-byte copy of a file the app wrote, with the source's mtime, and is verified to be the version the planner chose. The tool never edits or merges record contents.

**R3 One side changed.** When some copies are still as synced and every other copy holds one and the same new state, that state wins, provided its activity is not lower than that of any copy it would replace. A copy can also leave the synced state by going back in time (a restored backup, a promoted temp file, a login flushing stale memory). If its activity is lower, the case is decided by R4. If the change it undoes moved no activity, it cannot be told from a real edit and wins; R8 then keeps what it replaced.

**R4 Both changed.** Otherwise the copy with the greatest activity wins. If the top copies are tied on activity and differ in state, the session is left alone and reported. `--prefer PARTITION`, optionally with `--session ID`, settles ties in favour of one partition.

**R5 New records.** An id missing from a partition is created there unless R7 forbids it. Creation never overwrites: if a file has appeared at the target, the action is refused. Creation is allowed in a live partition, because the app cannot hold what it has never loaded.

**R6 Deletes.** For an id with a tombstone anywhere and a record anywhere, let T be the newest tombstone time. The scanner never reports a time in the future: it falls back to the tombstone file's own time, clamped to now. If some copy's activity is greater than T the session was used, or re-adopted (F10), after the delete, and the record wins. Otherwise the delete wins. Nothing is remembered about finished deletes, so nothing can be remembered wrongly.

Delete wins: every copy, with its `.tmp` sibling first (F7), is retired, and the tombstone is created wherever it is missing and no record remains. An orphaned `.tmp` beside a tombstone is retired. Record wins: the record propagates by R3 to R5 and the stale tombstones are retired after it is in place. While the surviving copies are tied no version is chosen, and the tombstones stay. A tombstone with no record anywhere is created wherever it is missing.

**R7 Lost records.** An id absent from a partition that is in that partition's `seen`, with no tombstone anywhere, was removed there without a tombstone (F3 mid-delete, or a failed tombstone write). It is never recreated there. It is reported, and `--recreate ID` lifts the hold for one session. An id gone from every partition is forgotten.

Presence is written down as it arises. What a run saw at its first scan is saved before its first write. Each create is saved the moment it completes. If that save fails the run stops and the record stays: removing it again would be a delete with no guard and no kept copy (R8, R9), and by then the app may have written over it. So a record removed in the middle of a run, or after a run that did not finish, is still known to have been there.

**R8 Nothing unique is destroyed.** A replaced copy is kept in `kept/<run>/<account>_<org>/` unless it is still as synced and the winner's activity is strictly greater. A retired record, temp file or tombstone is always kept. Kept files never overwrite each other, and the report names where each went, a temp file retired with its record included. Kept runs are pruned after a run with no failures: older than 30 days, and oldest first above 500 MB. The run that just finished counts toward that size and is never pruned.

**R9 Write guards.** Changing or removing an existing file requires that the partition is not live and that the file's stat equals the stat seen at scan. Both are checked before anything is written, and again after the new bytes are staged, immediately before the rename or unlink. Retiring a record also requires, at that last moment, that no `.tmp` sibling has appeared (F7). While the app is running a partition is live if any of these holds: its account equals `lastKnownAccountUuid`; the login cannot be read; `config.json.journal` exists (a config write is in flight, F9); or the tool first saw the current login less than 120 seconds ago, or has not recorded it yet (the first run, or a change in mid-run). The tool dates a login itself, in its state, because the app's config file cannot (F9). `pgrep` exit 0 means running, 1 means not running, anything else means running.

**R10 Crash safety.** A create and the saving of it (R7) happen with stop signals held back, so the two cannot be split. Results are saved after the writes. Every file is staged under a name that starts with `.sync-` and renamed or linked into place, and a staged file is discarded on any exit between the two. Stale `.sync-` files are swept from the partitions and from the tool's own folders. SIGTERM unwinds through cleanup. Commands that edit the sync history or the enrolment take the run lock. A run reads the enrolment under that lock, so once `--unenroll` returns no run is still working from the old set. A state file that cannot be trusted aborts the run; `--reset-state` keeps the old file and starts again from first contact.

**R11 Copies that cannot be judged.** An id with an unreadable or future-dated copy anywhere is left alone everywhere and reported. A file that is there but cannot be read is unreadable, never absent, and the verdict is not cached, so a permission repair is picked up. One odd record never stops a run. A partition that cannot be listed, or an entry in it that cannot be inspected, says nothing about what the partition holds, so the run aborts before any change. Only an entry that is gone counts as absent.

**R12 Unattended runs.** A quiet run appends to its own log file: writes, kept copies, and standing problems or a standing abort once per change. The log is capped. The file launchd holds open receives only unexpected tracebacks. The agent watches the enrolled partitions, is rewritten when enrolment changes, and is removed when fewer than two remain. A standing refusal is caught by the first guard and writes nothing, so a watched directory does not refire on it. `--status` tells a loaded agent from a plist that merely exists, and warns when an installed agent has never had a clean run or has had none for an hour.

## Non-goals

Cowork sessions, `scheduled-tasks.json`, `backlog/`, `archived-sessions.idx` (a load-order hint the app rebuilds) and transcripts are never synced.

## Known residual risks

A scenario belongs here, and is not coded around, when it needs two independent faults at the same moment. It gets code when it reproduces in the normal workflow with no fault injected, or when a single fault loses data. A guard for a rarer case brings a failure mode of its own.

- **Native multi-account mode (F12).** If the app parks sessions, the new login loads its own synced copy of a parked id, and the app then routes saves for that id between the two partitions in ways this tool cannot see. The 120-second grace covers a login change. It does not cover a session parked for hours. This tool is for logout-and-login switching. If the app gains a native account switcher for you, stop using it.
- **A login change between two manual runs.** The tool dates a login when it first sees it. Two changes between runs that end on the login it last saw go unnoticed, and the partition just left gets no grace. With the agent installed a run follows every change within seconds. Without it, do not run the tool in the first seconds after logging in.
- **The first run defers.** The first run ever dates the login as just changed, so replacing or retiring in the other partition waits two minutes. Creating records does not wait.
- **A time stamped while the clock ran ahead.** Such a record is left alone while its time is in the future. Once the clock catches up the time is taken at face value, because nothing then shows it to be wrong. Using the session, or deleting it under that login as well, settles it sooner.
- **A create that is not written down.** Stop signals cannot land between a create and the save that records it, but a kill that cannot be caught, a power loss, or a save that fails can. The record stays, and the next run's first scan records it if it is still there. It goes wrong only if the record was also removed without a delete marker before that run.
- **A retire that stops halfway.** A record with a leftover temp file is retired temp file first, because a temp file left alone would be promoted by the app. The record's guard is checked before the temp file is touched, so a record that changed since the scan leaves both alone. If the record changes in the few milliseconds the temp file takes to retire, the temp file is already in `kept/` and the report does not name it. That takes an app save cut short and a second writer in that window. The next run decides again from what it finds.
- **The last instant.** Between the final guard and the rename the app could write the same file. The window is the rename call itself. The app's next save restores its memory state.
- **First contact.** With no sync history (the first run, or after `--reset-state`), a record the app removed seconds ago, before writing its tombstone, can be put back once. The next run sees the tombstone and retires it again, because its activity predates the delete.
- **Imported sessions.** Deleting a synced session that was imported leaves its transcript on disk, because the other login's copy still claims it (app behaviour).
- **Remote Control.** Archiving or deleting, under one login, a session whose Remote Control link belongs to the other login sends that link's id with the wrong token (F13). The app ignores the failure.
- **Two app instances at once** can both change one session. R4 resolves it and keeps the loser.
- **Changes without activity** (a rename, an archive, PR status the app refreshes) keep the replaced copy every time they propagate. The size cap bounds the cost; watch `kept/` in the first days.
