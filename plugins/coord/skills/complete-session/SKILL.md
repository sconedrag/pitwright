---
name: complete-session
description: Mark this coordination session complete now, release all its locks, and archive its manifest - without ending the Claude Code session itself.
---

Complete this coordination session and release all its resources — calls the single reaper
implementation also used automatically at clean session end (releases every lock, archives
the manifest with status `completed`, logs the event); nothing here hand-edits files.

Check `/coord:inbox --open` first — this does **not** answer peers waiting on you.

### Steps

1. Resolve your own session id:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agent_message.py" whoami --json` (the `sessionId` field)
2. Complete it:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/reap_coordination.py" "<sessionId>" --complete`
3. Show the output verbatim. "skipped (no manifest)" means there was nothing to complete.

### Related

`/coord:reap-session` cleans up OTHER (stale/dead) sessions, not your own.
`/coord:release-files` frees specific files without ending the session at all.
