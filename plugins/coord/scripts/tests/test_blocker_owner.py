#!/usr/bin/env python3
"""Guard: blocker ownership resolution, three-state liveness, and the authority boundary
that decides whether a session may PING a peer or INHERIT what they left.

The stakes are asymmetric and that asymmetry is what these tests encode. Staying blocked
is an inconvenience; inheriting a live session's work destroys someone's in-flight state
and leaves them with no advocate. So uncertainty must always resolve toward "they are
still here", and `test_never_inherits_from_a_live_or_uncertain_owner` is the test that
must never be relaxed — not the happy paths around it.

Run: python3 scripts/tests/test_blocker_owner.py
"""

from __future__ import annotations
import json
import os
import sys
import tempfile
from pathlib import Path
os.environ.pop("CLAUDE_CODE_SESSION_ID", None)  # hermetic: tests pin identity themselves

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _testlib  # noqa: E402

import blocker_owner  # noqa: E402
import coord_locks  # noqa: E402

WT = "test-worktree"
NOW = "2026-08-28T12:00:00Z"


class Fixture:
    """A synthetic coordination dir. Patches the module's path accessors rather than
    chdir-ing, so a failing test cannot leave the real .claude/coordination mutated —
    two probes in this session already leaked live coordination state that way."""

    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "sessions").mkdir()
        (self.root / "locks").mkdir()
        self._saved = {}

    def __enter__(self):
        for name, fn in (
            ("coord_dir", lambda: self.root),
            ("sessions_dir", lambda: self.root / "sessions"),
            ("locks_dir", lambda: self.root / "locks"),
            ("worktree_id", lambda: WT),
            ("session_id", lambda: "me"),
        ):
            self._saved[name] = getattr(coord_locks, name)
            setattr(coord_locks, name, fn)
        self._saved_registry = blocker_owner._registry
        blocker_owner._registry = lambda: self.registry
        self.registry = {}
        return self

    def __exit__(self, *exc):
        for name, fn in self._saved.items():
            setattr(coord_locks, name, fn)
        blocker_owner._registry = self._saved_registry
        self.tmp.cleanup()
        return False

    def session(self, sid, *, pid=None, host=coord_locks.HOST, heartbeat=None,
                branch=None, name=None):
        m = {"sessionId": sid, "lastHeartbeat": heartbeat or _iso(0)}
        if pid is not None:
            m["pid"] = pid
        if host is not None:
            m["host"] = host
        (self.root / "sessions" / f"{sid}.json").write_text(json.dumps(m))
        if branch:
            self.registry[sid] = {"branch": branch, "humanName": name or sid}
        return self

    def lock(self, sid, path):
        key = coord_locks.lock_key(path, WT)
        (self.root / "locks" / f"{key}.lock").write_text(json.dumps(
            {"file": path, "worktree": WT, "sessionId": sid,
             "domain": "test", "lockedAt": NOW}))
        return self


def _iso(age_seconds):
    import datetime
    # Naive UTC, matching what coord_locks._heartbeat_age parses back.
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return (now - datetime.timedelta(seconds=age_seconds)).isoformat() + "Z"


DEAD_PID = _testlib.dead_pid()   # derived, not assumed — see _testlib
LIVE_PID = os.getpid()
OLD = 48 * 3600      # older than the 24h default staleness threshold


# ----------------------------------------------------------------- liveness: three states

def test_live_pid_is_alive_however_idle():
    """A process that is up is alive even after days idle — someone who walked away
    still owns their work."""
    with Fixture() as f:
        f.session("peer", pid=LIVE_PID, heartbeat=_iso(30 * 24 * 3600))
        v = coord_locks.liveness("peer")
        assert v["state"] == coord_locks.LIVE_ALIVE, v
        assert v["inheritable"] is False


def test_dead_pid_with_old_heartbeat_is_dead():
    with Fixture() as f:
        f.session("peer", pid=DEAD_PID, heartbeat=_iso(OLD))
        v = coord_locks.liveness("peer")
        assert v["state"] == coord_locks.LIVE_DEAD, v
        assert v["inheritable"] is True


def test_missing_pid_is_unknown_not_dead():
    """Ambiguity between 'remote session' and 'legacy manifest' is not death."""
    with Fixture() as f:
        f.session("peer", pid=None, heartbeat=_iso(OLD))
        v = coord_locks.liveness("peer")
        assert v["state"] == coord_locks.LIVE_UNKNOWN, v
        assert v["inheritable"] is False


def test_pid_recorded_on_another_host_is_unknown():
    """os.kill against a foreign pid probes an unrelated LOCAL process — the answer would
    be confidently wrong in either direction, which is worse than no answer."""
    with Fixture() as f:
        f.session("peer", pid=LIVE_PID, host="some-other-machine", heartbeat=_iso(OLD))
        v = coord_locks.liveness("peer")
        assert v["state"] == coord_locks.LIVE_UNKNOWN, v
        assert "another host" in v["reason"]


