#!/usr/bin/env python3
"""
test_session_registry.py — tests for scripts/session_registry.py.

Runs REAL subprocesses against a hermetic channel (SESSION_REGISTRY_CHANNEL) and a
hermetic HOME (for the read-only native scan), asserting:
  1. REGISTER+LIST — a registered session round-trips through list --json.
  2. NAME+RENAME — self-name and user-rename (by short id) update humanName.
  3. CONCURRENCY — N concurrent registers (distinct sessions) all land, file stays valid JSON.
  4. NATIVE-READONLY — list --all surfaces a ~/.claude transcript as source="native"
     WITHOUT writing it into the writable registry file.
  5. GC/LIVENESS — a dead-PID + old-heartbeat entry is pruned; a live-PID entry is kept.

Run: python3 scripts/tests/test_session_registry.py   (exit 0 = pass)
No external deps (plain asserts).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "session_registry.py"


def _run(channel: Path, *args: str, home: Path | None = None, sid: str = "tsid") -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["SESSION_REGISTRY_CHANNEL"] = str(channel)
    env["TERM_SESSION_ID"] = sid
    if home is not None:
        env["HOME"] = str(home)
    return subprocess.run(["python3", str(SCRIPT), *args],
                          env=env, capture_output=True, text=True)


def _registry_file(channel: Path) -> Path:
    return channel / "sessions-registry.json"


def _load(channel: Path) -> dict:
    return json.loads(_registry_file(channel).read_text())


def test_register_and_list(channel: Path) -> None:
    assert _run(channel, "register", "--session-id", "uuid-A", "--cwd", os.getcwd()).returncode == 0
    out = _run(channel, "list", "--json").stdout
    rows = json.loads(out)
    assert len(rows) == 1, rows
    assert rows[0]["sessionId"] == "uuid-A"
    assert rows[0]["status"] == "active"
    assert rows[0]["source"] == "registry"
    print("  ✓ register + list round-trips")


def test_name_and_rename(channel: Path) -> None:
    _run(channel, "register", "--session-id", "uuid-B", "--cwd", os.getcwd())
    assert _run(channel, "name", "My Session", "--session-id", "uuid-B").returncode == 0
    rows = json.loads(_run(channel, "list", "--json").stdout)
    b = next(r for r in rows if r["sessionId"] == "uuid-B")
    assert b["humanName"] == "My Session", b
    # user rename by short-id prefix
    assert _run(channel, "rename", "uuid-B", "Renamed").returncode == 0
    rows = json.loads(_run(channel, "list", "--json").stdout)
    b = next(r for r in rows if r["sessionId"] == "uuid-B")
    assert b["humanName"] == "Renamed", b
    print("  ✓ self-name + user-rename update humanName")


def test_concurrency(channel: Path) -> None:
    n = 10
    procs = []
    for i in range(n):
        env = dict(os.environ)
        env["SESSION_REGISTRY_CHANNEL"] = str(channel)
        env["TERM_SESSION_ID"] = f"sess-{i}"  # distinct → real contention, not re-entrant
        procs.append(subprocess.Popen(
            ["python3", str(SCRIPT), "register", "--session-id", f"cc-uuid-{i}", "--cwd", os.getcwd()],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    for p in procs:
        p.wait()
    store = _load(channel)  # must be valid JSON (no torn write)
    got = {sid for sid in store["sessions"] if sid.startswith("cc-uuid-")}
    assert len(got) == n, f"expected {n} concurrent entries, got {len(got)}: {sorted(got)}"
    print(f"  ✓ {n} concurrent registers all landed, file valid JSON")


def test_native_readonly(channel: Path) -> None:
    with tempfile.TemporaryDirectory() as home:
        home_p = Path(home)
        proj = home_p / ".claude" / "projects" / "-tmp-fake-project"
        proj.mkdir(parents=True)
        (proj / "native-uuid-X.jsonl").write_text('{"type":"x"}\n')
        _run(channel, "register", "--session-id", "uuid-C", "--cwd", os.getcwd(), home=home_p)
        before = _registry_file(channel).read_text()
        rows = json.loads(_run(channel, "list", "--all", "--json", home=home_p).stdout)
        native = [r for r in rows if r["source"] == "native"]
        assert any(r["sessionId"] == "native-uuid-X" for r in native), rows
        after = _registry_file(channel).read_text()
        assert before == after, "native scan must NOT write into the registry file"
    print("  ✓ list --all surfaces native sessions read-only (no write-back)")


def test_gc_liveness(channel: Path) -> None:
    # A live holder (real sleep process) + a dead/old entry written directly.
    live = subprocess.Popen(["sleep", "30"])
    try:
        dead = subprocess.Popen(["sleep", "0.1"])
        dead_pid = dead.pid
        dead.wait()
        time.sleep(0.2)  # ensure the pid is actually gone

        store = {
            "schemaVersion": 1,
            "updatedAt": "2020-01-01T00:00:00Z",
            "sessions": {
                "live-uuid": {"sessionId": "live-uuid", "pid": live.pid,
                              "lastHeartbeat": "2020-01-01T00:00:00Z", "status": "active",
                              "source": "registry", "humanName": "live"},
                "dead-uuid": {"sessionId": "dead-uuid", "pid": dead_pid,
                              "lastHeartbeat": "2020-01-01T00:00:00Z", "status": "active",
                              "source": "registry", "humanName": "dead"},
            },
        }
        _registry_file(channel).write_text(json.dumps(store))
        assert _run(channel, "gc").returncode == 0
        after = _load(channel)["sessions"]
        assert "dead-uuid" not in after, "dead+old entry should be pruned"
        assert "live-uuid" in after, "live-PID entry must never be pruned"
    finally:
        live.terminate()
        live.wait()
    print("  ✓ gc prunes dead+old, keeps live PID")


def main() -> int:
    tests = [
        test_register_and_list,
        test_name_and_rename,
        test_concurrency,
        test_native_readonly,
        test_gc_liveness,
    ]
    failed = 0
    for t in tests:
        with tempfile.TemporaryDirectory() as ch:
            try:
                t(Path(ch))
            except AssertionError as e:
                failed += 1
                print(f"  ✗ {t.__name__}: {e}")
            except Exception as e:  # noqa: BLE001 — test harness surfaces all failures
                failed += 1
                print(f"  ✗ {t.__name__}: unexpected {type(e).__name__}: {e}")
    if failed:
        print(f"\nFAILED: {failed}/{len(tests)}")
        return 1
    print(f"\nPASSED: {len(tests)}/{len(tests)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
