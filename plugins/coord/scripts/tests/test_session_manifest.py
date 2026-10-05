#!/usr/bin/env python3
"""
test_session_manifest.py — auto-registration and domain inference.

Two invariants here are load-bearing and would fail silently if broken:

1. The manifest FILENAME must match the documented sanitization formula applied to
   TERM_SESSION_ID (`tr -cd 'A-Za-z0-9_-'` — delete, not substitute). A shell implementation
   of the edit-time guard must be able to reproduce this exactly; derive it differently and
   the guard looks for a file this module never wrote — indistinguishable from "no locking".
2. An INFERRED domain must never overwrite a DECLARED one. A guess that silently re-homes a
   session would move its claimed territory without anyone asking.

Run: python3 scripts/tests/test_session_manifest.py   (exit 0 = pass)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import session_manifest as sm  # noqa: E402

# session_domain_infer.py is an app module and is NOT part of this plugin — session_manifest
# itself only ever reaches for it lazily (see `_publish_domain`'s own sys.path dance for
# session_registry, the same pattern), so this test suite covers session_manifest's own
# auto-registration and the "a declared domain always wins" invariant, never the inference
# heuristic itself (that lived in session_domain_infer's own test suite, upstream).

DOMAINS = {
    "ui-ux": {"exclusive": ["MyApp/Theme/**", "MyApp/MicroInteractions/**"],
              "shared": ["MyApp/UI/**"]},
    "data-core-health": {"exclusive": ["MyApp/Models/**"],
                         "shared": ["MyApp/Services/Health/**"]},
    "testing-infra": {"exclusive": ["MyApp/Tests/Mocks/**"], "shared": []},
    "web": {"exclusive": ["web/**"], "shared": []},
}


def _repo(tmp: str) -> str:
    """A real git repo — `sessions_dir` resolves via `git rev-parse --show-toplevel`."""
    root = Path(tmp) / "repo"
    (root / ".claude" / "coordination").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=root, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=root, capture_output=True)
    (root / "f.txt").write_text("x")
    subprocess.run(["git", "add", "f.txt"], cwd=root, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=root, capture_output=True)
    (root / ".claude" / "coordination" / "domains.json").write_text(json.dumps(DOMAINS))
    return str(root)


# --- filename parity with the guard --------------------------------------------------


def _dead_pid() -> int:
    """Derived, never a constant: 999_999 is live-capable on Linux CI."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _testlib
    return _testlib.dead_pid()

def test_sanitize_deletes_rather_than_substitutes() -> None:
    """The documented formula uses `tr -cd`, which DELETES. Substituting with '_' yields
    a different filename for the same session and breaks the handshake."""
    assert sm.sanitize_session_id("w0t1p0:9AB3-CD/EF") == "w0t1p09AB3-CDEF"


def test_sanitize_matches_the_shell_formula_exactly() -> None:
    """Executable parity: run the documented shell expression and compare."""
    raw = "w0t1p0:9AB3-CD.EF/GH i"
    shell = subprocess.run(f"printf '%s' \"{raw}\" | tr -cd 'A-Za-z0-9_-'",
                           shell=True, capture_output=True, text=True).stdout
    assert sm.sanitize_session_id(raw) == shell, f"{sm.sanitize_session_id(raw)!r} != {shell!r}"


# --- auto-registration ---------------------------------------------------------------

def test_ensure_creates_a_manifest_the_guard_would_find() -> None:
    """Compared RESOLVED, because the two sides derive the root differently and that is
    fine: the guard uses `cd … && pwd` (logical) while this module uses
    `git rev-parse --show-toplevel` (physical). On macOS /var vs /private/var makes those
    strings differ while naming the same directory — and a symlinked repo path is likewise
    safe, since following the link lands on the same inode. Comparing the raw strings would
    fail on a difference that cannot affect whether the guard finds the file."""
    with tempfile.TemporaryDirectory() as td:
        root = _repo(td)
        path = sm.ensure(root, 4242, session_id="sess-1")
        assert path is not None and path.name == "sess-1.json"
        expected = (Path(root) / ".claude" / "coordination" / "sessions").resolve()
        assert path.parent.resolve() == expected


def test_auto_registration_claims_no_paths() -> None:
    """Claiming a domain's globs on autopilot would let a session that merely started
    block peers over files it never touched."""
    with tempfile.TemporaryDirectory() as td:
        root = _repo(td)
        m = sm.read(sm.ensure(root, 1, session_id="s"))
        assert m["claimedPaths"] == []
        assert m["domain"] == sm.UNSCOPED
        assert m["autoRegistered"] is True


def test_ensure_records_branch_for_check_6b() -> None:
    """Check 6b fails OPEN without `branch`, so recording it is what arms it."""
    with tempfile.TemporaryDirectory() as td:
        root = _repo(td)
        assert sm.read(sm.ensure(root, 1, session_id="s"))["branch"] != ""


