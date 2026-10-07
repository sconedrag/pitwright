#!/usr/bin/env python3
"""test_identity.py — one session id, from Claude Code; v0.1 state is never adopted.

  1. Resolution: CLAUDE_CODE_SESSION_ID, else TERM_SESSION_ID, else the PPID cache (only
     when nothing else is set). Ids are sanitised by DELETING disallowed characters.
  2. `is_self` is plain equality — the ambient TERM_SESSION_ID is NOT self once a native id
     exists, because every tmux pane in a tab shares it.
  3. End to end with ONLY CLAUDE_CODE_SESSION_ID set (as on Linux, VS Code, WSL):
     SessionStart registers, an edit locks, a second session is blocked. v0.1 fails this.
  4. The tmux attack from review: a live v0.1 peer holds a file under the TERM id we share.
     We must be BLOCKED editing it, refused releasing it, shown it as a blocker, and must
     not adopt the peer's manifest.
  5. v0.1 state is never adopted, even when it names our native id (tmux panes shared one
     v0.1 manifest, so that field cannot prove ownership); the documented manual escape —
     release with CLAUDE_CODE_SESSION_ID blank — frees one's own old locks.
  6. SessionEnd releases only our own key — including after an early complete-session,
     when a tmux peer's TERM-keyed manifest is the only other one present.
  7. A session running across the upgrade (registered only under its 0.1 key) is still
     blocked by a live peer's lock, and is told to restart.

Run: python3 tests/test_identity.py   (exit 0 = pass)
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

import _identity  # noqa: E402
import coord_locks  # noqa: E402

FAILURES: list[str] = []
ID_VARS = ("CLAUDE_CODE_SESSION_ID", "TERM_SESSION_ID")
SHARED_TERM = "TERM-SHARED-BY-TMUX"
ME = "dddddddd-4444-4444-8444-444444444444"
PEER = "99999999-0000-4000-8000-000000000000"


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


class _Env:
    """Set exactly the given identity vars for the block; restore afterwards."""

    def __init__(self, **values: str):
        self.values = values
        self.saved = {k: os.environ.get(k) for k in ID_VARS}

    def __enter__(self):
        for k in ID_VARS:
            os.environ.pop(k, None)
        os.environ.update(self.values)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


def _env(**values: str) -> dict:
    """A subprocess env carrying exactly these identity vars and nothing inherited."""
    env = {k: v for k, v in os.environ.items()
           if k not in ID_VARS and k not in ("COORD_LOCKS_ADVISORY", "COORD_STALE_SECONDS")}
    env.update(values)
    return env


# ------------------------------------------------------------------- 1-2: the resolver

def test_resolution_order() -> None:
    with _Env(CLAUDE_CODE_SESSION_ID="native-1", TERM_SESSION_ID="w0t0p0:TERM-1"):
        check(_identity.session_id() == "native-1", "1. CLAUDE_CODE_SESSION_ID wins")
    with _Env(TERM_SESSION_ID="w0t0p0:TERM-2"):
        check(_identity.session_id() == "w0t0p0TERM-2",
              "1. without it, TERM_SESSION_ID is used, sanitised by deletion")


def test_ppid_cache_only_as_last_resort() -> None:
    fake_ppid = 2 ** 22 + os.getpid()   # not a real parent; only names the cache file
    cache = Path(f"/tmp/.claude-session-{fake_ppid}.id")
    try:
        cache.write_text("cached/id\n")
        with _Env():
            check(_identity.session_id(fake_ppid) == "cachedid",
                  "1. with neither var, the PPID cache is read (sanitised)")
        with _Env(TERM_SESSION_ID="TERM-3"):
            check(_identity.session_id(fake_ppid) == "TERM-3",
                  "1. ...and is ignored whenever a variable identifies the session")
    finally:
        cache.unlink(missing_ok=True)
    with _Env():
        check(_identity.session_id(fake_ppid) == "", "1. nothing at all -> empty id")


def test_is_self_is_equality() -> None:
    with _Env(CLAUDE_CODE_SESSION_ID="native-1", TERM_SESSION_ID="TERM-1"):
        check(_identity.is_self("native-1"), "2. our id is self")
        check(not _identity.is_self("TERM-1"),
              "2. the shared TERM id is NOT self once a native id exists")
        check(not _identity.is_self(""), "2. an empty owner is not self")
        check(_identity.is_self("x", "x") and not _identity.is_self("native-1", "x"),
              "2. an explicit sid is compared by equality only")


# --------------------------------------------------------------- end-to-end helpers

def _iso(age: int = 0) -> str:
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return (now - datetime.timedelta(seconds=age)).isoformat() + "Z"


def _new_repo(tmp: str, name: str, files=("a.txt",)) -> Path:
    repo = Path(tmp).resolve() / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    for f in files:
        (repo / f).write_text("x\n")
    return repo


def _coord(repo: Path) -> Path:
    return repo / ".claude" / "coordination"


def _session_start(repo: Path, payload_id: str, **ids: str) -> None:
    payload = json.dumps({"session_id": payload_id, "cwd": str(repo),
                          "hook_event_name": "SessionStart", "source": "resume"})
    subprocess.run(["bash", str(SCRIPTS / "session_start.sh")], input=payload, cwd=str(repo),
                   env=_env(**ids, CLAUDE_PROJECT_DIR=str(repo)),
                   capture_output=True, text=True, timeout=120)


def _edit(repo: Path, name: str, **ids: str) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_name": "Edit", "tool_input": {"file_path": str(repo / name)}})
    return subprocess.run([sys.executable, str(SCRIPTS / "lock_guard.py")], input=payload,
                          cwd=str(repo), env=_env(**ids, CLAUDE_PROJECT_DIR=str(repo)),
                          capture_output=True, text=True, timeout=60)


def _py(repo: Path, code: str, **ids: str) -> object:
    """Run `code` (which must print one JSON value) in the repo as the given session."""
    prog = f"import sys, json; sys.path.insert(0, {str(SCRIPTS)!r}); " + code
    out = subprocess.run([sys.executable, "-c", prog], cwd=str(repo),
                         env=_env(**ids, CLAUDE_PROJECT_DIR=str(repo)),
                         capture_output=True, text=True, timeout=60)
    try:
        return json.loads(out.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"unparsed": out.stderr[-300:]}


def _lock_owners(repo: Path) -> dict:
    locks = _coord(repo) / "locks"
    out = {}
    for f in locks.glob("*.lock") if locks.is_dir() else []:
        meta = json.loads(f.read_text())
        out[meta["file"]] = meta["sessionId"]
    return out


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _release_all_as_v01(repo: Path, term: str) -> subprocess.CompletedProcess:
    """The CHANGELOG's escape hatch: blank CLAUDE_CODE_SESSION_ID to act as the 0.1 key."""
    return subprocess.run([sys.executable, str(SCRIPTS / "coord_locks.py"), "release", "--all"],
                          cwd=str(repo),
                          env=_env(CLAUDE_CODE_SESSION_ID="", TERM_SESSION_ID=term,
                                   CLAUDE_PROJECT_DIR=str(repo)),
                          capture_output=True, text=True, timeout=60)


