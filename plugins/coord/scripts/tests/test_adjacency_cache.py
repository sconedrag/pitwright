#!/usr/bin/env python3
"""
test_adjacency_cache.py — the edit-time cross-worktree collision warning.

The distinction this file exists to protect: a FAILED or MISSING cache must read as
"unknown", never as "no collisions". Silently reporting nothing when the check did not run
is the failure mode that makes a gate worse than useless — it manufactures confidence.
That is the same class of bug as a hook silently drifting out of sync with what it guards,
so it gets a test rather than a comment.

Run: python3 scripts/tests/test_adjacency_cache.py   (exit 0 = pass)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import adjacency_cache as ac  # noqa: E402


def _seed(tmp: Path, collisions: dict, *, ok: bool = True, age: float = 0.0) -> None:
    """Point the module at a synthetic cache so tests never pay the real 20s run.

    Also pins `ac.ROOT` to the current process cwd: `_rel()` resolves a bare relative
    path (as these synthetic collision keys are) against `Path(path).resolve()`, which
    uses the process cwd — so ROOT must agree with that cwd for the round trip to land
    back on the same relative string. Production code derives ROOT from the project the
    harness hands it (coord_config.project_root()); here there is no real project, so the
    test supplies the one value that makes the arithmetic self-consistent, the same way
    it already overrides ac.CACHE instead of writing to the real one.
    """
    ac.ROOT = Path.cwd()
    ac.CACHE = tmp / "adjacency-cache.json"
    ac.CACHE.parent.mkdir(parents=True, exist_ok=True)
    ac.CACHE.write_text(json.dumps({
        "computedAt": time.time() - age,
        "branch": "mine",
        "head": "abc123def",
        "collisions": collisions,
        "ok": ok,
    }))


ONE = {"MyApp/Tools/Core/PlannerFrontierSelector.swift":
       [{"branch": "agentic/router-tool-expansion", "special": False}]}


def test_absent_cache_says_nothing() -> None:
    ac.CACHE = Path("/nonexistent/never/adjacency-cache.json")
    assert ac.check("MyApp/Foo.swift") == ""


def test_failed_refresh_is_unknown_not_clean(tmp_factory=None) -> None:
    """ok:false must NOT be reported as 'no collisions'. A check that did not run and a
    check that found nothing look identical to the reader unless we refuse to speak."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        _seed(Path(td), ONE, ok=False)
        assert ac.check("MyApp/Tools/Core/PlannerFrontierSelector.swift") == ""
        assert "unknown" in ac.status().lower()


def test_uncontended_path_says_nothing() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        _seed(Path(td), ONE)
        assert ac.check("MyApp/Somewhere/Else.swift") == ""


def test_contended_path_warns_and_names_the_branch() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        _seed(Path(td), ONE)
        msg = ac.check("MyApp/Tools/Core/PlannerFrontierSelector.swift")
        assert "agentic/router-tool-expansion" in msg
        assert "merge" in msg.lower(), "must say WHERE it bites, since nothing blocks here"


def test_warning_states_when_it_was_computed() -> None:
    """A cached answer presented as live invites trust it has not earned."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        _seed(Path(td), ONE, age=45 * 60)
        msg = ac.check("MyApp/Tools/Core/PlannerFrontierSelector.swift")
        assert "45 min ago" in msg and "abc123def" in msg


def test_stale_cache_is_labelled_stale() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        _seed(Path(td), ONE, age=ac.MAX_AGE_SECONDS + 60)
        msg = ac.check("MyApp/Tools/Core/PlannerFrontierSelector.swift")
        assert "STALE" in msg


def test_fresh_cache_is_not_labelled_stale() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        _seed(Path(td), ONE, age=60)
        assert "STALE" not in ac.check("MyApp/Tools/Core/PlannerFrontierSelector.swift")


def test_high_contention_file_is_called_out() -> None:
    """project.pbxproj collides constantly; 'expect a conflict' is the useful warning."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        _seed(Path(td), {"MyApp.xcodeproj/project.pbxproj":
                         [{"branch": "other", "special": True}]})
        msg = ac.check("MyApp.xcodeproj/project.pbxproj")
        assert "conflict" in msg.lower()


def test_multiple_peers_are_all_named() -> None:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        _seed(Path(td), {"f.swift": [{"branch": "b1", "special": False},
                                     {"branch": "b2", "special": False}]})
        msg = ac.check("f.swift")
        assert "b1" in msg and "b2" in msg


def test_absolute_paths_are_normalised() -> None:
    """The guard hands over absolute paths; adjacency records repo-relative ones."""
    assert ac._rel(str(ac.ROOT / "MyApp/Foo.swift")) == "MyApp/Foo.swift"


def test_rel_tolerates_a_path_outside_the_repo() -> None:
    assert ac._rel("/tmp/elsewhere/x.swift") != ""


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failures = []
    print(f"test_adjacency_cache.py — {len(tests)} tests")
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
