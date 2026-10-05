#!/usr/bin/env python3
"""test_heartbeat_formats.py — every heartbeat reader accepts every heartbeat writer's format.

Two formats are written today: `session_manifest` writes timezone-aware ISO
(`2026-10-01T12:00:00.123456+00:00`); the registry, locks and reaper write naive UTC with a
`Z` suffix (`2026-10-01T12:00:00Z`). Readers that parsed with `rstrip("Z")` and subtracted
from a naive `utcnow()` raised TypeError on the aware form — outside their `except` — so
`coord_locks.liveness()` crashed on any manifest the SessionStart hook had written, and
`is_stale()` crashed the moment such a session's pid died.

Run: python3 tests/test_heartbeat_formats.py   (exit 0 = pass)
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
sys.path.insert(0, str(SCRIPTS))

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


def _aware(age_s: int) -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(seconds=age_s)).isoformat()


def _naive_z(age_s: int) -> str:
    t = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return (t - datetime.timedelta(seconds=age_s)).isoformat() + "Z"


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp).resolve()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        os.chdir(repo)
        os.environ["CLAUDE_PROJECT_DIR"] = str(repo)
        os.environ.pop("COORD_STALE_SECONDS", None)
        import coord_locks as CL
        import render_board as RB
        import reap_coordination as RC

        sessions = repo / ".claude" / "coordination" / "sessions"
        sessions.mkdir(parents=True)
        dead = _dead_pid()
        old, fresh = 10 * 86400, 30

        for label, fmt in (("aware", _aware), ("naive+Z", _naive_z)):
            for sid, age in ((f"old_{label}", old), (f"fresh_{label}", fresh)):
                (sessions / f"{sid}.json").write_text(json.dumps({
                    "sessionId": sid, "pid": dead, "host": CL.HOST,
                    "lastHeartbeat": fmt(age)}))
            try:
                v_old = CL.liveness(f"old_{label}")["state"]
                v_fresh = CL.liveness(f"fresh_{label}")["state"]
            except Exception as exc:  # noqa: BLE001 - the crash IS the failure being tested
                v_old = v_fresh = f"raised {type(exc).__name__}"
            check(v_old == CL.LIVE_DEAD, f"{label}: liveness of an old dead session is dead ({v_old})")
            check(v_fresh == CL.LIVE_UNKNOWN,
                  f"{label}: liveness of a fresh dead-pid session is unknown ({v_fresh})")
            try:
                stale_old = CL.is_stale({"sessionId": f"old_{label}"})
                stale_fresh = CL.is_stale({"sessionId": f"fresh_{label}"})
            except Exception as exc:  # noqa: BLE001
                stale_old = stale_fresh = f"raised {type(exc).__name__}"
            check(stale_old is True and stale_fresh is False,
                  f"{label}: is_stale old/fresh = True/False ({stale_old}/{stale_fresh})")
            check(RB._age_str(fmt(fresh)) != "?", f"{label}: board renders the age")
            age = RC._heartbeat_age_seconds({"lastHeartbeat": fmt(fresh)})
            check(age < 3600, f"{label}: reaper reads the age ({age})")

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        return 1
    print("\nall heartbeat format checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
