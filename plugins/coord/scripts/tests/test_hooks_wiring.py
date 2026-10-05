#!/usr/bin/env python3
"""test_hooks_wiring.py — the plugin's hook manifest and its SessionStart hook.

  1. every command in hooks/hooks.json names a script that exists under the plugin;
  2. every hook script resolves its sibling scripts from its OWN directory, never from
     `scripts/` under the user's project (the plugin's scripts are not in the project);
  3. session_start.sh, run in a scratch repo with a SessionStart payload, exits 0, prints
     valid JSON (or nothing), and registers the session — the manifest that turns file
     locking on exists afterwards;
  4. a second live session in the same checkout gets the worktree advisory; the first did not.

Run: python3 tests/test_hooks_wiring.py   (exit 0 = pass)
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
PLUGIN = SCRIPTS.parent
FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


def _hook_commands() -> list[str]:
    data = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
    return [h["command"] for groups in data["hooks"].values()
            for g in groups for h in g["hooks"]]


def _session_start(repo: Path, sid: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, TERM_SESSION_ID=sid, CLAUDE_PROJECT_DIR=str(repo))
    payload = json.dumps({"session_id": f"uuid-{sid}", "cwd": str(repo),
                          "hook_event_name": "SessionStart", "source": "startup"})
    return subprocess.run(["bash", str(SCRIPTS / "session_start.sh")], input=payload,
                          cwd=str(repo), env=env, capture_output=True, text=True, timeout=120)


def main() -> int:
    commands = _hook_commands()
    for cmd in commands:
        m = re.search(r"\$\{CLAUDE_PLUGIN_ROOT\}/([^\"]+)", cmd)
        check(bool(m) and (PLUGIN / m.group(1)).is_file(), f"1. hook target exists: {cmd}")

    for cmd in commands:
        m = re.search(r"\$\{CLAUDE_PLUGIN_ROOT\}/([^\"]+)", cmd)
        if not m:
            continue
        text = (PLUGIN / m.group(1)).read_text()
        bad = re.findall(r"python3\s+scripts/\w+\.py", text)
        check(not bad, f"2. {m.group(1)} does not call project-relative scripts/ ({bad[:2]})")

    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp).resolve() / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)

        r = _session_start(repo, "sessFirst")
        check(r.returncode == 0, "3. session_start.sh exits 0")
        out = r.stdout.strip()
        parsed = None
        if out:
            try:
                parsed = json.loads(out)
            except ValueError:
                parsed = None
        check(out == "" or parsed is not None, "3. ...and prints valid JSON or nothing")
        manifest = repo / ".claude" / "coordination" / "sessions" / "sessFirst.json"
        check(manifest.is_file(), "3. ...and registers the session (manifest written)")
        ctx = (parsed or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
        check("other session(s) are active" not in ctx, "4. a solo session gets no advisory")

        r = _session_start(repo, "sessSecond")
        parsed = json.loads(r.stdout.strip() or "{}")
        ctx = parsed.get("hookSpecificOutput", {}).get("additionalContext", "")
        check("1 other session(s) are active" in ctx,
              "4. a second session in the same checkout gets the worktree advisory")

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        return 1
    print("\nall hook wiring checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
