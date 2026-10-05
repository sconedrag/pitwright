---
name: adjacency
description: Find which other worktree branches changed the same source files as yours, and tell those sessions before merge day. Use when starting work, before opening a pull request, and before a rebase.
---

Cross-worktree merge-collision early warning. Args: $ARGUMENTS

File locks are **worktree-scoped** — two sessions in different worktrees editing "the same"
file are editing different physical copies and never block each other. So across worktrees
the collision is not at edit time, it is at **merge** time. This finds it early.

### Look

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/adjacency.py"            # what MY branch collides with
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/adjacency.py" --all      # every pair, ranked
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/adjacency.py" --json
```

### Tell them

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/adjacency.py" --notify
```

Sends one addressed `FYI(adjacency)` per colliding peer and prints the doorbell payloads.
**Deduped**: the same collision is never announced twice — it re-sends only when the shared
file set actually changes.

### Then sequence, don't race

Adjacency is not a lock and does not stop anyone. Resolve it by agreeing an order:

- Whoever is closest to landing goes first; the other rebases onto that work.
- Say so explicitly in the thread — `/coord:inbox` to reply.
- If the tool's own output calls out a specific file as having its own separate
  serialization (some project metadata files are additive-merge-friendly and already
  handled that way), follow its printed advice rather than hand-merging it.

### Why the output is small

Raw overlap is almost all noise, so this filters hard before it will interrupt anyone:

- **Fresh base.** Comparisons are made against the branch's divergence from the remote
  tracking branch, not a possibly-stale local copy of it — otherwise every branch's diff
  also carries everything that has landed upstream since you last fetched, which can make
  a handful of real changes look like hundreds.
- **Churn removed.** Generated and per-run state (build artifacts, reports, caches,
  snapshots) is excluded from the comparison.
- **Breadth heuristic.** A file touched by more than half of all active branches is treated
  as shared infrastructure everyone edits, not two people colliding.

If it reports nothing, that is a real "nothing", not a broken detector — check with `--all`.
