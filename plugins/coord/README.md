# coord

Coordination for several Claude Code sessions working in one repository at once.

Two sessions in one checkout will edit the same file without knowing it. Two sessions in two
worktrees will not collide while they work, but they will at merge time. A session that has
finished but not committed (or committed but not pushed) leaves work that exists nowhere else.
`coord` makes each of these visible, and makes the first one impossible by default.

| Piece | What it does |
|---|---|
| **Edit-time locks** | The first session to edit a file in a checkout claims it. Another live session editing it is **blocked** and told how to ask for it. Locks are per worktree, so sessions in different worktrees never block each other. |
| **Board** | Who is working here, on what, holding which files, plus uncommitted/unpushed work. Shown at session start and on demand. |
| **Mailbox** | Addressed, repliable messages between sessions (`REQUEST` → `GRANT`/`DENY`/`DEFER`), durable across session restarts. Open requests addressed to you are surfaced on every prompt. |
| **Merge foresight** | Which other worktree branches changed the same files as yours, before anyone merges. |
| **Closeout ledger** | A running list of in-flight work; a gentle end-of-turn reminder when finished work sits uncommitted or unpushed. |
| **Liveness** | Every "may I take this?" decision is gated on whether the owner is alive, with three answers — alive, dead, unknown — and *unknown counts as alive*. A lock whose owning process is running is never treated as stale, however long it has been idle. |

## Install

```text
/plugin marketplace add sconedrag/pitwright
/plugin install coord@pitwright
```

Requires `git` and Python 3.9+ (`python3` on `PATH`, standard library only — the stock macOS `python3` works). Tested on macOS; written for Linux too, but not yet tested there.

Add the plugin's state directory to your repository's `.gitignore`:

```text
.claude/coordination/
```

## What runs automatically

| Hook | Script | Behaviour |
|---|---|---|
| `SessionStart` | `session_start.sh` | Registers the session (which turns locking on for it), sweeps orphaned locks, and adds the board, open peer requests and a worktree advisory to the session's context. |
| `PreToolUse` `Edit\|Write\|MultiEdit\|NotebookEdit` | `lock_guard.py` | Claims the file for this session, or blocks the edit if another live session in this checkout holds it. |
| `UserPromptSubmit` | `inbox_prompt_check.sh` | Surfaces peer requests awaiting your reply (deduplicated; quiet otherwise). |
| `Stop` | `closeout_stop_check.sh` | Reminds you about finished work left uncommitted or unpushed (advisory, deduplicated). |
| `SessionEnd` | `session_end_cleanup.sh` | Releases this session's locks and archives its manifest. |

Every hook fails open: a missing `python3`, a non-git directory or an internal error never
blocks a session, a prompt or an edit (the lock guard prints a one-line notice when it allows
an edit because of an error).

## Commands

| Command | Use it to |
|---|---|
| `/coord:board` | See everything at a glance. |
| `/coord:start-session <domain> [--name N] [--auto-claim] [activity…]` | Say what you are working on, so peers can see it. Optional: registration already happened at session start. |
| `/coord:claim-files <paths>` / `/coord:release-files [--all]` | Claim files before you start, or hand them back. |
| `/coord:ask-lock <path> "<why>"` | Ask the holder of a lock to release it. |
| `/coord:blocked [<path>]` | Find out who is blocking you, whether they are alive, and whether you may ask or adopt. |
| `/coord:msg <peer> "…"` / `/coord:inbox` / `/coord:broadcast "…"` | Talk to one session, read and reply, or tell everyone. |
| `/coord:sessions` | List and name sessions across all worktrees. |
| `/coord:adjacency` / `/coord:worktree-overlap` | See which branches will collide at merge. |
| `/coord:closeout` | Review in-flight work: uncommitted, unpushed, unmerged branches. |
| `/coord:session-status` / `/coord:reap-session` / `/coord:complete-session` | Inspect raw state, clean up after dead sessions, or finish without ending Claude Code. |

## Configuration

Optional `.claude/coord.json` in your repository; environment variables override it.

| Key | Default | Env override | Meaning |
|---|---|---|---|
| `locks_advisory` | `false` | `COORD_LOCKS_ADVISORY` | Warn instead of blocking on a held file. |
| `stale_seconds` | `86400` | `COORD_STALE_SECONDS` | How long a session whose process is gone keeps its locks. |
| `idle_badge_seconds` | `3600` | `COORD_IDLE_BADGE_SECONDS` | When the board marks a session idle. |
| `registry_cap` | `200` | `SESSION_REGISTRY_CAP` | Sessions kept in the cross-worktree registry. |
| `closeout_stale_branch_days` | `7` | `CLOSEOUT_STALE_BRANCH_DAYS` | When an unpushed local branch is reported. |
| `additive_files` | `[]` | — | Filename suffixes that change on nearly every branch and merge cleanly (e.g. `["project.pbxproj"]`); reported separately from real collisions. |
| `churn_globs` | `[]` | — | Generated paths to ignore in merge foresight. |
| `closeout_buckets` | `[]` | — | `[{"name", "prefix", "suffix"}]` groups for uncommitted files; a bucket named `migration` is called out when unpushed. |

## Where state lives

- `.claude/coordination/` in each worktree: session manifests, locks, the closeout ledger.
- `<git-common-dir>/agent-coordination/`, shared by every worktree of the repository: the
  session registry and the mailbox.

Hooks and commands write nothing outside the repository except Python bytecode caches. They
read Claude Code's own session transcripts under `~/.claude/projects/` (to list sessions) and
plan files under `.claude/plans/` in the repository and in your home directory (to label
closeout items).

Also included, but not wired to any hook or command: `scripts/memory_index.py`, a
lock-protected writer for a Claude Code memory index (`MEMORY.md` plus one file per memory)
under `~/.claude/projects/<project>/memory/`, safe for many sessions writing at once. Run it
yourself (`python3 scripts/memory_index.py --help`); nothing invokes it for you, because
regenerating an index you maintain by hand would replace it.

## Session identity

A session is identified by `CLAUDE_CODE_SESSION_ID`, which Claude Code sets for hooks and for
the commands it runs, so coordination works in any terminal or IDE. A subagent shares its
parent's id, and therefore its locks. If that variable is absent, coord falls back to
`TERM_SESSION_ID` (set by macOS Terminal and iTerm2), then to
`/tmp/.claude-session-<ppid>.id`.

`CLAUDE_CODE_SESSION_ID` is observed Claude Code behaviour rather than a documented
interface; if it ever disappears, coord degrades to the fallbacks above.

Upgrading from 0.1 changes the key on macOS Terminal and iTerm2, and 0.1 state is not
migrated — see [CHANGELOG.md](CHANGELOG.md) for what to do before and after upgrading.

## Known limitations

- Locks govern edits made through Claude Code's edit tools. A shell command that writes a file
  is not intercepted.
- Role-addressed messages (`topic:<role>`) need a role module that is not shipped.

## Tests

```text
bash scripts/tests/run_all.sh
```

Each test builds its own scratch git repository; nothing touches the repository you run it from.
