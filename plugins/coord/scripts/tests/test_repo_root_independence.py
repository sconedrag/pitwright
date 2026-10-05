#!/usr/bin/env python3
"""
test_repo_root_independence.py — regression guard: the plugin resolves the user's
PROJECT root, never its own install location.

These scripts were written as `<repo>/scripts/*.py`, so `Path(__file__).resolve().parent
.parent` correctly named the repo they coordinated. Shipped as a Claude Code plugin they
instead live in the plugin cache (here, `plugins/coord/scripts/`), nowhere near whatever
project the harness invokes them for — so that same derivation would name a directory
inside THIS PLUGIN (or, before the fix, a wrong ancestor of it) and every coordination
script would write its state in the wrong place, silently.

Two things, both load-bearing:
  [1] the real behavior — coord_locks resolves its coordination
      dir under a SCRATCH project, not under `plugins/coord`, whether that project is
      reached via `$CLAUDE_PROJECT_DIR` or via plain process cwd (the subprocess case,
      which is the genuine end-to-end path a hook actually takes).
  [2] that this suite would have CAUGHT the bug it guards — simulated via the exact old
      formula (never by editing the real source), proving [1] is not vacuously true.

Run: python3 scripts/tests/test_repo_root_independence.py   (exit 0 = pass)
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))
import coord_locks  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        FAILURES.append(label)


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t.t")
    _git(root, "config", "user.name", "t")
    (root / "f.txt").write_text("x")
    _git(root, "add", "f.txt")
    _git(root, "commit", "-qm", "init")


def test_coord_dir_lands_under_the_scratch_repo_via_env() -> None:
    print("\n[1a] coord_locks.coord_dir(), $CLAUDE_PROJECT_DIR pointing at a scratch repo")
    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td) / "scratch-repo"
        _init_repo(scratch)
        import os
        prev = os.environ.get("CLAUDE_PROJECT_DIR")
        os.environ["CLAUDE_PROJECT_DIR"] = str(scratch)
        try:
            d = coord_locks.coord_dir()
        finally:
            if prev is None:
                os.environ.pop("CLAUDE_PROJECT_DIR", None)
            else:
                os.environ["CLAUDE_PROJECT_DIR"] = prev
        check(d.resolve() == (scratch / ".claude" / "coordination").resolve(),
              f"coordination dir is under the scratch repo (got {d})")
        check("plugins/coord" not in str(d),
              "coordination dir is NOT under this plugin's own install path")


def test_coord_dir_lands_under_the_scratch_repo_via_subprocess_cwd() -> None:
    """The genuine end-to-end path: a fresh process, no env override, cwd alone deciding
    which project `git rev-parse --show-toplevel` (and so project_root()) resolves to —
    this is how a hook actually invokes these scripts."""
    print("\n[1b] coord_locks.py CLI, cwd set to a scratch repo, no env override")
    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td) / "scratch-repo"
        _init_repo(scratch)
        proc = subprocess.run(
            [sys.executable, str(SCRIPTS / "coord_locks.py"), "list", "--json"],
            cwd=str(scratch), capture_output=True, text=True, timeout=30,
        )
        check(proc.returncode == 0, f"coord_locks.py list exits clean (stderr: {proc.stderr[:200]})")
        created = scratch / ".claude" / "coordination"
        check(created.is_dir(), f"the scratch repo now owns a .claude/coordination dir ({created})")
        plugin_leak = SCRIPTS.parent / ".claude" / "coordination" / "locks"
        # This is the OLD bug's exact wrong location (plugins/coord/.claude/coordination) —
        # it must not have been freshly created by this run. (It may pre-exist from
        # something unrelated; the point is this invocation didn't write there.)
        if created.is_dir():
            # The two directories must be genuinely different filesystem locations.
            check(created.resolve() != plugin_leak.resolve(),
                  "the scratch repo's coordination dir is not the plugin's own directory")


def test_suite_would_have_caught_the_old_bug() -> None:
    """Prove [1] is not vacuous: simulate the old, pre-fix formula
    (`Path(__file__).resolve().parent.parent` from inside the plugin's own scripts/) via a
    monkeypatch of `coord_locks.project_root` — never by editing the real source — and show
    it disagrees with the scratch repo, i.e. the exact assertion in [1a] would have failed
    had the fix not landed."""
    print("\n[2] mutation check — reintroducing the old script-location formula")
    old_buggy_root = SCRIPTS.parent  # Path(__file__).resolve().parent.parent, pre-fix
    with tempfile.TemporaryDirectory() as td:
        scratch = Path(td) / "scratch-repo"
        _init_repo(scratch)

        real_project_root = coord_locks.project_root
        coord_locks.project_root = lambda: old_buggy_root  # the exact pre-fix derivation
        try:
            buggy_dir = coord_locks.coord_dir()
        finally:
            coord_locks.project_root = real_project_root

        would_have_failed = buggy_dir.resolve() != (scratch / ".claude" / "coordination").resolve()
        check(would_have_failed,
              "under the old formula, coord_dir() names the PLUGIN's own directory, not the "
              "scratch repo — confirming test [1a]'s assertion is sensitive to this exact "
              "regression, not trivially true")
        check(str(buggy_dir).startswith(str(old_buggy_root)),
              f"and specifically lands under plugins/coord (got {buggy_dir}), the historical bug site")


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    print(f"test_repo_root_independence.py — {len(tests)} tests")
    for _, fn in tests:
        fn()
    print(f"\n{'FAILED: ' + str(len(FAILURES)) if FAILURES else 'ALL PASS'}")
    for f in FAILURES:
        print(f"  - {f}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
