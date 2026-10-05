#!/usr/bin/env python3
"""The shared-state guard must ignore a peer's heartbeat and still catch a stray write.

WHY THIS EXISTS. `_shared_state_snapshot.py` guards the live cross-worktree channel against a
test writing records that other live sessions read. It was firing on essentially every run of
the test suite — and the mutation was never the suite's. Diffing the registry across one run
showed the only change was `lastHeartbeat` on a session owned by another worktree: a peer,
heartbeating while the suite ran.

That also retired a wrong conclusion. An earlier pass bisected for the offending test and got a
different answer every attempt, concluding "something in the suite writes, but no single test
owns it." Nothing in the suite writes. The bisect was naming whichever test happened to be
running when a peer's timer fired.

Both directions are pinned here because fixing a false positive by widening a filter is how a
guard quietly stops guarding — the second test is the one that matters.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SNAP = REPO / "scripts/_shared_state_snapshot.py"


def _mod():
    spec = importlib.util.spec_from_file_location("_sss", SNAP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _registry(tmp: Path, sessions: dict, updated: str = "2026-09-11T16:00:00Z") -> Path:
    """Write a fixture in the LIVE registry's shape.

    `updatedAt` is not decoration. The first version of these tests wrote a bare
    `{"sessions": {...}}`, the real file carries `schemaVersion` / `updatedAt` / `sessions`, and
    the missing top-level clock field is exactly what the first fix failed to normalize — so
    every test passed against a guard that still fired on the real file. A fixture invented
    rather than observed is a control that cannot come out the other way.
    """
    # The filename is load-bearing — `_digest` normalizes only `sessions-registry.json`.
    p = tmp / "sessions-registry.json"
    p.write_text(json.dumps({"schemaVersion": 1, "updatedAt": updated, "sessions": sessions}),
                 encoding="utf-8")
    return p


BASE = {
    "peer-1": {"domain": "system-infra", "worktree": "ual-burndown",
               "lastHeartbeat": "2026-09-11T16:00:00Z"},
    "peer-2": {"domain": None, "worktree": "main", "lastHeartbeat": "2026-09-11T16:00:00Z"},
}


def test_the_fixture_matches_the_live_registrys_shape() -> None:
    """Pin the fixture to reality, so the gap that let the incomplete fix pass cannot reopen."""
    m = _mod()
    ch = m.live_channel()
    live = (ch / "sessions-registry.json") if ch else None
    if live is None or not live.is_file():
        return  # no live channel here (CI); the other tests still hold
    live_keys = set(json.loads(live.read_text(encoding="utf-8")))
    with tempfile.TemporaryDirectory() as td:
        fixture_keys = set(json.loads(_registry(Path(td), BASE).read_text(encoding="utf-8")))
    missing = live_keys - fixture_keys
    assert not missing, (
        f"the live registry has top-level key(s) the fixture omits: {sorted(missing)} — any of "
        f"them could churn on a peer's write, and these tests would not notice"
    )


def test_a_peer_heartbeat_does_not_change_the_digest() -> None:
    """A peer heartbeat moves BOTH the entry's lastHeartbeat and the top-level updatedAt — the
    real write, not a simplified one. Normalizing only the first is what shipped broken."""
    m = _mod()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        before = m._digest(_registry(tmp, json.loads(json.dumps(BASE))))
        moved = json.loads(json.dumps(BASE))
        moved["peer-1"]["lastHeartbeat"] = "2026-09-11T16:54:52.233835Z"
        after = m._digest(_registry(tmp, moved, updated="2026-09-11T16:54:52.233835Z"))
    assert before == after, (
        "a peer's heartbeat still changes the digest — the guard will keep reporting another "
        "session's liveness update as a mutation by the test suite"
    )


def test_a_new_session_record_is_still_caught() -> None:
    """The bug the guard was BUILT for: a test writing live-looking session records."""
    m = _mod()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        before = m._digest(_registry(tmp, json.loads(json.dumps(BASE))))
        grown = json.loads(json.dumps(BASE))
        grown["stray-test-session"] = {"domain": "ui-ux", "worktree": "tmp",
                                       "lastHeartbeat": "2026-09-11T16:54:52Z"}
        after = m._digest(_registry(tmp, grown))
    assert before != after, "a NEW session record went undetected — the guard is now blind"


def test_a_claim_change_on_an_existing_record_is_still_caught() -> None:
    """The in-place overwrite the module docstring says a key-set diff structurally missed."""
    m = _mod()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        before = m._digest(_registry(tmp, json.loads(json.dumps(BASE))))
        hijacked = json.loads(json.dumps(BASE))
        hijacked["peer-2"]["domain"] = "ui-ux"   # would refuse every genuine ui-ux stand-in
        after = m._digest(_registry(tmp, hijacked))
    assert before != after, (
        "a domain claim overwritten in place went undetected — this is the exact failure the "
        "guard exists for, and normalizing must not have widened past the clock field"
    )


def test_normalization_is_scoped_to_the_registry_filename() -> None:
    """A contract file's own `lastHeartbeat`-shaped key must NOT be normalized away."""
    m = _mod()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        p = tmp / "some-contract.json"
        p.write_text(json.dumps({"sessions": {"a": {"lastHeartbeat": "t0"}}}), encoding="utf-8")
        before = m._digest(p)
        p.write_text(json.dumps({"sessions": {"a": {"lastHeartbeat": "t1"}}}), encoding="utf-8")
        after = m._digest(p)
    assert before != after, (
        "normalization leaked beyond sessions-registry.json — every watched file would inherit "
        "the blind spot"
    )


def test_the_controls_are_not_vacuous() -> None:
    """If `_digest` returned a constant, three of the four tests above would still pass."""
    m = _mod()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        a = m._digest(_registry(tmp, {"x": {"domain": "a"}}))
        b = m._digest(_registry(tmp, {"x": {"domain": "b"}}))
    assert a != b, "_digest is not discriminating at all"
    assert len(a) == 16, f"unexpected digest shape: {a!r}"
    assert "sessions-registry.json" in SNAP.read_text(encoding="utf-8"), (
        "the filename these tests depend on is gone from the guard"
    )


def main() -> int:
    tests = sorted((n, f) for n, f in globals().items()
                   if n.startswith("test_") and callable(f))
    if not tests:
        print("test_shared_state_snapshot: discovered ZERO tests — broken, not clean")
        return 2
    failures: list[str] = []
    for name, fn in tests:
        try:
            fn()
        except AssertionError as exc:
            failures.append(f"{name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: {type(exc).__name__}: {exc}")
    if failures:
        print(f"\n{len(failures)} of {len(tests)} shared-state-snapshot test(s) failed:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"all {len(tests)} shared-state-snapshot tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
