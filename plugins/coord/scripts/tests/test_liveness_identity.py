#!/usr/bin/env python3
"""Guard: the two inputs a liveness verdict rests on — the recorded pid and the
recorded machine — must not be able to make a LIVE session look abandoned.

Both were found shortly after the liveness verdict shipped, and both were invisible
because they fail toward plausible-looking output rather than error.

  1. A wrong pid is strictly WORSE than no pid. Once a heartbeat ages past the
     staleness threshold:
         absent            -> unknown -> never inheritable   (safe)
         correct, alive    -> alive   -> never inheritable   (protected)
         wrong, dead       -> DEAD    -> INHERITABLE         (live work adoptable)
     So a helper recording `os.getppid()` from a throwaway shell does not store a
     merely-useless number; it converts "idle but alive" into "abandoned, take it".

  2. `socket.gethostname()` is not stable. macOS rewrites the mDNS name with the
     network — the same machine reported `MacBook-Pro.local` and `Mac.lan` hours
     apart. A host mismatch collapses every verdict to `unknown`, which is safe but
     silently disables inheritance, and a disabled feature is indistinguishable from
     a working one that found nothing.

Run: python3 scripts/tests/test_liveness_identity.py
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

import coord_locks  # noqa: E402
import session_manifest  # noqa: E402

LIVE_PID = os.getpid()
DEAD_PID = _testlib.dead_pid()


# ------------------------------------------------------------------ pid validation

def test_dead_pid_is_not_recorded():
    """The core fix: a pid already dead at write time is dropped, not stored."""
    assert session_manifest._validated_pid(DEAD_PID) == 0


def test_live_pid_is_recorded():
    assert session_manifest._validated_pid(LIVE_PID) == LIVE_PID


def test_absent_and_nonsense_pids_become_zero():
    for bad in (None, 0, -1, "not-a-pid", 3.7e400):
        assert session_manifest._validated_pid(bad) == 0, bad


def test_a_dropped_pid_yields_unknown_never_dead():
    """The property that makes dropping correct: no pid can never authorise
    inheritance, however stale the heartbeat gets."""
    with _fixture() as (root, sid):
        _write(root, sid, pid=0, heartbeat_age=90 * 24 * 3600)
        v = coord_locks.liveness(sid)
        assert v["state"] == coord_locks.LIVE_UNKNOWN, v
        assert v["inheritable"] is False


def test_a_wrong_pid_would_have_been_inheritable():
    """Pins the hazard itself, so the fix cannot be quietly reverted: a stored dead
    pid + an aged heartbeat DOES read as inheritable. This is what _validated_pid
    prevents from ever being written."""
    with _fixture() as (root, sid):
        _write(root, sid, pid=DEAD_PID, heartbeat_age=90 * 24 * 3600)
        v = coord_locks.liveness(sid)
        assert v["state"] == coord_locks.LIVE_DEAD, v
        assert v["inheritable"] is True, (
            "if this ever stops being true the hazard is gone and this test can go; "
            "while it IS true, _validated_pid is what stands between a live session "
            "and having its work adopted"
        )


# --------------------------------------------------------------- machine identity

def test_machine_id_is_stable_across_calls():
    assert coord_locks.machine_id() == coord_locks.machine_id()


def test_machine_id_is_not_the_mdns_hostname_on_darwin():
    """The whole point: it must not track the name that changes with the network."""
    if sys.platform != "darwin":
        return
    import socket
    assert coord_locks.machine_id() != socket.gethostname(), (
        "machine_id fell back to gethostname on darwin — the hardware-UUID probe failed, "
        "so a network change will silently disable inheritance again"
    )
    assert coord_locks.machine_id().startswith("hw:")


def test_machine_id_never_returns_empty():
    """Every fallback path must yield SOMETHING; an empty host compares equal to a
    missing one and would quietly re-enable cross-machine pid probing."""
    assert coord_locks.machine_id().strip()


def test_manifest_records_the_stable_id_not_the_hostname():
    import socket
    assert session_manifest._machine_id() == coord_locks.machine_id()
    if sys.platform == "darwin":
        assert session_manifest._machine_id() != socket.gethostname()


def test_legacy_hostname_manifest_reads_unknown_not_dead():
    """Manifests written before this change carry a hostname. They compare unequal to
    a hardware UUID — which must degrade to `unknown`, never to `dead`."""
    with _fixture() as (root, sid):
        _write(root, sid, pid=LIVE_PID, host="MacBook-Pro.local",
               heartbeat_age=90 * 24 * 3600)
        v = coord_locks.liveness(sid)
        assert v["state"] == coord_locks.LIVE_UNKNOWN, v
        assert v["inheritable"] is False


# ------------------------------------------------------------------------ fixture

class _fixture:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()

    def __enter__(self):
        root = Path(self.tmp.name)
        (root / "sessions").mkdir()
        (root / "locks").mkdir()
        self._saved = (coord_locks.sessions_dir, coord_locks.locks_dir)
        coord_locks.sessions_dir = lambda: root / "sessions"
        coord_locks.locks_dir = lambda: root / "locks"
        return root, "sess"

    def __exit__(self, *exc):
        coord_locks.sessions_dir, coord_locks.locks_dir = self._saved
        self.tmp.cleanup()
        return False


def _write(root, sid, *, pid, heartbeat_age, host=None):
    import datetime
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    hb = (now - datetime.timedelta(seconds=heartbeat_age)).isoformat() + "Z"
    (root / "sessions" / f"{sid}.json").write_text(json.dumps({
        "sessionId": sid, "pid": pid,
        "host": coord_locks.HOST if host is None else host,
        "lastHeartbeat": hb,
    }))


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
