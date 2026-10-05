---
name: release-files
description: Release file locks held by this session. Use --all to release everything this session holds.
---

Release file locks. Paths: $ARGUMENTS

Locking is implemented once, in `coord_locks.py` — see the note in `/coord:claim-files`. Do
not hand-write the lock key or file format; always go through the script.

### Steps

1. Release the named paths (or every lock this session holds with `--all`). The script
   refuses to delete a lock owned by another session, and keeps the manifest's locked-files
   list in sync:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/coord_locks.py" release $ARGUMENTS      # or: release --all
   ```
   Output per path: `OK` (released / not locked) or `FAIL — held by another session (<id>)`.

2. Announce on the shared cross-worktree channel — this is what tells a peer who is waiting
   on the file:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/_agent_channel.py" --event FILES_RELEASED "Released: $ARGUMENTS"
   ```

3. **Answer any open request for these files.** If a peer asked for this file via
   `/coord:ask-lock`, releasing the lock is only half the exchange — they are not watching
   the filesystem, so tell them directly:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agent_message.py" reply <msgId> --type GRANT --body "released <path>"
   ```

4. Report what was released and what remains held:
   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/coord_locks.py" list
   ```

### Note

Releasing is also automatic when the session itself ends cleanly — the harness runs the
same release-and-archive step for you. Explicit release is still preferred while you are
still working: it unblocks peers immediately rather than at the end of your session.
