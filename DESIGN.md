# claude-desktop-session-sync: design

The contract this tool is built and reviewed against. Rules are numbered so tests and review findings can cite them.

## Purpose

The Claude desktop app keeps its Code sidebar as one index per login. One person using two subscription accounts sees a different sidebar under each. This tool keeps the sidebars identical by copying index records between the per-login directories. Transcripts are shared already and are never touched.

## Facts about the app that the design relies on

Verified against desktop app 2.2553.1 (main-process JavaScript) on 2026-09-21. Re-verify after a major app update.

| # | Fact | Where verified |
|---|------|----------------|
| F1 | A sidebar index lives at `<userData>/claude-code-sessions/<accountUuid>/<orgUuid>/`. The path is built from the login; symlinked components are refused (`O_NOFOLLOW`, `lstat` walk). | `getStorageDir`, `Ci`, `xi` |
| F2 | A record is `local_<id>.json`. It names no account. It points at a transcript through `cliSessionId`. | record key census over 794 files; loader `uS` has no account check |
| F3 | A delete removes `local_<id>.json.tmp` and `local_<id>.json` first, then writes `deleted_<id>` files holding the delete time in epoch ms. Work happens between the two steps, longer for imported sessions. | session delete routine, `ax` |
| F4 | Tombstone ids are filtered by sessions loaded in memory for the current login only, so a copy under another login does not suppress a tombstone. | `cliSessionIdsClaimedByLoadedSessions({localOnly:true})` |
| F5 | The app reads a partition when a login initialises, has no directory watcher, and rewrites a record from memory on every save. Memory wins over disk for a loaded session. | `loadSessionRecords`, `writeSessionToDisk` |
| F6 | Making a session visible saves its record with a new `lastFocusedAt`. File mtime therefore says nothing about which copy has newer state. | `setSessionVisibility` |
| F7 | Saves are write-temp-then-rename, with a direct-write fallback on some errors. At startup the app promotes an orphaned `local_*.json.tmp` to a live record. | `fS`, persistence writer |
| F8 | The app's own picker for CLI transcripts excludes, and refuses to adopt, any session id recorded under any login on the install. Copying records is the only way to share them. | `list`, `resolveForResume`, `lx` |
| F9 | `lastKnownAccountUuid` in `<userData>/config.json` is written synchronously before the session manager re-initialises, and is not cleared at logout. | `Io.set(Qbe, …)` |
| F10 | A re-import of a CLI transcript creates `local_<cliId>.json` and removes `deleted_<cliId>` under the current login only. Its `createdAt` and `lastActivityAt` come from the transcript and can predate an old tombstone. | `adoptCliSession`, `cx` |
| F11 | The app ignores files in a partition that do not start with `local_` or `deleted_`. | every reader filters on the prefix |
| F12 | With multi-account parking, a session still running under the previous login stays in memory and keeps saving into its own partition. The current login does not load a second copy of an id it already holds. | `parked`, `storageDirFor`, loader duplicate check |
| F13 | Records hold `bridgeSessionIds` and `remoteMcpServersConfig`. Bridge ids are sent to the Remote Control API with the current login's token when such a session is archived or deleted, not at load. | `cleanupRemoteBridgeSessions` callers |

## Model

- **Partition**: one enrolled directory (F1). Its **root** is the directory three levels up.
- **Record**: `local_<id>.json`, id grammar `[A-Za-z0-9_-]+`. **Tombstone**: `deleted_<id>`.
- **Normalised hash**: SHA-256 of the record parsed as JSON, minus volatile keys (`lastFocusedAt`, `processGoneReason`), serialised with sorted keys. Used only to decide whether two copies hold the same state. A record that is not a JSON object, or whose `sessionId` is not `local_<id>`, is **unreadable**.
- **State** (`state.json`), per session id: `agreed`, the normalised hash at which every enrolled partition last held the same state; `deleted`, true once a delete reached every partition. Per partition: `seen`, ids observed there; `placing`, ids the tool was about to create there.

## Rules

