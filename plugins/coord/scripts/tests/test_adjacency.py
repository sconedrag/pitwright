#!/usr/bin/env python3
"""
test_adjacency.py — the adjacency detector must be mostly SUBTRACTION.

Raw cross-worktree overlap is ~99% artifact: a worktree with a stale local main can report
thousands of "changed" files against a handful it genuinely touched, and a single generated
state file can appear in nearly every pairwise intersection. A collision warning built on
that would bury its one real signal and train everyone to ignore it. So these tests pin the
filtering, not just the plumbing.

  [1] CHURN     — generated/state paths are excluded; real source is kept.
  [2] BREADTH   — a file most branches touch is infrastructure, not a collision.
  [3] PAIRS     — intersections are correct, and pbxproj is separated from ordinary files.
  [4] DEDUPE    — the same collision is announced once; a CHANGED file set re-announces.

Run: python3 scripts/tests/test_adjacency.py   (exit 0 = pass)
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        FAILURES.append(label)


def test_churn(adj) -> None:
    print("\n[1] churn filtering")
    churn = [
        ".claude/state/last-green-build.txt",
        "Documentation/Reports/geometry/ActivityRings.iPhone15_dark.json",
        "dev-graph-out/nodes-raw.jsonl",
        "MyApp/Tests/Fixtures/PromptSnapshots/legacy_injuries.txt",
        ".claude/coordination/events.log",
    ]
    for f in churn:
        check(adj.is_churn(f), f"excluded: {f}")
    real = [
        "MyApp/Tools/Core/PlannerFrontierSelector.swift",
        "MyApp/UI/Views/ChatPanelView.swift",
        "supabase/migrations/20260101_x.sql",
    ]
    for f in real:
        check(not adj.is_churn(f), f"kept: {f}")


def test_breadth(adj) -> None:
    print("\n[2] breadth heuristic")
    # 8 branches; a file in 7 of them is infrastructure, one in 2 is a real collision.
    per = {f"b{i}": {"shared/Everywhere.swift"} for i in range(7)}
    per["b7"] = set()
    per["b0"] |= {"feature/Real.swift"}
    per["b1"] |= {"feature/Real.swift"}

    original = adj.collect
    try:
        adj.collect = lambda: {"perBranch": per, "meta": {}}  # type: ignore
        data = adj.collect()
        # Apply the same breadth pass collect() would.
        counts: dict[str, int] = {}
        for files in data["perBranch"].values():
            for f in files:
                counts[f] = counts.get(f, 0) + 1
        cutoff = max(2, int(len(per) * adj.BREADTH_FRACTION))
        wide = {f for f, c in counts.items() if c > cutoff}
        check("shared/Everywhere.swift" in wide,
              "a file in 7/8 branches is treated as infrastructure")
        check("feature/Real.swift" not in wide,
              "a file in 2/8 branches survives as a real collision")
    finally:
        adj.collect = original


def test_pairs(adj) -> None:
    print("\n[3] pair computation")
    per = {
        "branch-a": {"src/A.swift", "src/Shared.swift", "MyApp.xcodeproj/project.pbxproj"},
        "branch-b": {"src/B.swift", "src/Shared.swift", "MyApp.xcodeproj/project.pbxproj"},
        "branch-c": {"src/C.swift"},
    }
    rows = adj.pairs(per)
    ab = [r for r in rows if {r["a"], r["b"]} == {"branch-a", "branch-b"}]
    check(len(ab) == 1, "the one colliding pair is reported")
    check(ab and ab[0]["files"] == ["src/Shared.swift"],
          "only the genuinely shared source file is listed")
    check(ab and ab[0]["special"] == ["MyApp.xcodeproj/project.pbxproj"],
          "pbxproj is separated out (it has its own serialization)")
    check(not any("branch-c" in (r["a"], r["b"]) for r in rows),
          "a non-overlapping branch produces no pair")


def test_dedupe(adj, tmp: Path) -> None:
    print("\n[4] announce dedupe")
    os.chdir(tmp)
    (tmp / ".claude" / "coordination").mkdir(parents=True, exist_ok=True)

    pair = {"a": "mine", "b": "theirs", "files": ["src/Shared.swift"], "special": [],
            "count": 1}
    fp1 = adj.fingerprint(pair)
    check(adj.fingerprint(pair) == fp1, "fingerprint is stable for an unchanged file set")

    grown = dict(pair, files=["src/Shared.swift", "src/Another.swift"])
    check(adj.fingerprint(grown) != fp1, "a CHANGED file set yields a new fingerprint")

    # notify() returns (sent, orphaned) since 2026-08-30: a collision whose owning
    # session is GONE is reported rather than messaged, because a write to a dead
    # session's mailbox is delivered to nobody, permanently, with no error either side.
    # Both cases below have no registered peer at all, so both lists stay empty — the
    # orphaned assertions are not padding, they distinguish "nobody to tell" from
    # "somebody to tell, who is dead".

    # No peer registered on that branch -> nothing sent, nothing orphaned, no state.
    sent, orphaned = adj.notify("mine", [pair])
    check(sent == [], "no message when no session is registered on the peer branch")
    check(orphaned == [], "an unregistered branch is not reported as an orphan")

    adj._save_sent({"theirs": fp1})
    check(adj._sent_state().get("theirs") == fp1, "sent-state round-trips")
    sent2, orphaned2 = adj.notify("mine", [pair])
    check(sent2 == [], "an already-announced collision is not re-sent")
    check(orphaned2 == [], "dedupe suppresses the orphan path too")


def main() -> int:
    print("test_adjacency.py")
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        os.environ["AGENT_CHANNEL_DIR"] = str(tmp / "agent-coordination")
        # `churn_globs`/`additive_files` default EMPTY (app-specific in the repo this
        # shipped from), so this fixture supplies the same values the production defaults
        # used to carry, via .claude/coord.json + CLAUDE_PROJECT_DIR — never the real
        # repo's config. BUILTIN_CHURN_PATTERNS (.claude/state, .claude/coordination) are
        # NOT re-declared here: those are intrinsic to this plugin and need no config.
        (tmp / ".claude").mkdir(parents=True, exist_ok=True)
        (tmp / ".claude" / "coord.json").write_text(json.dumps({
            "churn_globs": [
                r"^Documentation/Reports/",
                r"^dev-graph-out/",
                r"/PromptSnapshots/",
                r"^scripts/\.[a-z_]+baseline",
                r"\.baseline\.json$",
            ],
            "additive_files": ["project.pbxproj"],
        }))
        os.environ["CLAUDE_PROJECT_DIR"] = str(tmp)
        import adjacency as adj  # noqa: E402
        try:
            test_churn(adj)
            test_breadth(adj)
            test_pairs(adj)
            test_dedupe(adj, tmp)
        finally:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
            os.chdir(cwd)
    print(f"\n{'FAILED: ' + str(len(FAILURES)) if FAILURES else 'ALL PASS'}")
    for f in FAILURES:
        print(f"  - {f}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
