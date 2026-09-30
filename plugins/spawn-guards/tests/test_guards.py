#!/usr/bin/env python3
"""Behavioural tests for the spawn-guards hooks.

Each hook is run as Claude Code runs it: the PreToolUse payload on stdin, the verdict in the exit
code (2 = block) and stderr. HOME, CLAUDE_PROJECT_DIR and CLAUDE_PLUGIN_DATA point at scratch
directories so a developer's own ~/.claude/agents cannot change the outcome.

Run: python3 plugins/spawn-guards/tests/test_guards.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Overridable so a deliberately broken copy can be tested: a suite that has only ever been seen
# passing is not evidence that it can fail.
SCRIPTS = Path(os.environ.get("SPAWN_GUARDS_SCRIPTS", Path(__file__).resolve().parents[1] / "scripts"))
MODEL_GUARD = SCRIPTS / "model-guard.sh"
PROMPT_GUARD = SCRIPTS / "prompt-context-guard.sh"
GOOD_PROMPT = ("Find every call site of parse_config in src/config.py and return a list of "
               "file:line entries; verify each by reading the line.")
FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'ok  ' if cond else 'FAIL'} {name}" + ("" if cond else f"  — {detail}"))
    if not cond:
        FAILURES.append(name)


def run(script: Path, tool_input: dict | str, root: Path, **env: str) -> tuple[int, str]:
    stdin = tool_input if isinstance(tool_input, str) else json.dumps(
        {"tool_name": "Agent", "tool_input": tool_input})
    full_env = {"PATH": os.environ["PATH"], "HOME": str(root / "home"),
                "CLAUDE_PROJECT_DIR": str(root / "project"),
                "CLAUDE_PLUGIN_DATA": str(root / "data"), **env}
    proc = subprocess.run(["bash", str(script)], input=stdin, capture_output=True, text=True,
                          env=full_env, timeout=30)
    return proc.returncode, proc.stderr


def spawn(agent: str, model: str | None = None, prompt: str = GOOD_PROMPT) -> dict:
    ti = {"subagent_type": agent, "prompt": prompt, "description": "t"}
    if model:
        ti["model"] = model
    return ti


def test_model_guard(root: Path) -> None:
    rc, err = run(MODEL_GUARD, spawn("Explore"), root)
    check("Explore with no model is blocked", rc == 2 and "BLOCKED" in err, f"rc={rc} {err}")
    rc, err = run(MODEL_GUARD, spawn("Explore", "haiku"), root)
    check("Explore at its floor passes silently", rc == 0 and not err.strip(), f"rc={rc} {err}")
    rc, err = run(MODEL_GUARD, spawn("Explore", "sonnet"), root)
    check("Explore above its ceiling passes WITH an advisory",
          rc == 0 and "ceiling" in err, f"rc={rc} {err}")
    rc, _ = run(MODEL_GUARD, spawn("some-agent-with-no-floor"), root)
    check("an agent with no floor passes", rc == 0)
    rc, _ = run(MODEL_GUARD, spawn("fork"), root)
    check("a fork is never tier-checked", rc == 0)
    rc, _ = run(MODEL_GUARD, "not json", root)
    check("an unparseable payload fails open", rc == 0)
    rc, _ = run(MODEL_GUARD, spawn("Explore"), root, SPAWN_GUARDS_OK="1")
    check("SPAWN_GUARDS_OK=1 disables the block", rc == 0)
    rc, _ = run(MODEL_GUARD, spawn("Explore", "some-future-model"), root)
    check("an unknown model name is never blocked on a guess", rc == 0)


def test_floor_sources(root: Path) -> None:
    agents = root / "project" / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "reviewer.md").write_text("---\nname: reviewer\nmodel: sonnet\n---\nReview code.\n")
    rc, err = run(MODEL_GUARD, spawn("reviewer", "haiku"), root)
    check("an agent file's model: is its floor (below it is blocked)",
          rc == 2 and "below its floor" in err, f"rc={rc} {err}")
    rc, _ = run(MODEL_GUARD, spawn("reviewer", "opus"), root)
    check("escalating above an agent-file floor is allowed", rc == 0)

    (root / "project" / ".claude" / "spawn-guards.json").write_text(
        json.dumps({"floors": {"planner": "opus"}}))
    rc, err = run(MODEL_GUARD, spawn("planner", "sonnet"), root)
    check("a floor from .claude/spawn-guards.json is enforced", rc == 2, f"rc={rc} {err}")

    (root / "project" / ".claude" / "spawn-guards.json").write_text(
        json.dumps({"floors": {"planner": "opus", "fork": "opus"}}))
    rc, err = run(MODEL_GUARD, spawn("fork"), root)
    check("a fork is exempt even when a floor is (mis)configured for it", rc == 0, f"rc={rc} {err}")

    user_agents = root / "home" / ".claude" / "agents"
    user_agents.mkdir(parents=True)
    (user_agents / "user-agent.md").write_text("---\nmodel: opus\n---\n")
    rc, _ = run(MODEL_GUARD, spawn("user-agent", "haiku"), root)
    check("a user-level agent file's floor is enforced", rc == 2)


def test_quality_advisory(root: Path) -> None:
    weak = spawn("general-purpose", "haiku", prompt="fix it")
    rc, err = run(MODEL_GUARD, weak, root)
    check("an under-specified delegation gets an advisory, not a block",
          rc == 0 and "under-specified" in err, f"rc={rc} {err}")
    rc, err = run(MODEL_GUARD, weak, root)
    check("the same weak prompt is not warned about twice", rc == 0 and not err.strip(), err)
    rc, err = run(MODEL_GUARD, spawn("general-purpose", "haiku"), root)
    check("a well-specified delegation gets no advisory", rc == 0 and not err.strip(), err)


def test_prompt_context_guard(root: Path) -> None:
    dangling = spawn("general-purpose", "haiku",
                     prompt="Fix the bug we discussed earlier and report what changed.")
    rc, err = run(PROMPT_GUARD, dangling, root)
    check("a parent-context reference is flagged (advisory by default)",
          rc == 0 and "parent-context" in err, f"rc={rc} {err}")
    rc, err = run(PROMPT_GUARD, dangling, root, PROMPT_CONTEXT_GUARD_STRICT="1")
    check("strict mode blocks it", rc == 2 and "BLOCKED" in err, f"rc={rc} {err}")
    rc, err = run(PROMPT_GUARD, {**dangling, "subagent_type": "fork"}, root,
                  PROMPT_CONTEXT_GUARD_STRICT="1")
    check("a fork may reference earlier context", rc == 0 and not err.strip(), err)
    rc, err = run(PROMPT_GUARD, spawn("general-purpose", "haiku"), root,
                  PROMPT_CONTEXT_GUARD_STRICT="1")
    check("a self-contained prompt passes strict mode", rc == 0 and not err.strip(), err)


def main() -> int:
    for fn in (test_model_guard, test_floor_sources, test_quality_advisory,
               test_prompt_context_guard):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "project").mkdir()
            (root / "home").mkdir()
            fn(root)
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED")
        return 1
    print("\nall spawn-guards tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