**R1 Enrolment.** Only partitions listed in `config.json` are read or written. A path qualifies if it is a real directory shaped `<root>/claude-code-sessions/<uuid>/<uuid>` with no symlinked component below the root. Fewer than two enrolled partitions, or an enrolled partition that is missing, aborts the run. A directory with records that is not enrolled is reported and never touched.

**R2 Copies are verbatim.** Every record the tool places is a byte-for-byte copy of a file the app wrote, with the source's mtime. The tool never edits or merges record contents.

**R3 One side changed.** For an id with differing normalised hashes and a known `agreed`: if every partition that differs from `agreed` holds the same hash, that version replaces the copies still at `agreed`.

**R4 Both changed.** Otherwise (no `agreed`, or two different new versions) the copy with the greatest `lastActivityAt` wins and every replaced copy is kept (R8). If the top two are tied the id is left alone and reported. `--prefer PARTITION` resolves ties in favour of one partition.

**R5 New records.** An id missing from a partition is created there unless R7 forbids it. Creation is allowed in a live partition, because the app cannot hold what it has never loaded.

**R6 Deletes.** For an id with a tombstone anywhere and a record anywhere, let T be the newest tombstone time. The scanner never reports a time in the future: it falls back to the tombstone file's own time, clamped to now.
- If `deleted` is true the record is a re-creation (F10): the record wins.
- Else if some copy has `lastActivityAt` greater than T the session was used after the delete: the record wins.
- Else the delete wins.

Delete wins: every copy and its `.tmp` sibling are retired (R8), and the tombstone is created wherever it is missing and no record remains. Record wins: the record propagates by R3 to R5 and the stale tombstones are retired. A tombstone with no record anywhere is created wherever it is missing.

**R7 Lost records.** An id absent from a partition that is in that partition's `seen` or `placing`, with no tombstone anywhere, was removed there without a tombstone (F3 mid-delete, or a failed tombstone write). It is never recreated there. It is reported. `placing` entries last one run.

**R8 Nothing unique is destroyed.** Before a file is replaced or removed, its bytes are copied to `kept/<run>/<account>_<org>/` when they differ from `agreed`, and always when retiring a record or a tombstone. Entries older than 30 days are pruned after a run with no failures.

**R9 Write guards.** Changing or removing an existing file requires all of: the partition is not live (the app is running and the partition's account equals `lastKnownAccountUuid`, or that cannot be read), re-checked immediately before the change; and the file's stat equals the stat seen at scan. `pgrep` exit 0 means running, 1 means not running, anything else means running.

**R10 Crash safety.** State is loaded, then intents (`placing`) are saved before any write, then results after. Every file is written to a temporary name that starts with `.sync-` and renamed into place. Stale `.sync-` files are swept at start. SIGTERM unwinds through cleanup. A corrupt state file aborts the run; `--reset-state` starts again from first contact.

**R11 Unreadable copies.** An id with an unreadable copy anywhere is left alone everywhere and reported.

**R12 Unattended runs.** Quiet mode logs what was written, and standing problems once per change of the problem set. The log is capped. The agent watches the enrolled partitions and is rewritten when enrolment changes. `--status` shows the last successful run.

## Why a parked session is safe (F12)

A parked session saves into its own partition while another login is current. The tool would replace that file only if the other partition's copy of the same id changed. It cannot: the current login holds the parked session in memory and never loads its own copy. A delete of a parked session writes the tombstone into the parked partition; the other copy is in the live partition and waits for R9.

## Non-goals

Cowork sessions, `scheduled-tasks.json`, `backlog/`, `archived-sessions.idx` (a load-order hint the app rebuilds) and transcripts are never synced.

## Known residual risks

- Between the final stat check and the rename there is a window of microseconds in which the app could write the same file. The app's next save restores its memory state.
- Deleting a synced session leaves its transcript on disk when the session was imported, because the other login's copy still claims it (app behaviour, F8 claim set).
- Archiving or deleting, under one login, a session whose Remote Control link belongs to the other login sends that link's id with the wrong token (F13). The app ignores the failure.
- Two app instances running at once can both change one session. R4 resolves it and keeps the loser.
