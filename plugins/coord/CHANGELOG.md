# Changelog

## 0.2.0

**Session identity now comes from Claude Code, so coord works outside macOS Terminal and iTerm2.**

In 0.1 a session was identified by `TERM_SESSION_ID`. Only macOS Terminal and iTerm2 set it, so on Linux, in VS Code and in most Windows/WSL terminals a session resolved to no id. It was then not registered, took no locks, was not blocked by anyone else's, and did not appear on the board.

coord now uses `CLAUDE_CODE_SESSION_ID`, which Claude Code sets for hooks and for the commands it runs. If that is absent it falls back to `TERM_SESSION_ID`, and only when neither is set to `/tmp/.claude-session-<ppid>.id`. A session has exactly one id, resolved in one module, `scripts/_identity.py`, which replaces six copies of the old resolution order.

### Upgrading from 0.1

On macOS Terminal and iTerm2 the key changes from `TERM_SESSION_ID` to the Claude Code id. **0.1 state is not migrated.** Every pane of a tmux server inherits one `TERM_SESSION_ID` (from the terminal tab that started the server), so 0.1 kept one manifest for all of them, recording one process. Nothing in it can say which pane a given lock belongs to. Moving it to a new id could therefore take a live pane's locks.

Instead, 0.1 manifests and locks stay under their old key, and 0.2 sessions treat them as an ordinary peer's. Once the 0.2 code is running, nothing releases them on that session's behalf: SessionEnd and `/coord:complete-session` act on the new id. A 0.1 lock whose process is gone becomes claimable by anyone after `stale_seconds` (default 24 h, `COORD_STALE_SECONDS`). Its manifest stays until `python3 "<plugin>/scripts/reap_coordination.py" --all-stale` removes it.

**Before upgrading, run `/coord:complete-session` (or `/coord:release-files`) in each running session.** That is the clean path.

To free locks your own 0.1 session left behind, first check who holds what:

```bash
python3 "<plugin>/scripts/coord_locks.py" list
```

Then, from the same terminal (inside the same tmux server if you use one), confirm that `echo "$TERM_SESSION_ID"` matches the holder, with `:` and other characters outside `A-Za-z0-9_-` removed, and run:

```bash
CLAUDE_CODE_SESSION_ID= python3 "<plugin>/scripts/coord_locks.py" release --all
```

Blanking `CLAUDE_CODE_SESSION_ID` makes the command act as the 0.1 key. It refuses while the process recorded under that key is still running, which covers another tmux pane and a session not yet restarted. Run from a different terminal, it resolves a different key and reports `(no locks held)`.

**Restart sessions after upgrading.** A session started under 0.1 should be restarted (or resumed) so SessionStart registers it under its new id. Until then it has no manifest under that id:
- it takes no locks, and each edit prints a notice saying so;
- it is still blocked by other sessions' live locks.

### Other changes

- **SessionStart** keys the manifest with the same resolver the commands use. If the hook payload's `session_id` ever differs from it, an `IDENTITY_MISMATCH` event is logged to the coordination channel's `events.log`.
- **Mailbox:** the per-checkout `current-session.json` marker is trusted only when its pid matches the session's own manifest. 0.1 trusted it unchecked, so a session could read mail meant for another session in the same checkout.
- **`/coord:start-session --name` and `/coord:sessions name`** now name the session by `CLAUDE_CODE_SESSION_ID` (the registry's key), reading that marker only when the variable is unset.
- **SessionEnd** releases exactly this session's id, never another key it might share with a peer.

## 0.1.0

Initial release.
