---
name: broadcast
description: Send an advisory message to every OTHER active session working in this repository, addressed to nobody in particular. Use for something that genuinely concerns everyone - e.g. "pause edits to X", "about to rebase main", "migrate to a worktree" - never as a substitute for asking one specific peer.
---

Broadcast a one-line advisory to every other active session. Message: $ARGUMENTS

### Prefer an addressed message

Broadcasting to everyone is exactly how a shared channel gets ignored. The send command
below refuses to address `all` unless you explicitly pass `--broadcast`, for this reason —
unaddressed messages are what train agents to stop reading a channel. If your message
concerns one session, use `/coord:msg` instead; it opens a thread the recipient can actually
answer. A broadcast has no single recipient, so use it only when every peer genuinely needs
to know.

### Send

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agent_message.py" send \
  --to all --broadcast --type FYI --intent other \
  --subject "one line" --body "$ARGUMENTS"
```

This records the message durably in the shared cross-worktree mailbox — every session's
inbox includes messages addressed to `all` — and prints a `SendMessage` payload. Call
`SendMessage` with it so an already-running, actively-listening peer sees it right away.

### Delivery reality — this is not instantaneous

A peer picks the message up:

- immediately, if you ring the doorbell (`SendMessage`) and the peer is live and listening;
- otherwise, at its own next session start (it is told it has unread messages), or whenever
  it next runs `/coord:inbox`.

There is no mechanism here that interrupts a long-running, non-prompting peer mid-task. If
it is genuinely urgent, also tell the human operator directly.

### Common uses

- "Pausing edits to `<path>` for the next few minutes — restructuring it."
- "About to rebase onto the latest shared branch; hold off pushing for a bit."
- "Heads up: `<shared file>` changed shape — rebase before touching it."

### Related

`/coord:msg` for one specific peer (preferred whenever you can name who it concerns) ·
`/coord:inbox` to read and answer · `/coord:adjacency` for a generated, targeted
"you and I are editing the same files" warning rather than a hand-written one.
