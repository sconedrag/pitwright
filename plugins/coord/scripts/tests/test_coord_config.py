#!/usr/bin/env python3
"""
test_coord_config.py — project-root resolution + the merged settings precedence.

coord_config.py is the one place that resolves "what project is this invocation for" and
"what does setting X currently resolve to". Both answers depend on environment the caller
controls ($CLAUDE_PROJECT_DIR, a cwd, an env var override) and on an optional
.claude/coord.json in that project — this file is a plain-script test in the same style as
the rest of this suite (no pytest), run directly or via run_all.sh.

Covers:
  [1] DEFAULTS      — every documented default, unconfigured.
  [2] FILE OVERRIDE — .claude/coord.json beats the built-in default.
  [3] ENV OVERRIDE  — an env var beats coord.json (and the default).
  [4] MALFORMED     — bad JSON / wrong shape -> defaults, never a crash.
  [5] PROJECT ROOT  — $CLAUDE_PROJECT_DIR honoured; a subdirectory resolves to the git
                      toplevel; a non-git directory falls back to itself.
  [6] EMPTY LISTS   — churn_globs / additive_files / closeout_buckets default to [].

Run: python3 scripts/tests/test_coord_config.py   (exit 0 = pass)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import coord_config as cc  # noqa: E402

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


class _EnvScope:
    """Save/restore a set of env vars, deleting any that were unset on entry."""

    def __init__(self, **values: str) -> None:
        self._values = values
        self._saved: dict[str, str | None] = {}

    def __enter__(self):
        for k, v in self._values.items():
            self._saved[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *exc) -> None:
        for k, prev in self._saved.items():
            if prev is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = prev


def test_defaults() -> None:
    print("\n[1] defaults, unconfigured")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "proj"
        _init_repo(root)
        expected = {
            "stale_seconds": 86400,
            "idle_badge_seconds": 3600,
            "representation_stale_seconds": 21600,
            "registry_cap": 200,
            "closeout_stale_branch_days": 7,
            "memory_hot_budget_bytes": 20000,
            "memory_dir": None,
            "churn_globs": [],
            "additive_files": [],
            "closeout_buckets": [],
        }
        for key, want in expected.items():
            got = cc.get(key, root=root)
            check(got == want, f"{key} default is {want!r} (got {got!r})")


def test_file_override() -> None:
    print("\n[2] .claude/coord.json overrides the built-in default")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "proj"
        _init_repo(root)
        (root / ".claude").mkdir()
        (root / ".claude" / "coord.json").write_text(json.dumps({
            "stale_seconds": 111,
            "churn_globs": ["^build/"],
        }))
        check(cc.get("stale_seconds", root=root) == 111, "scalar override from coord.json")
        check(cc.get("churn_globs", root=root) == ["^build/"], "list override from coord.json")
        # An unconfigured key in the same file is untouched.
        check(cc.get("registry_cap", root=root) == 200, "unconfigured key keeps its default")


def test_env_beats_file() -> None:
    print("\n[3] env var overrides coord.json (and the default)")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "proj"
        _init_repo(root)
        (root / ".claude").mkdir()
        (root / ".claude" / "coord.json").write_text(json.dumps({"stale_seconds": 111}))
        with _EnvScope(COORD_STALE_SECONDS="222"):
            check(cc.get("stale_seconds", root=root) == 222,
                  "env wins over both coord.json and the default")
        # Restored: back to the file's value with the env var gone.
        check(cc.get("stale_seconds", root=root) == 111,
              "env override does not leak once unset")


def test_malformed_file_degrades_to_defaults() -> None:
    print("\n[4] malformed coord.json -> defaults, no crash")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "proj"
        _init_repo(root)
        (root / ".claude").mkdir()
        (root / ".claude" / "coord.json").write_text("{not valid json")
        try:
            got = cc.get("stale_seconds", root=root)
            ok = True
        except Exception as exc:  # noqa: BLE001
            got, ok = None, False
        check(ok, "malformed JSON does not raise")
        check(got == 86400, f"malformed JSON falls back to the default (got {got!r})")

    with tempfile.TemporaryDirectory() as td2:
        root2 = Path(td2) / "proj2"
        _init_repo(root2)
        (root2 / ".claude").mkdir()
        (root2 / ".claude" / "coord.json").write_text(json.dumps(["not", "an", "object"]))
        check(cc.get("stale_seconds", root=root2) == 86400,
              "a JSON array (not an object) also falls back to defaults")


def test_project_root_honours_claude_project_dir() -> None:
    print("\n[5a] project_root() honours $CLAUDE_PROJECT_DIR")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "proj"
        _init_repo(root)
        other_cwd = Path(td)  # NOT the repo — proves cwd alone is not what's being read
        with _EnvScope(CLAUDE_PROJECT_DIR=str(root)):
            prev = os.getcwd()
            os.chdir(other_cwd)
            try:
                got = cc.project_root()
            finally:
                os.chdir(prev)
        check(got.resolve() == root.resolve(),
              f"project_root() followed $CLAUDE_PROJECT_DIR, not cwd (got {got})")


def test_project_root_resolves_subdir_to_git_toplevel() -> None:
    print("\n[5b] a subdirectory resolves to the git toplevel")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "proj"
        _init_repo(root)
        sub = root / "a" / "b" / "c"
        sub.mkdir(parents=True)
        with _EnvScope(CLAUDE_PROJECT_DIR=str(sub)):
            got = cc.project_root()
        check(got.resolve() == root.resolve(),
              f"project_root() climbed from a nested dir to the toplevel (got {got})")


def test_project_root_falls_back_to_base_outside_a_repo() -> None:
    print("\n[5c] a non-git directory falls back to itself")
    with tempfile.TemporaryDirectory() as td:
        bare = Path(td) / "not-a-repo"
        bare.mkdir()
        with _EnvScope(CLAUDE_PROJECT_DIR=str(bare)):
            got = cc.project_root()
        check(got.resolve() == bare.resolve(),
              f"project_root() fell back to the base dir, not some ancestor repo (got {got})")


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    print(f"test_coord_config.py — {len(tests)} tests")
    for _, fn in tests:
        fn()
    print(f"\n{'FAILED: ' + str(len(FAILURES)) if FAILURES else 'ALL PASS'}")
    for f in FAILURES:
        print(f"  - {f}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
