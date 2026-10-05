#!/usr/bin/env python3
"""test_lock_guard.py — the edit-time lock hook, driven as Claude Code drives it.

Each case pipes a PreToolUse payload into `lock_guard.py` in a scratch git repo and checks
the exit code (0 allow, 2 block) and the lock left on disk:

  1. a registered session editing an unlocked file is allowed AND claims it;
  2. a second live session editing that file is BLOCKED, told how to ask for it;
  3. the same, with COORD_LOCKS_ADVISORY=1, is allowed with a warning and does not steal it;
  4. a session with no manifest is allowed and takes no lock (locking is opt-in per session);
  5. a path outside the project is allowed;
  6. a lock whose owner is dead (pid gone, heartbeat past the threshold) is reclaimed;
  7. the owner editing its own file again is allowed;
  8. a malformed payload never blocks.

Run: python3 tests/test_lock_guard.py   (exit 0 = pass)
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
GUARD = SCRIPTS / "lock_guard.py"
sys.path.insert(0, str(SCRIPTS))

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


def _iso(delta_seconds: int = 0) -> str:
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    t = now - datetime.timedelta(seconds=delta_seconds)
    return t.isoformat() + "Z"


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _host() -> str:
    import coord_locks
    return coord_locks.HOST


def _manifest(repo: Path, sid: str, pid: int, heartbeat_age: int = 0) -> None:
    d = repo / ".claude" / "coordination" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sid}.json").write_text(json.dumps({
        "sessionId": sid, "pid": pid, "host": _host(), "domain": "docs",
        "lastHeartbeat": _iso(heartbeat_age), "lockedFiles": [],
    }))


def _run(repo: Path, sid: str, path: str, extra_env: dict | None = None,
         raw: str | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items()
           if k not in ("COORD_LOCKS_ADVISORY", "COORD_STALE_SECONDS")}
    env.update({"TERM_SESSION_ID": sid, "CLAUDE_PROJECT_DIR": str(repo)})
    env.update(extra_env or {})
    payload = raw if raw is not None else json.dumps(
        {"tool_name": "Edit", "tool_input": {"file_path": path}})
    return subprocess.run([sys.executable, str(GUARD)], input=payload, cwd=str(repo),
                          env=env, capture_output=True, text=True, timeout=60)


def _lock_owner(repo: Path) -> list[str]:
    locks = repo / ".claude" / "coordination" / "locks"
    return sorted(json.loads(f.read_text())["sessionId"] for f in locks.glob("*.lock"))


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp).resolve() / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        target = repo / "src.txt"
        target.write_text("x\n")
        live = os.getpid()

        _manifest(repo, "sessA", live)
        _manifest(repo, "sessB", live)

        r = _run(repo, "sessA", str(target))
        check(r.returncode == 0, "1. registered session, unlocked file -> allowed")
        check(_lock_owner(repo) == ["sessA"], "1. ...and the file is now locked by that session")

        r = _run(repo, "sessB", str(target))
        check(r.returncode == 2, "2. second live session -> blocked (exit 2)")
        check("FILE LOCKED" in r.stderr and "/coord:ask-lock" in r.stderr,
              "2. ...with a message naming the lock and how to ask for it")

        r = _run(repo, "sessB", str(target), {"COORD_LOCKS_ADVISORY": "1"})
        check(r.returncode == 0 and "advisory" in r.stderr,
              "3. COORD_LOCKS_ADVISORY=1 -> allowed with a warning")
        check(_lock_owner(repo) == ["sessA"], "3. ...and the advisory edit did not steal the lock")

        other = repo / "other.txt"
        other.write_text("y\n")
        r = _run(repo, "sessUnregistered", str(other))
        check(r.returncode == 0 and _lock_owner(repo) == ["sessA"],
              "4. unregistered session -> allowed, no lock taken")

        outside = Path(tmp).resolve() / "outside.txt"
        r = _run(repo, "sessB", str(outside))
        check(r.returncode == 0, "5. path outside the project -> allowed")

        r = _run(repo, "sessA", str(target))
        check(r.returncode == 0 and r.stderr == "", "7. owner re-editing its own file -> allowed")

        _manifest(repo, "sessA", _dead_pid(), heartbeat_age=10 * 86400)
        r = _run(repo, "sessB", str(target))
        check(r.returncode == 0, "6. dead owner past the threshold -> lock reclaimed, edit allowed")
        check(_lock_owner(repo) == ["sessB"], "6. ...and the lock now belongs to the new session")

        r = _run(repo, "sessB", str(target), raw="not json")
        check(r.returncode == 0, "8. malformed payload -> never blocks")

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        return 1
    print("\nall lock_guard checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