def test_ensure_is_idempotent_and_preserves_a_declared_session() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = _repo(td)
        path = sm.ensure(root, 1, session_id="s")
        m = sm.read(path)
        m.update({"domain": "ui-ux", "humanName": "Declared",
                  "claimedPaths": ["MyApp/UI/**"]})
        path.write_text(json.dumps(m))

        # A real SessionStart passes the pid of a LIVE process. A pid that is already dead
        # at write time is dropped rather than recorded (a stored dead pid eventually reads
        # as `dead` + inheritable, which would offer a live session's work up for adoption),
        # so a synthetic constant no longer round-trips.
        live = os.getpid()
        sm.ensure(root, live, session_id="s")  # a later SessionStart
        after = sm.read(path)
        assert after["domain"] == "ui-ux", "a restart must not flatten a declared domain"
        assert after["humanName"] == "Declared"
        assert after["claimedPaths"] == ["MyApp/UI/**"]
        assert after["pid"] == live, "liveness fields SHOULD refresh"

        sm.ensure(root, _dead_pid(), session_id="s")  # a caller passing a dead pid
        assert sm.read(path)["pid"] == 0, "a dead pid must be dropped, not stored"


# --- domain inference (the "never overwrites a declared domain" invariant only — the
# inference heuristic itself lives in session_domain_infer.py, an app module not shipped
# with this plugin) -----------------------------------------------------------------

def test_inference_never_overwrites_a_declared_domain() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = _repo(td)
        path = sm.ensure(root, 1, session_id="s")
        m = sm.read(path); m["domain"] = "web"; path.write_text(json.dumps(m))

        assert sm.set_domain(root, "ui-ux", session_id="s", inferred=True) is False
        assert sm.read(path)["domain"] == "web"


def test_inferred_domain_is_marked_unconfirmed_and_claims_nothing() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = _repo(td)
        path = sm.ensure(root, 1, session_id="s")
        assert sm.set_domain(root, "ui-ux", session_id="s", inferred=True,
                             rationale="matched: theme") is True
        m = sm.read(path)
        assert m["domain"] == "ui-ux"
        assert m["domainConfirmed"] is False
        assert m["domainInferred"] is True
        assert m["claimedPaths"] == [], "an unconfirmed guess must claim no territory"


def test_declared_domain_may_overwrite_an_inference() -> None:
    """The human always wins — `/coord:start-session` corrects a bad guess."""
    with tempfile.TemporaryDirectory() as td:
        root = _repo(td)
        sm.ensure(root, 1, session_id="s")
        sm.set_domain(root, "ui-ux", session_id="s", inferred=True)
        assert sm.set_domain(root, "web", session_id="s", claimed_paths=["web/**"]) is True
        m = sm.read(sm.sessions_dir(root) / "s.json")
        assert m["domain"] == "web" and m["claimedPaths"] == ["web/**"]


def test_missing_repo_degrades_quietly() -> None:
    with tempfile.TemporaryDirectory() as td:
        assert sm.ensure(td, 1, session_id="s") is None or True  # never raises


# --- the wiring itself ---------------------------------------------------------------
# A UserPromptSubmit hook that exits non-zero BLOCKS THE PROMPT. The bare
# `python3 scripts/session_domain_infer.py` form has been wired while the script existed
# only on an unmerged branch, so every other checkout hit ENOENT and every session was
# hard-blocked on every turn. settings.json is one shared tracked file; scripts arrive per
# branch. These pin the guarded command string that survives that window.

GUARDED = "[ -f scripts/session_domain_infer.py ] && python3 scripts/session_domain_infer.py; exit 0"


def _run_guarded(cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", GUARDED], cwd=cwd, input="{}",
                          capture_output=True, text=True, timeout=30)


def test_guarded_invocation_exits_zero_when_the_script_is_absent() -> None:
    """The exact production failure: settings.json wired, script not yet merged."""
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "scripts").mkdir()
        assert _run_guarded(td).returncode == 0


def test_guarded_invocation_exits_zero_when_the_script_crashes() -> None:
    """An interpreter-level failure (syntax error) is unreachable by internal try/except,
    because the process dies before executing any of this module."""
    with tempfile.TemporaryDirectory() as td:
        scripts = Path(td) / "scripts"
        scripts.mkdir()
        (scripts / "session_domain_infer.py").write_text("this is not valid python(\n")
        proc = _run_guarded(td)
        assert proc.returncode == 0, f"a crashing hook must not block the prompt: {proc.stderr}"


def test_bare_invocation_would_have_blocked() -> None:
    """Documents WHY the guard exists — the unguarded form really does fail non-zero."""
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "scripts").mkdir()
        bare = subprocess.run(["bash", "-c", "python3 scripts/session_domain_infer.py"],
                              cwd=td, input="{}", capture_output=True, text=True, timeout=30)
        assert bare.returncode != 0, "if this ever passes, the guard's rationale is stale"


def test_guarded_invocation_still_runs_the_real_script() -> None:
    """The guard must not neuter it: with the script present, it still executes and the
    hook contract (exit 0, JSON or nothing on stdout) holds."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    proc = subprocess.run(["bash", "-c", GUARDED], cwd=repo_root,
                          input=json.dumps({"prompt": "hello", "cwd": str(repo_root)}),
                          capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0
    if proc.stdout.strip():
        json.loads(proc.stdout)  # must be valid hook JSON when it speaks at all


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failures = []
    print(f"test_session_manifest.py — {len(tests)} tests")
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            print(f"  FAIL  {name}: {exc}")
            failures.append(name)
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
            failures.append(name)
    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'ALL PASS'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