def _v01_manifest(repo: Path, key: str, native: str, pid: int) -> None:
    """A manifest as coord v0.1 wrote it: keyed by TERM_SESSION_ID, nativeSessionId stamped."""
    sessions = _coord(repo) / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{key}.json").write_text(json.dumps({
        "sessionId": key, "nativeSessionId": native, "pid": pid, "host": coord_locks.HOST,
        "domain": "docs", "lastHeartbeat": _iso(), "lockedFiles": []}))


def _manifests(repo: Path) -> list[str]:
    return sorted(p.stem for p in (_coord(repo) / "sessions").glob("*.json"))


# --------------------------------------------------------------- 3: native-only sessions

def test_native_only_session_is_coordinated() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = _new_repo(tmp, "native")
        a, b = "aaaaaaaa-1111-4111-8111-111111111111", "bbbbbbbb-2222-4222-8222-222222222222"
        _session_start(repo, a, CLAUDE_CODE_SESSION_ID=a)
        _session_start(repo, b, CLAUDE_CODE_SESSION_ID=b)
        check(_manifests(repo) == sorted([a, b]),
              f"3. SessionStart registers each session under its Claude Code id ({_manifests(repo)})")
        r = _edit(repo, "a.txt", CLAUDE_CODE_SESSION_ID=a)
        check(r.returncode == 0 and _lock_owners(repo) == {"a.txt": a},
              "3. the first edit takes the lock")
        r = _edit(repo, "a.txt", CLAUDE_CODE_SESSION_ID=b)
        check(r.returncode == 2 and "FILE LOCKED" in r.stderr,
              "3. a second session is blocked — with TERM_SESSION_ID unset throughout")


