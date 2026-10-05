---
name: msg
description: Send an addressed, repliable message to a specific peer Claude session (as opposed to /coord:broadcast, which reaches everyone and is therefore mostly ignored). Use to coordinate with one session about a file, a branch, or a shared contract.
---

Send an addressed message to a peer session. Args: $ARGUMENTS

### Who is out there

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agent_message.py" whoami          # your own identity
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/session_registry.py" list         # peers, worktrees, branches
```

Also call **`ListAgents`** — the harness sees live sessions the file-based registry does not
(the registry only knows sessions that have registered, which happens automatically at
session start). Use a peer's human name, session id, or worktree name as `--to`.

### Send

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/agent_message.py" send \
  --to "<peer name | sessionId | worktree>" \
  --type FYI --intent status \
  --subject "one line" --body "the detail" \
  --files path/one.py path/two.py
```

Types: `REQUEST` (expects an answer) · `FYI` (no answer expected) · `PROPOSE`/`COUNTER`/
`ACCEPT` (negotiating a shared contract) · `GRANT`/`DENY`/`DEFER` (answers) · `ACK` ·
`WITHDRAW`. Intents: `lock-release`, `handoff`, `adjacency`, `interface-proposal`,
`memory-share`, `lease`, `status`, `other`.

Then **ring the doorbell**: the command prints a `SendMessage` payload — call `SendMessage`
with it so a live peer sees it now. The message is already durable in the mailbox, so the
doorbell only buys latency; a peer that is offline still receives it at its next session
start or next `/coord:inbox`.

### Address it. Do not broadcast.

`--to all` is refused unless you also pass `--broadcast`, deliberately: unaddressed messages
are exactly what trains people to stop reading a shared channel. If it concerns one session,
name that session.

### Ask for a boundary, not for labour

Before sending, check which of these you are actually asking for:

- **their own state** — release a lock, tell me your branch status ✅
- **a shared contract** — agree this signature, agree who lands first ✅
- **an FYI** — we collide here, this finding affects you ✅
- **their hands on your task** — "can you run the regen while you're in there" ❌

The last one dissolves the boundary rather than defining it. The peer then holds two
objectives and will prioritise its own, so your objective quietly loses its advocate — and
nobody notices until the work is late and unowned. Ask instead for the thing that unblocks
*you doing it*, and say plainly that the task stays yours.

If a peer offers to take your work, decline and restate the boundary. That is not
territorialism; it is what keeps someone accountable for each objective.

### Boundaries

Ask a peer for **coordination**, not for work your own session was blocked from doing —
that is permission laundering, and the receiving session is instructed to refuse it. A
request that touches code or git will wait on the other operator's confirmation, so expect
`DEFER` as often as `GRANT`, and say what you need clearly enough that a human can decide
quickly.

### Related

`/coord:inbox` to read and reply · `/coord:ask-lock` when you specifically need a file a peer
holds · `/coord:broadcast` only for something that genuinely concerns every session.
