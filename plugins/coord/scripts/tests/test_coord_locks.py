#!/usr/bin/env python3
"""
test_coord_locks.py — invariants for scripts/coord_locks.py.

These lock down two drift bugs that recur whenever a skill re-implements locking in prose
instead of calling the shared module, which then diverges from the enforcing hook:

  1. KEY PARITY — the Python key must equal the documented key formula
     `sha256("<worktree>:<relpath>")[:16]`, the same formula a shell implementation of the
     edit-time guard must be able to reproduce. A skill that hashes the bare path instead
     puts its claims in a different namespace from the guard, so the claim protects nothing.
  2. LEGACY LOCKS SURVIVE — a plain-text lock owned by a LIVE session must NOT be deleted.
     A guard that treats 'unparseable' as 'stale' and rm -f's it silently destroys a peer's
     lock.
  3. LIVENESS — a lock whose owning PID is alive is never stale, however long idle;
     a dead-PID owner past the heartbeat threshold is.
  4. CONTENTION — a second live session cannot take a held lock.

Run: python3 scripts/tests/test_coord_locks.py   (exit 0 = pass)
No external deps (plain asserts).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
os.environ.pop("CLAUDE_CODE_SESSION_ID", None)  # hermetic: tests pin identity themselves

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import coord_locks as CL  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        FAILURES.append(label)


def _bash_key(wt: str, rel: str) -> str:
    """Recompute the key via the same shell pipeline a bash implementation of the guard
    would use, to prove it lands on the same key as the Python formula."""
    # shell=True is the point here — it reproduces the bash key formula via a real shell
    # pipeline. Interpolated values are test-local constants, not user input.
    out = subprocess.run(
        f'printf %s "{wt}:{rel}" | shasum -a 256 | cut -c1-16',
        # nosemgrep: python.lang.security.audit.subprocess-shell-true.subprocess-shell-true
        shell=True, capture_output=True, text=True,
    )
    return out.stdout.strip()


def test_key_parity() -> None:
    print("\n[1] key parity with the documented bash key formula")
    wt = CL.worktree_id()
    for rel in (
        "MyApp/UI/Views/ChatPanelView.swift",
        "scripts/coord_locks.py",
        "Documentation/Dev Guides/Core/CLOSEOUT_HYGIENE.md",  # spaces in path
    ):
        py = hashlib.sha256(f"{wt}:{rel}".encode()).hexdigest()[:16]
        sh = _bash_key(wt, rel)
        check(py == sh, f"{rel[:48]:<48} py={py} sh={sh}")

    # The regression itself: path-only hashing must NOT equal the worktree-scoped key.
    rel = "MyApp/UI/Views/ChatPanelView.swift"
    path_only = hashlib.sha256(rel.encode()).hexdigest()[:16]
    scoped = hashlib.sha256(f"{wt}:{rel}".encode()).hexdigest()[:16]
    check(path_only != scoped,
          "path-only key differs from worktree-scoped key (the original drift)")


def _sandbox(tmp: Path) -> None:
    """Point coord_locks at a hermetic coordination dir."""
    (tmp / ".claude" / "coordination" / "locks").mkdir(parents=True, exist_ok=True)
    (tmp / ".claude" / "coordination" / "sessions").mkdir(parents=True, exist_ok=True)
    CL.project_root = lambda: tmp          # type: ignore[assignment]
    CL.worktree_id = lambda: "testwt"      # type: ignore[assignment]


def _write_session(tmp: Path, sid: str, pid: int, heartbeat: str) -> None:
    (tmp / ".claude" / "coordination" / "sessions" / f"{sid}.json").write_text(json.dumps({
        "sessionId": sid, "pid": pid, "lastHeartbeat": heartbeat,
        "domain": "testing", "lockedFiles": [],
    }))


def test_lifecycle_and_legacy() -> None:
    print("\n[2] claim/release lifecycle, legacy locks, liveness, contention")
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _sandbox(tmp)
        live_pid = os.getpid()
        now = "2099-01-01T00:00:00"          # far-future heartbeat = fresh
        old = "2000-01-01T00:00:00"          # ancient heartbeat = stale if PID dead

        _write_session(tmp, "SESS_A", live_pid, now)
        _write_session(tmp, "SESS_B", live_pid, now)
        _write_session(tmp, "SESS_DEAD", 999999999, old)

        target = "MyApp/Foo.swift"

        ok, why = CL.claim(target, sid="SESS_A", domain="testing")
        check(ok, f"SESS_A claims a free file ({why})")

        ok, _ = CL.claim(target, sid="SESS_A")
        check(ok, "re-claim by the same session is idempotent")

        ok, why = CL.claim(target, sid="SESS_B")
        check(not ok, f"SESS_B is refused a held lock ({why})")

        o = CL.owner(target)
        check(o is not None and o["sessionId"] == "SESS_A", "owner() attributes the lock")

        rec = json.loads(CL.lock_path(target).read_text())
        check(set(rec) >= {"file", "worktree", "sessionId", "domain", "lockedAt"},
              "lock record carries the guard's JSON schema")

        ok, why = CL.release(target, sid="SESS_B")
        check(not ok, f"SESS_B cannot release SESS_A's lock ({why})")
        ok, _ = CL.release(target, sid="SESS_A")
        check(ok and not CL.lock_path(target).exists(), "owner releases cleanly")

        # --- legacy plain-text lock owned by a LIVE session must survive a claim attempt
        legacy_target = "MyApp/Legacy.swift"
        CL.lock_path(legacy_target).write_text("SESS_A\n2026-08-01T00:00:00Z\n" + legacy_target + "\n")
        parsed = CL.read_lock(legacy_target)
        check(parsed is not None and parsed["sessionId"] == "SESS_A",
              "legacy text lock is parsed, not treated as corrupt")
        ok, why = CL.claim(legacy_target, sid="SESS_B")
        check(not ok, f"live-owned legacy lock is NOT stolen ({why})")
        check(CL.lock_path(legacy_target).exists(),
              "live-owned legacy lock still exists (not destructively deleted)")

        # --- a dead-PID owner past the heartbeat threshold IS reclaimable
        dead_target = "MyApp/Dead.swift"
        ok, _ = CL.claim(dead_target, sid="SESS_DEAD")
        check(ok, "SESS_DEAD takes a lock")
        check(CL.owner(dead_target) is None, "dead+ancient owner reads as stale")
        ok, why = CL.claim(dead_target, sid="SESS_B")
        check(ok, f"a live session reclaims a genuinely stale lock ({why})")

        # --- a live-but-idle owner is NEVER stale, however old the heartbeat
        idle_target = "MyApp/Idle.swift"
        _write_session(tmp, "SESS_IDLE", live_pid, old)   # alive PID, ancient heartbeat
        ok, _ = CL.claim(idle_target, sid="SESS_IDLE")
        check(ok, "SESS_IDLE takes a lock")
        o = CL.owner(idle_target)
        check(o is not None,
              "live PID + ancient heartbeat is NOT stale (never steal from someone who walked away)")


def main() -> int:
    print("test_coord_locks.py")
    test_key_parity()
    test_lifecycle_and_legacy()
    print(f"\n{'FAILED: ' + str(len(FAILURES)) if FAILURES else 'ALL PASS'}")
    for f in FAILURES:
        print(f"  - {f}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