# --------------------------------------------------------------- 4: the tmux attack

def test_tmux_peer_sharing_term_id_stays_a_peer() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = _new_repo(tmp, "tmux", files=("peer.txt",))
        # A LIVE v0.1 peer in another pane: our TERM id, its own native id, a live pid.
        _v01_manifest(repo, SHARED_TERM, PEER, os.getpid())
        r = _edit(repo, "peer.txt", TERM_SESSION_ID=SHARED_TERM)
        check(r.returncode == 0 and _lock_owners(repo) == {"peer.txt": SHARED_TERM},
              "4. setup: the v0.1 peer holds peer.txt under the shared TERM id")

        me = {"CLAUDE_CODE_SESSION_ID": ME, "TERM_SESSION_ID": SHARED_TERM}
        _session_start(repo, ME, **me)
        check(SHARED_TERM in _manifests(repo),
              "4. SessionStart did NOT adopt the peer's manifest (its nativeSessionId is not ours)")
        r = _edit(repo, "peer.txt", **me)
        check(r.returncode == 2, "4. editing the peer's file is BLOCKED")
        rel = _py(repo, "import coord_locks; print(json.dumps(coord_locks.release('peer.txt')))",
                  **me)
        check(isinstance(rel, list) and rel[0] is False,
              f"4. releasing the peer's lock is REFUSED ({rel})")
        check(_lock_owners(repo) == {"peer.txt": SHARED_TERM}, "4. the peer still holds it")
        who = _py(repo, "import blocker_owner; "
                        "print(json.dumps(blocker_owner.owner_of_path('peer.txt')))", **me)
        check(isinstance(who, dict) and who.get("sessionId") == SHARED_TERM,
              f"4. blocker_owner names the peer as the blocker ({who})")
        out = _release_all_as_v01(repo, SHARED_TERM)
        check(out.returncode == 1 and "refusing" in out.stderr
              and _lock_owners(repo) == {"peer.txt": SHARED_TERM},
              "4. the 0.1 escape hatch REFUSES while the key's process is alive (tmux peer)")


# --------------------------------------------------------------- 5: v0.1 state is left alone

