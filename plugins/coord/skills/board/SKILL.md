---
name: board
description: Show the central coordination board - every active session in this worktree with its human name, domain, plan, task IDs, current activity, heartbeat age and lock count, plus (when present) a build-queue, pending-verification backlog, last-known-good marker, closeout hygiene, and cross-worktree interfaces. Use to see who is working on what before starting structural changes, or anytime you need situational awareness across sessions.
---

Render the coordination board. Arguments: $ARGUMENTS

This is the single human-readable view of "who is doing what right now" across all
concurrent sessions in this worktree. It auto-surfaces at session start too.

### Steps

1. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/render_board.py"` (add `--json` if
   `$ARGUMENTS` contains `--json`).
2. Show the output verbatim.
3. If the board reports stale sessions or orphan locks, remind the user they can clear them
   with `/coord:reap-session --all-stale`.

### What it shows

- **Active sessions**: human name, domain, short id, heartbeat age (idle after ~1h quiet,
  reap-eligible only once the owning process is gone AND the heartbeat is a day stale — both
  configurable), lock count, and activity line.
- **Cleanup**: count of stale sessions + orphan locks, with the reap command.
- **Closeout hygiene**: uncommitted/unpushed drift and open ledger items (`/coord:closeout`).
- Opportunistically, if the host project writes them: a build-queue snapshot, a
  pending-verification backlog, a last-known-good marker — simply absent otherwise.

Re-run `/coord:start-session <domain> "<new activity>"` to refresh your activity line
without disturbing your claimed files.

### Pairs with

- `/coord:start-session <domain> [--auto-claim] [--name "..."] [description]` (populates the board)
- `/coord:reap-session --all-stale` (clears what the board flags)
- `/coord:session-status` (lower-level: raw session/lock listing, no narration)
