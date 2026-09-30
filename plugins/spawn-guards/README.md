# spawn-guards

Two `PreToolUse` hooks for Claude Code subagent spawns.

## model-guard — enforce a model-tier floor

Which model a subagent runs on is decided by the `model` option passed at spawn time. A spawn
with no `model` inherits the main session's model, so a quick file search can silently run on
your largest model. Relying on an agent file's `model:` frontmatter alone has also been observed
not to govern the served model in some Claude Code versions.

This hook makes the intended tier enforceable at the moment it is decided:

| Stage | What it checks | Effect |
|---|---|---|
| Floor | spawn omits `model`, or requests a model below the agent's floor | **blocks** (exit 2), telling the model which tier to re-spawn with |
| Ceiling | a read-only search agent requested above its ceiling | advisory |
| Delegation quality | the prompt names no deliverable, no acceptance check, no file/symbol, or is very short — two or more signals required | advisory, once per distinct prompt |

**Where floors come from**, in order:

1. the agent's own frontmatter `model:` in `<project>/.claude/agents/<name>.md`, then
   `~/.claude/agents/<name>.md` — so re-tiering an agent in its own file updates enforcement;
2. `<project>/.claude/spawn-guards.json`;
3. built-in defaults: `Explore` and `general-purpose` → `haiku` (floor), `Explore` → `haiku` (ceiling).

```json
{
  "floors":   { "code-reviewer": "sonnet", "security-review": "opus" },
  "ceilings": { "Explore": "haiku" },
  "search_agents": ["Explore"]
}
```

Tier order is `haiku < sonnet < opus < fable`. An agent with no floor, `model: inherit`, an
unrecognised model name, a forked subagent (`subagent_type: "fork"`), or an unparseable payload
always passes — the guard never blocks on a guess.

## prompt-context-guard — flag delegations that are not self-contained

A non-fork subagent sees its own definition, the spawn prompt and the project `CLAUDE.md` — not
the parent conversation. "Fix the bug we discussed" points at context the subagent does not
have; it will not error, it will guess. This hook flags:

- **parent-context** phrases (`as discussed`, `the file we found`, `from earlier`, `the above`, …);
- **no-deliverable** — nothing says what to hand back;
- **no-boundary** — a long delegation with no stated limit on scope.

Advisory by default; `PROMPT_CONTEXT_GUARD_STRICT=1` makes findings block. Forks are exempt,
since they inherit the parent's context by design.

## Install

```text
/plugin marketplace add sconedrag/claude-devtools
/plugin install spawn-guards@claude-devtools
```

Requires `python3` on `PATH` (standard library only). Without it the hooks fail open.

**Override** for one session: `SPAWN_GUARDS_OK=1` disables both hooks.

## Tests

```bash
python3 plugins/spawn-guards/tests/test_guards.py
```

Runs each hook exactly as Claude Code does (payload on stdin, verdict in the exit code) against
scratch `HOME` / project / plugin-data directories. Point `SPAWN_GUARDS_SCRIPTS` at a modified
copy of `scripts/` to confirm the suite fails when a guard is broken.