def test_v01_state_is_never_adopted() -> None:
    """Even when the v0.1 manifest names OUR native id. Under v0.1 every tmux pane wrote one
    shared manifest, so `nativeSessionId` only names the pane that started last — a second
    review reproduced "proven" adoption taking a live pane's locks. The explicit escape
    hatch the CHANGELOG documents (blank CLAUDE_CODE_SESSION_ID to act as the v0.1 key)
    must still free your own old locks."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _new_repo(tmp, "noadopt", files=("a.txt",))
        old_term = "TERM-OLD"
        # Our own 0.1 session, since restarted: its recorded process is gone, its
        # heartbeat is recent, so liveness is "unknown" — treated as alive by everyone.
        _v01_manifest(repo, old_term, ME, _dead_pid())
        _edit(repo, "a.txt", TERM_SESSION_ID=old_term)
        check(_lock_owners(repo) == {"a.txt": old_term}, "5. setup: a v0.1 lock under TERM-OLD")

        me = {"CLAUDE_CODE_SESSION_ID": ME, "TERM_SESSION_ID": old_term}
        _session_start(repo, ME, **me)
        check(sorted(_manifests(repo)) == sorted([ME, old_term]),
              f"5. SessionStart registered the native id and left the v0.1 manifest ({_manifests(repo)})")
        check(_lock_owners(repo) == {"a.txt": old_term}, "5. the v0.1 lock was not re-keyed")
        r = _edit(repo, "a.txt", **me)
        check(r.returncode == 2,
              "5. until it is stale, the v0.1 lock blocks us like any peer's (liveness unknown)")

        out = _release_all_as_v01(repo, old_term)
        check(out.returncode == 0 and _lock_owners(repo) == {},
              f"5. escape hatch: release --all with CLAUDE_CODE_SESSION_ID blank frees it ({out.stdout.strip()})")
        r = _edit(repo, "a.txt", **me)
        check(r.returncode == 0 and _lock_owners(repo) == {"a.txt": ME},
              "5. ...after which our edit takes the lock under the native id")


# --------------------------------------------------------------- 6: SessionEnd

def _session_end(repo: Path, **ids: str) -> None:
    subprocess.run(["bash", str(SCRIPTS / "session_end_cleanup.sh")], input="{}",
                   cwd=str(repo), env=_env(**ids, CLAUDE_PROJECT_DIR=str(repo)),
                   capture_output=True, text=True, timeout=60)


def test_session_end_releases_only_our_key() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        repo = _new_repo(tmp, "end", files=("mine.txt", "peer.txt"))
        _v01_manifest(repo, SHARED_TERM, PEER, os.getpid())
        _edit(repo, "peer.txt", TERM_SESSION_ID=SHARED_TERM)

        me = {"CLAUDE_CODE_SESSION_ID": ME, "TERM_SESSION_ID": SHARED_TERM}
        _session_start(repo, ME, **me)
        _edit(repo, "mine.txt", **me)
        check(_lock_owners(repo) == {"mine.txt": ME, "peer.txt": SHARED_TERM},
              "6. setup: one lock each")

        _session_end(repo, **me)
        check(_lock_owners(repo) == {"peer.txt": SHARED_TERM},
              f"6. SessionEnd released mine, not the tmux peer's ({_lock_owners(repo)})")
        check(SHARED_TERM in _manifests(repo), "6. ...and left the peer's manifest")

        # Early /coord:complete-session already archived ours; SessionEnd must not then
        # fall through to the only manifest left, which is the peer's.
        _session_end(repo, **me)
        check(_lock_owners(repo) == {"peer.txt": SHARED_TERM} and SHARED_TERM in _manifests(repo),
              "6. a second SessionEnd (our manifest gone) still leaves the peer alone")


# --------------------------------------------------------------- 7: running across the upgrade

def test_unregistered_session_respects_peer_locks() -> None:
    """A session started under 0.1 and now running 0.2 code resolves its native id, under
    which it has no manifest. It must still be blocked by a live peer's lock, and be told
    to restart, rather than edit silently."""
    with tempfile.TemporaryDirectory() as tmp:
        repo = _new_repo(tmp, "across", files=("peer.txt", "free.txt"))
        peer = {"CLAUDE_CODE_SESSION_ID": PEER}
        _session_start(repo, PEER, **peer)
        _edit(repo, "peer.txt", **peer)
        _v01_manifest(repo, "TERM-MINE", ME, os.getpid())

        across = {"CLAUDE_CODE_SESSION_ID": ME, "TERM_SESSION_ID": "TERM-MINE"}
        r = _edit(repo, "peer.txt", **across)
        check(r.returncode == 2 and "FILE LOCKED" in r.stderr,
              "7. an unregistered session is still BLOCKED by a live peer's lock")
        r = _edit(repo, "free.txt", **across)
        check(r.returncode == 0 and "Restart or resume" in r.stderr
              and "free.txt" not in _lock_owners(repo),
              "7. an unlocked file is allowed, no lock taken, with a restart notice")
        r = _edit(repo, "free.txt", CLAUDE_CODE_SESSION_ID="cccccccc-0000-4000-8000-000000000000")
        check(r.returncode == 0 and r.stderr == "",
              "7. a plain unregistered session (no 0.1 manifest) gets no notice")


def main() -> int:
    tests = [(n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)]
    print(f"test_identity.py — {len(tests)} tests")
    for _, fn in tests:
        fn()
    print(f"\n{'FAILED: ' + ', '.join(FAILURES) if FAILURES else 'ALL PASS'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
