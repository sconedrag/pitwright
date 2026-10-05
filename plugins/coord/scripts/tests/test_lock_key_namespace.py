#!/usr/bin/env python3
"""
test_lock_key_namespace.py — every lock writer must use the CANONICAL key.

A coordination lock is addressed by `sha256("<worktree>:<repo-relative-path>")[:16]`
(`coord_locks.lock_key`). A writer that derives its own key — typically by hashing the bare
path instead of the worktree-scoped one — puts the lock in a namespace no reader looks in. The
result is not a noisy failure but a silent one:

  - the edit-time guard (`lock_guard.py`) computes the canonical worktree-scoped key, so a
    lock filed under the wrong key blocks NOBODY;
  - `coord_locks release` computes the same key, reports "not locked", and leaves the file;
  - `coord_locks list` reads the directory, so it still shows the lock as HELD.

A release that can neither succeed nor fail, on a lock that never protected anything. This
has happened more than once in scripts that reimplement the key formula instead of calling
`coord_locks.lock_key()`/`lock_path()` directly — which is why it is worth a regression test
rather than a comment.

Run: python3 scripts/tests/test_lock_key_namespace.py   (exit 0 = pass)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import coord_locks  # noqa: E402

# Deriving a lock key by hashing something that is NOT worktree-scoped.
BARE_PATH_HASH = re.compile(
    r"sha256\(\s*[A-Za-z_][A-Za-z0-9_]*(?:PATH|path|Path)\w*\.encode\(\)\s*\)"
    r"|echo\s+-n\s+\"\$REL_PATH\"\s*\|\s*shasum"
)

# Empty on purpose, and it should stay that way.
#
# An entry here is a file allowed to hold the bug this test exists to prevent. Add one only
# with a reason and a plan to remove it; a permanent exception is how a guard becomes
# decorative.
KNOWN_SUPERSEDED: set[str] = set()

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


def test_no_reachable_writer_derives_its_own_key() -> None:
    """Class-level guard: nothing under scripts/ hashes a bare path for a lock."""
    offenders = []
    scanned = 0
    for p in sorted(SCRIPTS.rglob("*.py")) + sorted(SCRIPTS.rglob("*.sh")):
        rel = str(p.relative_to(ROOT))
        # `coord_locks`/`_coord_lock` DEFINE the canonical formula, and this file carries the
        # old one as a probe string to prove the detector still fires. Without this exclusion
        # the test flags itself — wrong, and the fastest way to teach everyone to ignore it.
        # Same carve-out `audit_coordination_wiring.py` makes for `audit_*` files.
        if (rel in KNOWN_SUPERSEDED
                or p.name in ("coord_locks.py", "_coord_lock.py")
                or p.resolve() == Path(__file__).resolve()):
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        scanned += 1
        for m in BARE_PATH_HASH.finditer(text):
            line = text[:m.start()].count("\n") + 1
            # A mention inside a comment is documentation of the bug, not the bug.
            src_line = text.splitlines()[line - 1].strip()
            if src_line.startswith("#") or src_line.startswith("//"):
                continue
            offenders.append(f"{rel}:{line}  {src_line[:70]}")

    check(scanned > 20, f"scanned {scanned} scripts — a tiny scan would pass vacuously")
    if offenders:
        print("\n  Writers deriving their own lock key:")
        for o in offenders:
            print(f"    {o}")
        print("\n  Use coord_locks.lock_path() / lock_key() instead.\n")
    check(not offenders, "no reachable script derives a lock key from a bare path")


def test_the_detector_catches_the_old_formula() -> None:
    """A guard that cannot fail is not a guard."""
    probe = 'h = hashlib.sha256(PBXPROJ_RELATIVE_PATH.encode()).hexdigest()[:16]'
    check(BARE_PATH_HASH.search(probe) is not None,
          "the old bare-path derivation is detected")


def test_canonical_key_is_worktree_scoped() -> None:
    """Two worktrees must produce different keys for the same path — the property the
    whole scheme rests on."""
    a = coord_locks.lock_key("MyApp/Foo.swift", wt="main")
    b = coord_locks.lock_key("MyApp/Foo.swift", wt="feature-x")
    check(a != b, "same path in different worktrees yields different keys")


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    print(f"test_lock_key_namespace.py — {len(tests)} tests")
    for _, fn in tests:
        fn()
    print(f"\n{'FAILED: ' + ', '.join(FAILURES) if FAILURES else 'ALL PASS'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
