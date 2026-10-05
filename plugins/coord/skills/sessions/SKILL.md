---
name: sessions
description: List and (re)name Claude Code sessions across all worktrees of this repo. Answers "which session was the one doing X?" and lets you name a session (yours or another) so it's recognizable and easy to identify later. Backed by the cross-worktree session registry.
---

Manage the cross-worktree session registry. Arguments: $ARGUMENTS

This is the machine-wide companion to `/coord:board` (which is per-worktree): it tracks
every session's identity, human name, worktree, branch and liveness in one shared registry
at the repo's common git directory. Sessions are auto-registered at session start; this
skill lists and renames them.

### Steps

Parse `$ARGUMENTS` and dispatch:

1. **List (default — no subcommand):**
   - `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session_registry.py" list` (add `--all` if
     `$ARGUMENTS` contains `--all` to merge in read-only, machine-wide sessions outside this
     registry; add `--json` for machine output).
   - Show the output verbatim.

2. **Name the CURRENT session — `name "<name>"`:**
   - Resolve *self*: read `.claude/coordination/current-session.json` (written at session
     start) → use its `sessionId`. If the marker is missing, fall back to the registry entry
     whose `pid` equals this shell's parent process id; if still ambiguous and exactly one
     active session exists in this worktree, use that. If self cannot be resolved, tell the
     user to pass an explicit id via `rename`.
   - `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session_registry.py" name "<name>" --session-id <resolved-id>`.

3. **Rename ANY session — `rename <id|name> "<new name>"`:**
   - `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session_registry.py" rename <id-or-oldname> "<new name>"`.
   - `<id>` accepts the full id, a short-id prefix, or the exact current human name.

4. After a name/rename, run `list` again so the user sees the updated registry.

### What it shows

Per session: live/idle/stale badge, human name, short id, worktree (or project path for a
machine-wide `--all` entry), branch, and source (`registry` = tracked here; machine-wide =
read-only). Live iff the owner process is alive; idle if live but quiet a while; stale if
dead and past the fallback age.

### Pairs with

- `/coord:board` — per-worktree coordination view (sessions + locks).
- `/coord:start-session` — also upserts your session's domain into this registry.
- `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session_registry.py" gc` — prune dead+aged entries
  (also runs on every write).
