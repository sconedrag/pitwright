#!/usr/bin/env python3
"""
test_closeout_landed.py — tests for the merged-PR reconciliation in scripts/closeout_ledger.py.

This repo merges with --delete-branch and squash rewrites SHAs, so a LANDED branch loses its
remote ref and every one of its commits reads as unpushed forever. `/coord:closeout check` nagged
about work already on `main`, and the Stop-hook backstop nudged about it every few turns — a
warning that fires on non-problems is how a real one gets tuned out.

Asserts, against the pure functions (no network, no `gh`):
  1. LANDED       — a merged-PR head reports landed, contributes no actionable drift, is clean.
  2. UNPUSHED     — a branch with no merged PR still reports as unpushed and still nudges.
  3. DEGRADATION  — `landed=None` (gh unavailable) falls back to the unpushed wording and
                    still nudges; it must NEVER claim a branch is safe.
  4. MIGRATIONS   — a landed branch does not suppress the unpushed-migration escalation path
                    for a genuinely-unpushed one.

Run: python3 scripts/tests/test_closeout_landed.py   (exit 0 = pass)
No external deps (plain asserts).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import closeout_ledger as cl  # noqa: E402


def _report(landed, unpushed=3, uncommitted=0, migrations=()):
    """Minimal reconcile()-shaped dict — only the keys the functions under test read."""
    return {
        "branch": "feat/example",
        "uncommitted": [f"f{i}.swift" for i in range(uncommitted)],
        "uncommitted_count": uncommitted,
        "uncommitted_migrations": [],
        "unpushed_commits": [{"sha": f"abc{i}", "subject": f"commit {i}"} for i in range(unpushed)],
        "unpushed_count": unpushed,
        "unpushed_files": [],
        "unpushed_migrations": list(migrations),
        "landed": landed,
        "open_items": [],
    }


def test_landed_contributes_no_actionable_drift():
    assert cl._actionable_unpushed(_report(landed=True)) == 0
    assert "committed-but-unpushed" not in cl._stop_reminder(_report(landed=True))


def test_unpushed_still_reports_and_nudges():
    r = _report(landed=False)
    assert cl._actionable_unpushed(r) == 3
    assert "committed-but-unpushed" in cl._stop_reminder(r)


def test_unknown_degrades_to_unpushed_never_claims_safe():
    # gh unavailable -> landed is None. The contract inherited from sweep_branches: fall back
    # to the noisier signal rather than ever marking a branch falsely safe.
    r = _report(landed=None)
    assert cl._actionable_unpushed(r) == 3, "unknown must NOT be treated as landed"
    assert "committed-but-unpushed" in cl._stop_reminder(r)


def test_landed_branch_reads_clean_but_unpushed_one_does_not():
    landed = _report(landed=True)
    assert landed["uncommitted_count"] == 0 and cl._actionable_unpushed(landed) == 0
    plain = _report(landed=False)
    assert cl._actionable_unpushed(plain) > 0


def test_unpushed_migration_escalation_survives():
    # The migration path is the highest-stakes signal; a landed branch must not weaken it
    # for a branch that genuinely has not pushed.
    r = _report(landed=False, migrations=("supabase/migrations/20260101_x.sql",))
    assert "migration" in cl._stop_reminder(r)



# --------------------------------------------------------------------------- #
# Tip-vs-name reconciliation
# --------------------------------------------------------------------------- #
# `_merged_pr_heads` once returned only branch NAMES, and nothing exercised the distinction.
# A branch name is reused across PRs and keeps accumulating commits after each squash-merge,
# so "a merged PR had this name" never meant "this branch's work is on main". A branch can be
# reported SAFE TO DELETE while sitting many commits past the newest PR merged under its
# name, in a live worktree, with most of its added lines absent from main — the tip being
# genuinely unpushed work.

def _landed(heads, branch, tip):
    """Delegates to the SHIPPED function — deliberately not a local reimplementation.

    The first version of this helper was a copy of the logic, and when the implementation
    moved from a name-keyed lookup to a name-independent one the copy did not. The suite kept
    passing while testing nothing that ships. `branch` is retained only so the call sites read
    naturally; the real test is the SHA.
    """
    if heads is None:
        return None
    return cl._tip_merged(tip, heads)


def test_a_tip_that_a_merged_pr_carried_is_landed():
    assert _landed({"feat/x": {"aaa111"}}, "feat/x", "aaa111") is True


def test_a_branch_that_CONTINUED_past_its_merged_pr_is_NOT_landed():
    """The bug this file exists for. Name matches, tip does not."""
    assert _landed({"feat/x": {"aaa111"}}, "feat/x", "bbb222") is False


def test_a_name_reused_across_several_prs_matches_ANY_of_their_tips():
    heads = {"feat/x": {"aaa111", "ccc333"}}
    assert _landed(heads, "feat/x", "ccc333") is True
    assert _landed(heads, "feat/x", "ddd444") is False


def test_a_tip_that_merged_under_a_DIFFERENT_name_is_still_landed():
    """A local branch can sit at exactly the SHA a merged PR carried, under a different
    name than the PR's own head ref. A name-keyed lookup called it unpushed work at risk.
    A SHA a merged PR carried is on main whatever the local ref is called."""
    assert _landed({"feat/real-name": {"aaa111"}}, "pr1953", "aaa111") is True


def test_a_sha_no_merged_pr_carried_is_not_landed():
    assert _landed({"other": {"aaa111"}}, "feat/x", "zzz999") is False


def test_gh_unavailable_never_claims_landed():
    """Degradation must not resolve toward 'safe' — worse than nagging."""
    assert _landed(None, "feat/x", "aaa111") is None


def test_an_empty_tip_is_not_landed():
    assert _landed({"feat/x": {"aaa111"}}, "feat/x", "") is False


def test_the_cache_key_changed_so_a_name_only_cache_is_ignored():
    """An old cache held `heads` as a LIST of names. Reading it as authoritative would
    reinstate the bug silently, so the reader keys on `heads_by_sha` and ignores the rest."""
    src = (Path(__file__).resolve().parent.parent / "closeout_ledger.py").read_text()
    assert 'blob.get("heads_by_sha")' in src, "cache reader must key on heads_by_sha"
    assert 'headRefName,headRefOid' in src, "the gh query must fetch head SHAs"

def _repo(tmp: Path) -> Path:
    """A throwaway repo with a real `origin/main` ref, so the content test exercises git."""
    import subprocess

    def g(*a):
        subprocess.run(["git", *a], cwd=tmp, capture_output=True, text=True, check=False)

    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (tmp / "a.txt").write_text("1"); (tmp / "gen.json").write_text("{}")
    g("add", "-A"); g("commit", "-qm", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp, capture_output=True,
                          text=True).stdout.strip()

    # a feature branch that edits a.txt and a generated file
    g("checkout", "-qb", "feature")
    (tmp / "a.txt").write_text("2"); (tmp / "gen.json").write_text('{"n":1}')
    g("add", "-A"); g("commit", "-qm", "feature work")

    # main lands the SAME a.txt content under a different SHA (a squash), and regenerates
    # gen.json differently — the exact shape that made three real branches read as at-risk.
    g("checkout", "-q", "main")
    (tmp / "a.txt").write_text("2")
    g("add", "-A"); g("commit", "-qm", "squashed equivalent")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp, capture_output=True,
                          text=True).stdout.strip()
    g("update-ref", "refs/remotes/origin/main", head)

    # a second branch whose work never reached main at all
    g("checkout", "-qb", "orphan", base)
    (tmp / "never.txt").write_text("lost")
    g("add", "-A"); g("commit", "-qm", "genuinely unpushed")
    g("checkout", "-q", "main")
    return tmp


def test_content_equality_recognises_work_that_landed_under_another_branch():
    """Tip-equality asks "did a PR merge FROM this branch" — a different question from "did
    this work reach main". A branch can report as at-risk while its substantive files are
    already byte-identical on main."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td))
        assert cl._content_landed(root, "feature", ["a.txt"]) is True, \
            "a file identical on main must count as landed"


def test_a_branch_whose_work_is_absent_is_NOT_called_safe():
    """The asymmetry is the whole safety property: this may only ever move a branch from
    at-risk to safe on positive evidence, never the reverse."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td))
        assert cl._content_landed(root, "orphan", ["never.txt"]) is False, \
            "work that exists nowhere on main must never be reported as landed"
        # and one differing file is enough to withhold the claim, even alongside an
        # identical one — a generated index drifts constantly and proves nothing.
        assert cl._content_landed(root, "feature", ["a.txt", "gen.json"]) is False, \
            "a differing file must withhold the landed claim"


def test_nothing_to_compare_is_not_evidence():
    """An empty file list would make `all(...)` vacuously true — the exact shape that turns a
    safety check into a rubber stamp."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = _repo(Path(td))
        assert cl._content_landed(root, "feature", []) is False
        assert cl._content_landed(root, "feature", [""]) is False


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\ntest_closeout_landed: {len(tests)} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
