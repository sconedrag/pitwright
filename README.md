# pitwright

**Tools for the pit crew behind your agents.** A [Claude Code](https://code.claude.com) plugin
marketplace for running Claude Code seriously: many subagents, long sessions, several sessions
on one repository at once — kept on the right model, coordinated, and inside budget.

Everything here was built and measured on a large production repository before being extracted.
Each plugin installs on its own and has its own tests.

| Plugin | Status | What it does |
|---|---|---|
| [`spawn-guards`](plugins/spawn-guards) | available | Enforces a per-agent model-tier floor on subagent spawns; flags delegation prompts that reference context the subagent cannot see. |
| `coord` | in preparation | Coordination for parallel sessions: worktree-aware locks, durable addressed messages between sessions, a live board, merge-collision foresight, a concurrency-safe memory index, a global build-slot semaphore. |
| `agent-economics` | in preparation | Per-agent × model cost and latency from session transcripts, delegation rate by call and by cost, requested-vs-served tier, A/B harnesses for tier choices. |
| `context-hooks` | in preparation | A context-fullness notice, a pre-compaction gate that waits for a natural stopping point, and a nudge for under-specified prompts. |

## Install

```text
/plugin marketplace add sconedrag/pitwright
/plugin install spawn-guards@pitwright
```

Plugins need `python3` on `PATH` and use only its standard library.

## License

Apache-2.0 — see [LICENSE](LICENSE).
