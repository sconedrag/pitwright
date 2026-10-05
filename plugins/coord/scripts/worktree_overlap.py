#!/usr/bin/env python3
"""
worktree_overlap.py — Coordination Harness v2, cross-worktree collision early-warning.

In the worktree model the real merge hazard isn't edit-time (each worktree edits
its own physical files) — it's MERGE-time: two worktree branches that changed the
SAME file will conflict when both fold back into main. The per-file lock system
(session-file-guard) can't see this because each worktree has its own coordination
dir and its own physical copies.

This reads every worktree branch from the SHARED object store (no shared
coordination needed — git already has all branches) and reports pairwise file
overlap, so you can resolve/sequence BEFORE doing duplicate work or hitting a
surprise conflict.

For each worktree it considers:
  - committed divergence from main:  git diff --name-only <merge-base>...<branch>
  - uncommitted working-tree edits:  git -C <worktree> status --porcelain

Run from anywhere in the repo:
    python3 scripts/worktree_overlap.py            # human report
    python3 scripts/worktree_overlap.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from itertools import combinations
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coord_config  # noqa: E402


def _git(*args: str, cwd: str | None = None) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
        ).stdout
    except (OSError, ValueError):
        return ""


def repo_root() -> str:
    return str(coord_config.project_root())


def worktrees() -> list[dict]:
    """Parse `git worktree list --porcelain` into [{path, branch}], excluding main."""
    out = _git("worktree", "list", "--porcelain")
    entries, cur = [], {}
    for line in out.splitlines():
        if line.startswith("worktree "):
            cur = {"path": line[len("worktree "):]}
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch "):].replace("refs/heads/", "")
        elif line == "" and cur:
            entries.append(cur); cur = {}
    if cur:
        entries.append(cur)
    # Exclude the primary checkout (branch == main / master).
    return [e for e in entries if e.get("branch") not in ("main", "master") and "branch" in e]


def _main_ref() -> str:
    """The freshest available main ref.

    Prefer origin/main. Basing the merge-base on the LOCAL main is what made this tool
    unusable: local main here was 255 commits behind origin/main, so every branch's diff
    also contained everything that had landed on main since — this worktree reported 1675
    changed files where it had genuinely changed 25 (67x inflation), and the pairwise
    overlap that fell out of it was ~98.5% artifact. A collision detector that is mostly
    wrong is one nobody can act on.
    """
    for ref in ("origin/main", "origin/master", "main", "master"):
        if _git("rev-parse", "--verify", "--quiet", ref).strip():
            return ref
    return "main"


def changed_files(branch: str, path: str) -> set[str]:
    """Files this worktree changed vs main — committed divergence + uncommitted edits."""
    files: set[str] = set()
    base = _git("merge-base", _main_ref(), branch).strip()
    if base:
        for ln in _git("diff", "--name-only", f"{base}", branch).splitlines():
            if ln.strip():
                files.add(ln.strip())
    # Uncommitted edits in the worktree dir (staged + unstaged + untracked tracked-ish).
    for ln in _git("status", "--porcelain", cwd=path).splitlines():
        name = ln[3:].strip() if len(ln) > 3 else ""
        # handle "old -> new" rename form
        if " -> " in name:
            name = name.split(" -> ", 1)[1]
        if name:
            files.add(name)
    return files


def main_uncommitted(root: str) -> set[str]:
    files = set()
    for ln in _git("status", "--porcelain", cwd=root).splitlines():
        name = ln[3:].strip() if len(ln) > 3 else ""
        if " -> " in name:
            name = name.split(" -> ", 1)[1]
        if name and not name.startswith(".claude/coordination"):
            files.add(name)
    return files


def main() -> int:
    parser = argparse.ArgumentParser(description="Cross-worktree file-overlap detector.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    root = repo_root()
    wts = worktrees()
    per = {w["branch"]: changed_files(w["branch"], w["path"]) for w in wts}
    main_dirty = main_uncommitted(root)

    # Pairwise overlaps between worktrees.
    pairs = []
    for (b1, f1), (b2, f2) in combinations(per.items(), 2):
        inter = sorted(f1 & f2)
        if inter:
            pairs.append({"a": b1, "b": b2, "files": inter})
    # Overlap of each worktree with main's own uncommitted changes.
    main_overlaps = []
    for b, f in per.items():
        inter = sorted(f & main_dirty)
        if inter:
            main_overlaps.append({"branch": b, "files": inter})

    # A file matching `additive_files` (default empty — the app this shipped from used this
    # for a near-universal Xcode project.pbxproj overlap) is additive/merge-friendly — call
    # it out separately rather than flagging it as an ordinary conflict.
    additive_suffixes = tuple(coord_config.get("additive_files"))

    def _is_additive(path: str) -> bool:
        return bool(additive_suffixes) and path.endswith(additive_suffixes)

    if args.json:
        print(json.dumps({
            "worktrees": [{"branch": b, "changedCount": len(f)} for b, f in per.items()],
            "pairwiseOverlaps": pairs,
            "mainUncommittedOverlaps": main_overlaps,
        }, indent=2))
        return 0

    print("\n═══ Cross-worktree overlap (merge-collision early-warning) ═══")
    if not wts:
        print("  No worktrees besides main — nothing to collide.")
        return 0
    for b, f in per.items():
        print(f"  ● {b}: {len(f)} file(s) changed vs main")
    if not pairs and not main_overlaps:
        print("\n  ✅ No file overlaps between worktrees — merges should be conflict-free.")
        return 0
    if pairs:
        print("\n  ⚠️ Worktrees touching the SAME files (will conflict at merge):")
        for p in pairs:
            non_additive = [x for x in p["files"] if not _is_additive(x)]
            note = "  (additive files only — low-risk)" if additive_suffixes and not non_additive else ""
            print(f"    {p['a']}  ✕  {p['b']}  — {len(p['files'])} file(s){note}")
            for x in p["files"][:8]:
                tag = " [additive]" if _is_additive(x) else ""
                print(f"        {x}{tag}")
            if len(p["files"]) > 8:
                print(f"        … +{len(p['files']) - 8} more")
    if main_overlaps:
        print("\n  ⚠️ Worktrees overlapping main's uncommitted changes:")
        for m in main_overlaps:
            print(f"    {m['branch']} — {len(m['files'])} file(s) (commit/merge main first)")
    print("\n  → Resolve by: merging the overlapping worktrees SEQUENTIALLY (rebase each on")
    print("    the prior), or splitting their scope so they stop touching shared files.")
    print("    Non-additive overlaps are real conflicts; additive ones (if configured) are "
          "merge-friendly (keep both).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
