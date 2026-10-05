#!/usr/bin/env python3
"""`_evidence_paths` — a machine-generated file must not veto a branch's clearance.

Both reconcilers in the closeout branch sweep (`_content_landed`, `_main_strictly_newer`)
require the branch to lead NOWHERE among the files it touched. `MyApp.xcodeproj/
project.pbxproj` is rewritten wholesale, in a different order, by every session that adds a
file — so a branch that registered a file weeks ago always leads there regardless of how
completely its real work landed, and that one path vetoes the whole branch.

A branch can have every real file byte-IDENTICAL on main and still be reported "work exists
nowhere else" on the strength of the generated file alone. A bucket that is ~100% noise stops
being read, which is how stale local branches accumulate unnoticed in the first place.

The dangerous direction is the opposite one, so it gets the most cases here: excluding a path
must never turn genuinely-unpushed work into a false "safe". Hence `test_generated_only_branch
_is_never_cleared` and `test_real_work_still_blocks_even_when_pbxproj_is_excluded` — if the
exclusion list ever grows to swallow hand-edited files, those two go red.

Each case pins one branch of the logic, so weakening any single guard turns exactly one case
red rather than leaving the suite green. Built against a real temp git repo: the questions are
about blobs and a merge-base, which cannot be faked with stubs.

Run: python3 scripts/tests/test_closeout_generated_paths.py   (exit 0 = pass)
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import closeout_ledger as cl  # noqa: E402

PBX = "MyApp.xcodeproj/project.pbxproj"
failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, text=True)


def write(root: Path, rel: str, text: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8")


def build(root: Path) -> str:
    """A merge-base, a `mainline` that moved on, and four branches forked from that base."""
    git(root, "init", "-q", "-b", "mainline")
    git(root, "config", "user.email", "t@t.t")
    git(root, "config", "user.name", "t")
    # `additive_files` defaults to EMPTY (app-specific in the repo this shipped from) —
    # this fixture supplies its own via .claude/coord.json, read by `root`-scoped config
    # lookups (_content_landed/_main_strictly_newer both pass root=root), so it never
    # touches process env or the real repo's config.
    write(root, ".claude/coord.json",
          '{"additive_files": ["project.pbxproj"]}\n')
    write(root, PBX, "objects = {\n  A = 1;\n}\n")
    write(root, "Real.swift", "struct Real {}\n")
    write(root, "Doc.md", "intro\n")
    git(root, "add", PBX, "Real.swift", "Doc.md")
    git(root, "commit", "-qm", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root),
                          capture_output=True, text=True).stdout.strip()

    def branch(name: str, body) -> None:
        git(root, "checkout", "-q", "-b", name, base)
        body()
        git(root, "checkout", "-q", "mainline")

    # 1 · the real shape: work landed verbatim on main, pbxproj reordered differently.
    def _landed_but_pbx_reordered():
        write(root, PBX, "objects = {\n  BRANCH_ORDER = 9;\n  A = 1;\n}\n")
        write(root, "Real.swift", "struct Real { let seam: Int }\n")
        git(root, "commit", "-qm", "seam + registration", "--", PBX, "Real.swift")
    branch("b_landed_pbx_noise", _landed_but_pbx_reordered)

    # 2 · ONLY the generated file changed — no evidence survives exclusion, so never cleared.
    def _pbx_only():
        write(root, PBX, "objects = {\n  ONLY_HERE = 7;\n  A = 1;\n}\n")
        git(root, "commit", "-qm", "registration only", "--", PBX)
    branch("b_pbx_only", _pbx_only)

    # 3 · genuinely unpushed work alongside pbxproj churn — must STILL be at risk.
    def _real_work_plus_pbx():
        write(root, PBX, "objects = {\n  X = 3;\n  A = 1;\n}\n")
        write(root, "Unpushed.swift", "struct Unpushed {}\n")
        git(root, "add", "Unpushed.swift")
        git(root, "commit", "-qm", "real work", "--", PBX, "Unpushed.swift")
    branch("b_real_work", _real_work_plus_pbx)

    # 4 · hand-edited doc the branch leads on, PAIRED WITH a file that did land.
    #
    # The pairing is what makes this load-bearing. An over-broad exclusion list that swallowed
    # `.md` would leave only the landed file as evidence — all identical — and the branch would
    # clear while still holding work nobody else has. Were this branch to touch the doc and the
    # GENERATED file only, an over-broad list would empty the evidence set and the branch would
    # stay at-risk for the wrong reason, passing a mutant it should catch.
    def _doc_leads():
        write(root, "Real.swift", "struct Real { let seam: Int }\n")   # identical to main's
        write(root, "Doc.md", "intro\nBRANCH ONLY\n")                  # branch alone has this
        git(root, "commit", "-qm", "doc edit beside landed work", "--", "Real.swift", "Doc.md")
    branch("b_doc_leads", _doc_leads)

    # mainline takes branch 1's real work verbatim, rewrites pbxproj its own way, and moves on.
    write(root, "Real.swift", "struct Real { let seam: Int }\n")
    write(root, PBX, "objects = {\n  A = 1;\n  MAIN_ORDER = 42;\n}\n")
    write(root, "Later.swift", "struct Later {}\n")
    git(root, "add", "Later.swift")
    git(root, "commit", "-qm", "main advances", "--", "Real.swift", PBX, "Later.swift")
    git(root, "branch", "-f", "origin/main", "mainline")
    return base


def touched(root: Path, branch: str, base: str) -> list:
    out = subprocess.run(["git", "diff", "--name-only", f"{base}..{branch}"],
                         cwd=str(root), capture_output=True, text=True)
    return [x for x in out.stdout.splitlines() if x]


def main() -> int:
    # --- the pure filter, independent of any repo -----------------------------------------
    # `additive_files` defaults to empty (app-specific), so these pin the suffix list
    # explicitly via the `generated` override rather than relying on config/filesystem —
    # the mechanism under test is the FILTER, not where its suffix list comes from.
    GEN = (PBX.rsplit("/", 1)[-1],)  # "project.pbxproj" — the suffix, not the full path
    check("filter drops pbxproj", cl._evidence_paths([PBX, "a.swift"], generated=GEN), ["a.swift"])
    check("filter keeps hand-edited churn",
          cl._evidence_paths(["CLAUDE.md", "scripts/audit_registry.yaml"], generated=GEN),
          ["CLAUDE.md", "scripts/audit_registry.yaml"])
    check("filter can empty the list", cl._evidence_paths([PBX], generated=GEN), [])
    # A same-named file elsewhere is still generated; a look-alike name is NOT excluded.
    check("suffix match is anchored to the filename",
          cl._evidence_paths(["Other.xcodeproj/project.pbxproj", "my_project.pbxproj.bak"],
                             generated=GEN),
          ["my_project.pbxproj.bak"])

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        base = build(root)

        def verdict(b: str) -> bool:
            files = touched(root, b, base)
            return cl._content_landed(root, b, files) or cl._main_strictly_newer(root, b, files)

        # 1 · THE FIX: real work identical on main, only pbxproj differs -> cleared.
        check("landed branch cleared despite pbxproj noise", verdict("b_landed_pbx_noise"), True)

        # 2 · no evidence left after exclusion -> never cleared.
        check("pbxproj-only branch is never cleared", verdict("b_pbx_only"), False)

        # 3 · the direction that must never regress: real unpushed work stays at risk.
        check("genuinely unpushed work still at risk", verdict("b_real_work"), False)

        # 4 · hand-edited file the branch leads on still blocks.
        check("hand-edited doc still blocks clearance", verdict("b_doc_leads"), False)

    if failures:
        print("FAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("ok - 8 checks passed (evidence filter + 4 repo shapes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
