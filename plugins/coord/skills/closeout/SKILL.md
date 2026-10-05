---
name: closeout
description: Track and close out in-flight implementation work - the "running list". Surfaces uncommitted changes, committed-but-unpushed commits, and unpushed database migrations, and maintains a per-branch ledger of work items (open -> committed -> pushed -> done | dropped). Use to review what hasn't been closed out, mark work done, or intentionally drop an abandoned branch of work.
---

Track / review / close out in-flight work. Arguments: $ARGUMENTS

Catches the "abandoned just before commit/push" failure mode: a *completed* unit of work
left uncommitted, committed-but-unpushed, or on an orphaned branch while attention moved on
to the next piece of work. Unpushed database migrations are the same class of silent loss
and are flagged distinctly.

The brain is `closeout_ledger.py`. The ledger lives at
`.claude/coordination/closeout-ledger.json` (gitignored, per-worktree).

### Usage

- `/coord:closeout` or `/coord:closeout check` — reconciliation report: uncommitted (bucketed
  migration/source/docs/other), committed-but-unpushed commits, unpushed migrations, and
  which open ledger items have no live changes ("finished or forgotten?"). This is the
  **periodic review** — run it when wrapping a unit of work or before ending a session.
- `/coord:closeout list` — the ledger ("running list") + live drift summary.
- `/coord:closeout branches` — repo-wide sweep of LOCAL branches carrying commits that exist
  on no remote — abandoned feature branches whose work was never pushed. Read-only: it
  flags (idle age, unpushed count, migration flag), never deletes. Remediate by pushing the
  branch (and opening a pull request) or deleting it if truly abandoned. Pushed-but-unmerged
  branches and the main branch are not flagged. If a squash-merge workflow deletes a branch's
  remote ref on merge, a landed branch can otherwise look unpushed forever — this command
  reconciles against merged pull requests where that information is available, splitting
  genuinely **at-risk** work (exists nowhere else) from already-**landed** work (safe to
  delete locally).
- `/coord:closeout start "<title>" [--plan <file>] [--paths <globs>]` — add an open work
  item (auto-captures the current branch). `--paths` ties files to the item so `check` can
  tell whether it still has live changes.
- `/coord:closeout done <id>` — mark an item closed out (merged / pushed / no longer in flight).
- `/coord:closeout drop <id> "<reason>"` — intentionally abandon an item. Recording the
  reason makes abandonment a *logged decision*, not silent loss.

### Steps

Run the brain directly and relay its output. Examples:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/closeout_ledger.py" check
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/closeout_ledger.py" list
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/closeout_ledger.py" branches
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/closeout_ledger.py" start "Workout-log persistence" --paths "src/workout_log*.py"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/closeout_ledger.py" done co-3
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/closeout_ledger.py" drop co-4 "superseded by co-7"
```

Pass `$ARGUMENTS` through to the script's subcommand. After a `check` that surfaces
outstanding work, help the user close it out: commit what's loose, push the branch and open
a pull request, or recover orphaned uncommitted work with a WIP commit.

### Anti-instructions

- Do NOT mark an item `done` to silence the reminder when the work is still
  uncommitted/unpushed — `done` means actually closed out. To deliberately abandon, use
  `drop` with a reason.
- Do NOT edit the closeout ledger file by hand — go through the script so timestamps and
  bookkeeping stay consistent.
- The ledger is gitignored and per-worktree; it does not sync across machines/worktrees.
