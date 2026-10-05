#!/usr/bin/env python3
"""`_main_strictly_newer` — the third positive-evidence signal in the closeout branch sweep.

`_content_landed` clears a branch only when every touched file is byte-IDENTICAL on main. That
is the rare shape. A branch can be reported as "work exists nowhere else" while main led on
every substantive file and the branch led on none — none could be cleared, because main had
moved on and so nothing was identical any more.

The obvious generalisation — "the diff from branch to main only ADDS lines" — is wrong in BOTH
directions, which is the whole reason this file exists:

  · it UNDER-clears where main DELETED a file the branch touched (the diff is all deletions) —
    those files, byte-identical across several independent branches, were main's own deletions.
  · it OVER-clears where the BRANCH deleted a file main still has (the diff is all additions),
    silently discarding the one thing that branch uniquely did. This is the dangerous direction:
    the sweep exists to stop unpushed work being lost, so a false "safe" defeats its purpose.

Each case below pins one branch of the logic, so removing any single guard turns exactly one
case red rather than leaving the suite green on a weakened check. Built against a real temp git
repo — the questions are about blobs and a merge-base, which cannot be faked with stubs.

Run: python3 scripts/tests/test_closeout_main_newer.py   (exit 0 = pass)
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import closeout_ledger as cl  # noqa: E402

failures: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        failures.append(f"{label}: got {got!r}, want {want!r}")


def git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(root), check=True,
                   capture_output=True, text=True)


def write(root: Path, rel: str, text: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text, encoding="utf-8")


def touched(root: Path, branch: str) -> list:
    """Exactly what sweep_branches passes: files in the branch's not-on-any-remote commits."""
    out = subprocess.run(["git", "log", "--format=", "--name-only", branch, "--not", "--remotes"],
                         cwd=str(root), capture_output=True, text=True)
    return out.stdout.splitlines()


def build(root: Path) -> None:
    git(root, "init", "-q", "-b", "mainline")
    git(root, "config", "user.email", "t@t.t")
    git(root, "config", "user.name", "t")
    # --- merge base: what both sides forked from -------------------------------------------
    write(root, "shared.txt", "a\n")
    write(root, "gone_on_main.txt", "x\n")
    write(root, "kept_on_main.txt", "k\n")
    write(root, "binary.bin", "\x00v1\n")
    git(root, "add", "shared.txt", "gone_on_main.txt", "kept_on_main.txt", "binary.bin")
    git(root, "commit", "-qm", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root),
                          capture_output=True, text=True).stdout.strip()

    # --- the six branches, each forked from base -------------------------------------------
    def branch(name: str, body) -> None:
        git(root, "checkout", "-q", "-b", name, base)
        body()
        git(root, "checkout", "-q", "mainline")

    # 1 · main will contain this branch's line, plus more  -> CLEARED (modified, additions-only)
    def _cleared():
        write(root, "shared.txt", "a\nb\n")
        git(root, "commit", "-qm", "add b", "--", "shared.txt")
    branch("b_subset", _cleared)

    # 2 · branch touched a file main later DELETED        -> CLEARED (the ~150-file shape)
    def _main_deleted():
        write(root, "gone_on_main.txt", "x\ny\n")
        git(root, "commit", "-qm", "edit doomed file", "--", "gone_on_main.txt")
    branch("b_main_deleted", _main_deleted)

    # 3 · branch CREATED a file main never had            -> NOT cleared (real unpushed work)
    def _created():
        write(root, "brand_new.txt", "n\n")
        git(root, "add", "brand_new.txt")
        git(root, "commit", "-qm", "new file", "--", "brand_new.txt")
    branch("b_created", _created)

    # 4 · branch DELETED a file main still has            -> NOT cleared (the over-clear trap)
    def _deleted():
        git(root, "rm", "-q", "kept_on_main.txt")
        git(root, "commit", "-qm", "remove it", "--", "kept_on_main.txt")
    branch("b_deleted_file", _deleted)

    # 5 · branch CHANGED a line main does not have        -> NOT cleared (a real deletion)
    def _leads():
        write(root, "shared.txt", "a\nZZZ\n")
        git(root, "commit", "-qm", "diverge", "--", "shared.txt")
    branch("b_leads", _leads)

    # 6 · binary, differing on both sides                 -> NOT cleared (no line counts)
    def _binary():
        write(root, "binary.bin", "\x00v-branch\n")
        git(root, "commit", "-qm", "bin", "--", "binary.bin")
    branch("b_binary", _binary)

    # --- main advances past every branch ----------------------------------------------------
    write(root, "shared.txt", "a\nb\nc\n")          # contains b_subset's line, plus c
    write(root, "binary.bin", "\x00v-main\n")
    git(root, "rm", "-q", "gone_on_main.txt")
    git(root, "add", "shared.txt", "binary.bin")
    git(root, "commit", "-qm", "main moves on")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root),
                          capture_output=True, text=True).stdout.strip()
    git(root, "update-ref", "refs/remotes/origin/main", head)


with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    build(root)

    # 0 · the fixture itself must be load-bearing: if `--not --remotes` yields nothing, every
    #     assertion below would pass vacuously on an empty path list.
    for b in ("b_subset", "b_main_deleted", "b_created", "b_deleted_file", "b_leads", "b_binary"):
        check(f"fixture: {b} has touched files", bool(touched(root, b)), True)

    expected = {
        "b_subset": True,          # main contains its line and has added another
        "b_main_deleted": True,    # main deleted the file — main is newer, not the branch
        "b_created": False,        # the branch made something main has never seen
        "b_deleted_file": False,   # the branch's deletion IS its work
        "b_leads": False,          # one removed line is enough
        "b_binary": False,         # no line counts, so no evidence
    }
    for name, want in expected.items():
        check(f"_main_strictly_newer({name})",
              cl._main_strictly_newer(root, name, touched(root, name)), want)

    # 1 · degradation: no evidence must never read as evidence.
    check("empty file list is not evidence", cl._main_strictly_newer(root, "b_subset", []), False)
    check("unknown branch is not evidence",
          cl._main_strictly_newer(root, "no_such_branch", ["shared.txt"]), False)

    # 2 · the signal is actually CONSULTED by the sweep, not merely defined. `landed` must flip
    #     for b_subset with no merged PR anywhere — which is precisely the case that used to be
    #     reported as "work exists nowhere else".
    real = cl._merged_pr_heads
    try:
        cl._merged_pr_heads = lambda *_a, **_k: {}      # gh reachable, zero merged PRs
        swept = {b["branch"]: b["landed"] for b in cl.sweep_branches(root)}
    finally:
        cl._merged_pr_heads = real
    check("sweep_branches saw the branches", bool(swept), True)
    for name, want in expected.items():
        check(f"sweep_branches landed[{name}]", swept.get(name), want)

if failures:
    print(f"FAIL ({len(failures)}):")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("test_closeout_main_newer: 6 file shapes + 2 degradation cases + end-to-end sweep, all pinned")
