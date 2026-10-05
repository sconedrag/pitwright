---
name: claim-files
description: Explicitly lock specific files for this session, preventing other sessions in the same checkout from editing them until you release them.
---

Lock specific files. Paths: $ARGUMENTS

Locking is implemented once, in `coord_locks.py` — the same implementation the edit-time
guard enforces against, so a claim made here is never invisible to the hook that actually
blocks a conflicting edit.

### Steps

1. Look up your session's declared domain, if any — purely a bookkeeping label shown on
   the board and in lock listings, not an access-control boundary. It is fine to omit this
   and let it default to `unknown`.
   ```bash
   SID=$(python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agent_message.py" whoami --json | python3 -c "import json,sys; print(json.load(sys.stdin)['sessionId'])")
   DOMAIN=$(python3 -c "import json; print(json.load(open('.claude/coordination/sessions/${SID}.json')).get('domain','unknown'))" 2>/dev/null || echo unknown)
   ```

2. Claim the paths in one call — it computes the guard-compatible key, writes the
   `{file, worktree, sessionId, domain, lockedAt}` record atomically, and updates the
   manifest's locked-files list:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/coord_locks.py" claim --domain "$DOMAIN" $ARGUMENTS
   ```
   Each path reports `OK` (claimed / already held by this session) or `FAIL` with the
   reason (`held by <sessionId> (<domain>)`). A lock whose owning session's process is dead
   *and* whose heartbeat is older than the stale-session threshold is reclaimed
   automatically; a lock whose process is still alive is **never** stolen, however long idle.

3. Announce the claim on the shared cross-worktree channel so peers see it:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/_agent_channel.py" --event FILES_CLAIMED "Claimed: $ARGUMENTS"
   ```

4. Report which files locked and which were skipped, naming the holding session for each
   skip. **If a file you need is held by a live peer, do not wait passively** — see
   `/coord:ask-lock` to request it, which opens an addressed, repliable thread with the owner.

### Scope limit (important)

Lock keys are **worktree-scoped**: two sessions in *different* worktrees editing the "same"
path do not block each other, because they are editing different physical copies. Locks
only protect against collisions **inside one checkout**. Across worktrees, the real
collision is at *merge* time — use `/coord:worktree-overlap` and `/coord:adjacency` for that.
