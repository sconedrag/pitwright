#!/usr/bin/env python3
"""test_manifest_cli.py — `session_manifest.py declare|show`, the CLI /coord:start-session uses.

  1. after the SessionStart hook registered a session, `declare` sets domain, name and
     activity, and the board shows the name;
  2. a later `declare` with only a domain keeps the name already set;
  3. a session with no manifest gets exit 1 and an explanation — the CLI never creates one
     (only the SessionStart hook knows the long-lived Claude pid).

Run: python3 tests/test_manifest_cli.py   (exit 0 = pass)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


def _run(repo: Path, sid: str, *args: str, stdin: str = "") -> subprocess.CompletedProcess:
    env = dict(os.environ, TERM_SESSION_ID=sid, CLAUDE_PROJECT_DIR=str(repo))
    return subprocess.run(list(args), input=stdin, cwd=str(repo), env=env,
                          capture_output=True, text=True, timeout=120)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp).resolve()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        _run(repo, "cliA", "bash", str(SCRIPTS / "session_start.sh"),
             stdin=json.dumps({"session_id": "uuid-cliA", "cwd": str(repo)}))
        manifest = repo / ".claude" / "coordination" / "sessions" / "cliA.json"

        r = _run(repo, "cliA", sys.executable, str(SCRIPTS / "session_manifest.py"),
                 "declare", "docs", "--name", "Docs pass", "--activity", "rewriting the README")
        m = json.loads(manifest.read_text()) if manifest.is_file() else {}
        check(r.returncode == 0, f"1. declare exits 0 ({r.stderr.strip()[:120]})")
        check((m.get("domain"), m.get("humanName"), m.get("activity"))
              == ("docs", "Docs pass", "rewriting the README"),
              f"1. domain, name and activity recorded ({m.get('domain')}, {m.get('humanName')}, "
              f"{m.get('activity')})")
        board = _run(repo, "cliA", sys.executable, str(SCRIPTS / "render_board.py")).stdout
        check("Docs pass" in board, "1. the board shows the declared name")

        _run(repo, "cliA", sys.executable, str(SCRIPTS / "session_manifest.py"), "declare", "api")
        m = json.loads(manifest.read_text())
        check(m.get("domain") == "api" and m.get("humanName") == "Docs pass",
              "2. re-declaring only the domain keeps the name")

        r = _run(repo, "nobody", sys.executable, str(SCRIPTS / "session_manifest.py"),
                 "declare", "docs")
        check(r.returncode == 1 and "SessionStart" in r.stderr,
              "3. unregistered session -> exit 1 with an explanation")
        check(not (manifest.parent / "nobody.json").exists(), "3. ...and no manifest is created")

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        return 1
    print("\nall manifest CLI checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
