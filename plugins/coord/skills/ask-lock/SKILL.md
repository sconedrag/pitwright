---
name: ask-lock
description: Ask the session that holds a file lock to release it, opening a repliable thread. Use when an edit is blocked by "FILE LOCKED by another session" instead of waiting silently or deleting the lock.
---

Request a locked file from its owner. Args: $ARGUMENTS — `<path> "<why you need it>"`

### 1. See who holds it

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/peer_context.py" <path>
```

If it reports **free**, just retry the edit — the lock was stale and has been reclaimed.
If it reports a **stale lock present**, retry too; the next claim reclaims it.

### 2. Ask

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agent_message.py" send \
  --to "<owner humanName or sessionId>" \
  --type REQUEST --intent lock-release \
  --subject "release <path>?" \
  --body "<why you need it, how long you need it, and whether it can wait>" \
  --files <path>
```

Then **ring the doorbell** — the command prints a `SendMessage` payload; call `SendMessage`
with it so a live owner sees the ask in seconds rather than at its next inbox check.

Say what would unblock you *specifically*. "A `/coord:release-files` on `config.py` would
unblock me; it's a 3-line additive change, ~2 minutes" gets answered. "Please release" does
not.

### 3. While you wait — do not block

The request is durable; it survives both sessions restarting. Meanwhile:

- **Work on something else.** Do not spin or poll.
- **Never delete the lock.** A lock whose owning process is alive is never stale, however
  long idle — the owner may be mid-edit with unsaved work.
- If it is genuinely blocking and the owner is unresponsive, **escalate to your operator**.
  Your request also resurfaces automatically in the owner's inbox at their next session
  start and on a timer while you both keep working.
- Check status with `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agent_message.py" outbox --open`.

### If you are a SUBAGENT

**Do not run this.** You share your parent session's identity (both resolve from the same
environment variable), so a peer cannot tell you apart from your parent or from sibling
subagents. Report upward instead:

> `SendMessage` to `"main"`: `COORD_BLOCKED on <path> (held by <owner>) — <what I was doing>`

Your parent is the single coordination principal and will negotiate.

---

## If you are the OWNER receiving one of these

You may **auto-release without asking your operator** when both hold — this is a reversible
coordination act on your own state, nothing more:

```bash
git status --porcelain -- <path>                                 # must print NOTHING (no uncommitted work)
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/coord_locks.py" owner <path>   # must show the lock is yours
```

Then release and answer:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/coord_locks.py" release <path>
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agent_message.py" reply <msgId> --type GRANT --body "released"
```

**Otherwise, ask your operator first.** If you have uncommitted work in that file, releasing
risks losing it — reply `DENY` with the reason, or `DEFER` with a trigger:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agent_message.py" reply <msgId> --type DEFER \
  --body "trigger: after my current edit lands (~10 min)"
```

Answer either way. An unanswered request leaves a peer blocked — that is the exact failure
this exists to fix.
