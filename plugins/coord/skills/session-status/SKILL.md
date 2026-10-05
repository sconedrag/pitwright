---
name: session-status
description: View all active coordination sessions in this worktree, their domains, locked files, and stale detection - the raw listing, read-only. Use /coord:board instead for the narrated, higher-level view.
---

Display multi-session coordination state for this worktree. Read-only; mutates nothing.

1. `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/render_board.py" --json` — per session: id,
   domain, human name, lock count, heartbeat age, alive/idle/stale.
2. `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/coord_locks.py" list` — one line per lock, with
   owning session and domain.
3. For other worktrees of this repo too: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session_registry.py" list --all`

Report per session (domain, status, heartbeat age, locked files), total + orphaned lock
counts, and declared domains. If nothing is registered: "No active coordination sessions."

`/coord:board` is the curated, narrated view; this is the lower-level raw listing.