def test_dead_pid_with_recent_heartbeat_is_unknown():
    """Process gone but heartbeat fresh = mid-restart or an unconfirmed crash."""
    with Fixture() as f:
        f.session("peer", pid=DEAD_PID, heartbeat=_iso(5))
        v = coord_locks.liveness("peer")
        assert v["state"] == coord_locks.LIVE_UNKNOWN, v


def test_absent_manifest_is_unknown_not_dead():
    with Fixture():
        v = coord_locks.liveness("never-existed")
        assert v["state"] == coord_locks.LIVE_UNKNOWN, v
        assert v["inheritable"] is False


# --------------------------------------------------- the invariant that must never relax

def test_never_inherits_from_a_live_or_uncertain_owner():
    """THE load-bearing test. No liveness state other than `dead` may ever authorise
    inheritance, for any blocker type, at any heartbeat age."""
    cases = {
        "live":            dict(pid=LIVE_PID, heartbeat=_iso(OLD)),
        "no-pid":          dict(pid=None, heartbeat=_iso(OLD)),
        "other-host":      dict(pid=LIVE_PID, host="elsewhere", heartbeat=_iso(OLD)),
        "recent-heartbeat": dict(pid=DEAD_PID, heartbeat=_iso(5)),
    }
    for label, kw in cases.items():
        with Fixture() as f:
            f.session("peer", branch="feat/x", **kw)
            f.lock("peer", "a.py")
            verdicts = blocker_owner.resolve(paths=["a.py"], branch="feat/x", dirty=False)
            assert verdicts, f"{label}: expected the peer to be found"
            for v in verdicts:
                assert v["recommendedAction"] == blocker_owner.ACT_PING, \
                    f"{label}: {v['blocker']} authorised {v['recommendedAction']}"


# ------------------------------------------------------------- reversibility tiering

def test_dead_owner_of_a_lock_is_inheritable():
    """A lock is a claim, not work — transferring it destroys nothing."""
    with Fixture() as f:
        f.session("peer", pid=DEAD_PID, heartbeat=_iso(OLD))
        f.lock("peer", "a.py")
        v = blocker_owner.owner_of_path("a.py")
        assert v["recommendedAction"] == blocker_owner.ACT_INHERIT, v


def test_dead_owner_of_a_branch_goes_to_the_operator():
    """Their git state is not reversible, however certainly dead they are."""
    with Fixture() as f:
        f.session("peer", pid=DEAD_PID, heartbeat=_iso(OLD), branch="feat/x")
        v = blocker_owner.owner_of_branch("feat/x")
        assert v["recommendedAction"] == blocker_owner.ACT_PROPOSE, v
        assert v["reversible"] is False


def test_dead_owner_of_uncommitted_work_goes_to_the_operator():
    with Fixture() as f:
        f.session("peer", pid=DEAD_PID, heartbeat=_iso(OLD))
        f.lock("peer", "wip.py")
        rows = blocker_owner.owners_of_dirty(["wip.py"])
        assert len(rows) == 1, rows
        assert rows[0]["recommendedAction"] == blocker_owner.ACT_PROPOSE, rows[0]


# ------------------------------------------------------------------ ownership resolution

def test_my_own_lock_is_not_a_blocker():
    with Fixture() as f:
        f.session("me", pid=LIVE_PID)
        f.lock("me", "a.py")
        assert blocker_owner.owner_of_path("a.py") is None
        assert blocker_owner.owners_of_dirty(["a.py"]) == []


def test_unlocked_path_has_no_owner():
    with Fixture():
        assert blocker_owner.owner_of_path("nobody-holds-this.py") is None


def test_every_verdict_carries_evidence_and_a_human_name():
    """An owner asserted without provenance is the failure this repo keeps re-learning."""
    with Fixture() as f:
        f.session("peer", pid=LIVE_PID, branch="feat/x", name="Router work")
        f.lock("peer", "a.py")
        for v in blocker_owner.resolve(paths=["a.py"], branch="feat/x"):
            assert v["evidence"], v
            assert v["livenessReason"], v
        assert blocker_owner.owner_of_branch("feat/x")["humanName"] == "Router work"


# ------------------------------------------------------- ownership comes from disk

def test_locked_files_derives_from_disk_not_the_manifest_array():
    """The manifest array is lossy in both directions: the guard's fast path returns
    before appending, and the reaper clears it wholesale. A session holding real locks
    while its array reads empty is the common state, and it made the worktree gate call a
    just-edited file somebody else's."""
    with Fixture() as f:
        f.session("peer", pid=LIVE_PID)          # manifest has NO lockedFiles key at all
        f.lock("peer", "held.py")
        assert coord_locks.locked_files("peer", WT) == ["held.py"]


def test_locked_files_ignores_other_worktrees():
    with Fixture() as f:
        f.session("peer", pid=LIVE_PID)
        f.lock("peer", "held.py")
        assert coord_locks.locked_files("peer", "a-different-worktree") == []


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
