---
name: start-session
description: Register this session for multi-session coordination. Declares a domain (a free-form label shown on the board), an optional human name, and an activity line, and optionally locks your currently in-flight files. Optional --auto-claim scans the working tree and claims modified files.
---

Register for coordinated multi-session work. Arguments: $ARGUMENTS

### What's already true before you run this

A session is **auto-registered the moment it starts** — identity and file-locking are on by
default. This skill is an *upgrade* on top of that: it declares a domain (a short label for
what you're working on, shown on `/coord:board`), an optional human-readable name, an
activity line, and — with `--auto-claim` — locks files you're already mid-edit on. Running
it is not what turns coordination on; it's what makes your presence legible to peers.

### Argument parsing

Split `$ARGUMENTS` into:
- **First token**: `<domain>` — a free-form label (e.g. `frontend`, `docs`,
  `migration-2024-10`). There is no fixed registry of domains to validate against; it is
  purely descriptive bookkeeping, not an access-control boundary — actual file protection
  comes from locks (`/coord:claim-files`), which work regardless of domain.
- **`--auto-claim`** (anywhere in arguments): lock every file this session has already
  modified or staged.
- **`--name "<human name>"`**: a short name for this session, recorded in the cross-worktree
  session registry (visible via `/coord:sessions`).
- **Remaining tokens**: a free-text description of what you're doing — becomes this
  session's activity line on `/coord:board`.

Example invocations:
- `/coord:start-session frontend` — declare the domain, no file-level locks
- `/coord:start-session frontend --auto-claim fixing the nav bar` — declare + lock
  currently-modified files + set activity to "fixing the nav bar"
- `/coord:start-session docs --name "Docs pass"` — declare, and name this session for the
  cross-worktree registry

### Steps

1. Declare the domain, plus the name and activity line if given. This updates the
   manifest the SessionStart hook created; it never creates one:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session_manifest.py" declare "<domain>" \
     --name "<name>" --activity "<description>"
   ```
   Omit `--name` / `--activity` when not given; omitted fields keep their current values.
   If it reports "no manifest for this session", the plugin's SessionStart hook did not run
   for this session (hooks disabled, or the session predates installing the plugin) —
   tell the user to restart the session; do not create the manifest by hand.

2. **If `--auto-claim` is present**, lock every modified or staged file through the
   canonical lock implementation — never hand-roll the lock key or format. Feed the paths
   through `xargs`, not an unquoted shell variable (zsh does not word-split one, so several
   paths would arrive as a single argument):
   ```bash
   { git status --porcelain | awk '{print $NF}'; git diff --cached --name-only; } | sort -u \
     | xargs -r python3 "${CLAUDE_PLUGIN_ROOT}/scripts/coord_locks.py" claim --domain "<domain>"
   ```
   Report N files claimed, M skipped — naming the holding session for each skip. To claim
   only a subset, use `/coord:claim-files <paths>` instead.

3. **If `--name` was given**, also record it in the cross-worktree registry, which is what
   `/coord:sessions` lists:
   ```bash
   SID=$(python3 -c "import json; print(json.load(open('.claude/coordination/current-session.json'))['sessionId'])" 2>/dev/null)
   [ -n "$SID" ] && python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session_registry.py" name "<name>" --session-id "$SID"
   ```

4. Report: domain declared, auto-claimed file count (if any), the name (if any), and next
   steps — `/coord:claim-files <paths>` for more locks, `/coord:complete-session` when done.
   `/coord:board` shows whether another active session already declared the same domain
   (advisory only, never blocking).

### Auto-claim scope and safety

Auto-claim picks up every file with a working-tree or index modification, with no further
filtering — opening and abandoning a file without modifying it does NOT lock it. Auto-claim
is additive and idempotent — running it twice, or after making more changes, only adds new
locks.
