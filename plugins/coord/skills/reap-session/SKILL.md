---
name: reap-session
description: Release orphaned coordination state - reap DEAD sessions (owning process gone) and delete orphan locks (locks whose owning session no longer exists). Use when /coord:board or /coord:session-status shows stale sessions, or when phantom file locks block edits.
---

Reap stale sessions and orphan locks. Argument: $ARGUMENTS

This wraps `reap_coordination.py`. Sessions often end without an explicit completion step,
so stale manifests and orphaned lock files accumulate and create phantom contention. This
skill clears them.

### Usage

- `/coord:reap-session` or `/coord:reap-session --all-stale` — reap every session that is
  NOT live AND sweep all orphan locks (the common cleanup — both do the same thing).
  **Liveness-gated:** a session whose owning process is still alive is never reaped, however
  long it has been idle — so a developer who walks away is safe. Only sessions with a
  dead/absent process whose heartbeat is older than the fallback threshold (default 24
  hours, overridable) are reaped.
- `/coord:reap-session <session-id>` — reap exactly one named session (operator override;
  bypasses the liveness/age checks — use when you KNOW it's dead).
- `/coord:reap-session --orphan-locks` — delete orphan locks only; leave sessions alone.
- Add `--dry-run` to any of the above to preview without mutating.

### Steps

1. Run a dry-run first so the user sees the blast radius:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/reap_coordination.py" <args> --dry-run`
2. Show the dry-run output. If it reaps anything non-trivial (>0 sessions or a large orphan
   count), confirm with the user before the real run.
3. Run for real: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/reap_coordination.py" <args>`
4. Report the count of sessions reaped, locks released, and orphan locks deleted.

### What it does NOT do

- It does not touch LIVE sessions (owning process still running) unless you name one
  explicitly — idle time alone never makes a live session reapable.
- A single named-session reap does NOT also sweep orphan locks (scoped operation).
- Reaped manifests are archived with `status: "reaped"` — nothing is lost, only released.

### Anti-instructions

- Do NOT delete the locks directory wholesale by hand — that would release ACTIVE sessions'
  locks too. Always go through this script, which checks ownership.
