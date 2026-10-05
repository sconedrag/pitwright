#!/usr/bin/env python3
"""
closeout_ledger.py — Closeout hygiene tracker.

Catches the "abandoned just before commit/push" failure mode: a *completed* unit
of work left uncommitted, committed-but-unpushed, or on an orphaned branch while
attention moved on to the next branch of an implementation plan. Unpushed DB
migrations are the same class of silent loss, called out distinctly.

Two layers, one brain:

  1. RAW GIT DRIFT (`reconcile`) — works with ZERO discipline. Buckets uncommitted
     changes (migration / source / docs / other), counts unpushed commits on the
     current branch, flags unpushed migrations. Powers the Stop-hook backstop
     (`stopcheck`), the SessionStart surface line (`surface_line`), and the
     /coord:board Closeout section.

  2. EXPLICIT LEDGER — the "running list". Bounded (<=100) JSON of work items with
     a closeout lifecycle (open -> committed -> pushed -> done | dropped), seeded
     automatically on session start (`autoseed`) or manually (/coord:closeout start).

Ledger:  .claude/coordination/closeout-ledger.json  (gitignored, per-worktree)
Watch:   .claude/coordination/closeout-watch.json    (gitignored; Stop-hook dedup)

Pairs with a commit-forward discipline (commit a logical unit as soon as it's green,
don't hold it uncommitted waiting on async verification). Once work IS surfaced,
recover/close it by committing, pushing the branch (opening/refreshing its PR), or
saving a WIP snapshot.

Best-effort by design: no git / no python / detached checkout -> empty result, never
a crash. Per-checkout: reports THIS worktree's drift.

Usage:
  python3 scripts/closeout_ledger.py check                 # human reconciliation report
  python3 scripts/closeout_ledger.py list                  # ledger + live drift
  python3 scripts/closeout_ledger.py start "<title>" [--plan F] [--paths A B ...]
  python3 scripts/closeout_ledger.py done <id>
  python3 scripts/closeout_ledger.py drop <id> "<reason>"
  python3 scripts/closeout_ledger.py autoseed              # idempotent; SessionStart uses this
  python3 scripts/closeout_ledger.py stopcheck             # Stop-hook: stale-gated reminder or ""
  python3 scripts/closeout_ledger.py --surface             # one-line SessionStart highlight
  python3 scripts/closeout_ledger.py --json                # machine-readable reconcile + ledger
"""

# `set | None` (PEP 604) is evaluated at runtime in annotations; the hooks that call this
# script can run under the SYSTEM interpreter (/usr/bin/python3 is 3.9.6 on this machine),
# which would raise TypeError at import. The future import defers annotation evaluation.
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coord_config  # noqa: E402

LEDGER_CAP = 100               # bounded collection: evict closed items first
OPEN_STATUSES = ("open", "committed", "pushed")
STALE_TURNS_DEFAULT = 3        # turns an unchanged drift must persist before nudging
STALE_TURNS_HIGH_STAKES = 1    # committed-but-unpushed work nudges sooner
# Idle days before an unpushed branch enters the passive nudge — see _stale_branch_days();
# the default (7) lives in coord_config's "closeout_stale_branch_days" schema entry.
MAX_BRANCH_SCAN = 500          # guard against pathological repos in the branch sweep
MERGED_PR_CACHE_TTL = 6 * 3600  # seconds; the sweep runs on SessionStart/Stop, so don't re-query gh each time
MERGED_PR_SCAN = 1000          # merged PRs to reconcile against; raise if your repo has more history


def _stale_branch_days(root: Path | None = None) -> int:
    """`closeout_stale_branch_days`, overridable via env (or .claude/coord.json). `root`
    scopes the lookup to a known repo rather than the current process cwd."""
    return coord_config.get("closeout_stale_branch_days", root=root)


# ---------------------------------------------------------------------------
# git / fs helpers
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    return coord_config.project_root()


def _git(root: Path, *args: str) -> str:
    """Run a git command; return stdout (stripped) or '' on any failure."""
    try:
        out = subprocess.run(["git", *args], cwd=str(root),
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _coord(root: Path) -> Path:
    return root / ".claude" / "coordination"


def _ledger_path(root: Path) -> Path:
    return _coord(root) / "closeout-ledger.json"


def _watch_path(root: Path) -> Path:
    return _coord(root) / "closeout-watch.json"


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().replace(microsecond=0).isoformat()


def _load_json(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _save_json(path: Path, data) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))
    except OSError:
        pass


def _idle_days(iso) -> int:
    try:
        ts = datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=datetime.timezone.utc)
        return max(0, (_utcnow() - ts).days)
    except (ValueError, TypeError):
        return 0


# ---------------------------------------------------------------------------
# ledger CRUD (bounded)
# ---------------------------------------------------------------------------

def load_ledger(root: Path) -> list:
    data = _load_json(_ledger_path(root))
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    return []


def save_ledger(root: Path, items: list) -> None:
    # Bounded: keep all in-progress items; if over cap, evict oldest closed ones.
    if len(items) > LEDGER_CAP:
        active = [it for it in items if it.get("status") in OPEN_STATUSES]
        closed = [it for it in items if it.get("status") not in OPEN_STATUSES]
        closed.sort(key=lambda it: it.get("updatedAt", ""))
        room = max(0, LEDGER_CAP - len(active))
        items = active + closed[-room:] if room else active
    _save_json(_ledger_path(root), {"items": items})


def _next_id(items: list) -> str:
    n = 0
    for it in items:
        sid = str(it.get("id", ""))
        if sid.startswith("co-"):
            try:
                n = max(n, int(sid[3:]))
            except ValueError:
                pass
    return f"co-{n + 1}"


def current_branch(root: Path) -> str:
    return _git(root, "rev-parse", "--abbrev-ref", "HEAD")


def _active_plan(root: Path):
    """Most recently modified plan markdown, if any."""
    candidates = []
    for d in (root / ".claude" / "plans", Path.home() / ".claude" / "plans"):
        if d.is_dir():
            candidates.extend(d.glob("*.md"))
    if not candidates:
        return None
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    return str(newest)


def _title_from_plan(plan_path) -> str:
    if not plan_path:
        return ""
    try:
        for line in Path(plan_path).read_text().splitlines():
            line = line.strip()
            if line.startswith("# "):
                return line[2:].strip()
    except OSError:
        pass
    return ""


# ---------------------------------------------------------------------------
# raw git drift — the reconciler
# ---------------------------------------------------------------------------

def _bucket_rules(root: Path | None = None) -> list:
    """[{"name", "prefix", "suffix"}, ...] from the `closeout_buckets` setting.

    Default empty: the project this was extracted from classified uncommitted files by
    app-specific paths (a database-migrations-folder prefix, a docs-folder prefix) that
    don't generalize. A project supplies its own via `.claude/coord.json`; everything
    else falls into the "other" catch-all, which always exists.

    `root` scopes the config lookup to a known repo (most callers already carry one)
    instead of the current process cwd.
    """
    rules = []
    for r in coord_config.get("closeout_buckets", root=root):
        if isinstance(r, dict) and r.get("name"):
            rules.append({"name": str(r["name"]), "prefix": r.get("prefix", ""),
                          "suffix": r.get("suffix", "")})
    return rules


def _bucket_match(path: str, rule: dict) -> bool:
    prefix, suffix = rule.get("prefix", ""), rule.get("suffix", "")
    if not prefix and not suffix:
        return False
    return (not prefix or path.startswith(prefix)) and (not suffix or path.endswith(suffix))


def _migration_rule(root: Path | None = None) -> dict:
    """The `closeout_buckets` entry named "migration", or {} if unconfigured — the single
    source both `_bucket()` and the unpushed/stale-branch migration filters read, so a
    project configures migration detection once rather than per call site."""
    for r in _bucket_rules(root):
        if r["name"] == "migration":
            return r
    return {}


def _is_migration(path: str, rule: dict) -> bool:
    return bool(rule) and _bucket_match(path, rule)


def _bucket(paths: list, root: Path | None = None) -> dict:
    rules = _bucket_rules(root)
    b: dict = {"other": []}
    for rule in rules:
        b.setdefault(rule["name"], [])
    for p in paths:
        placed = False
        for rule in rules:
            if _bucket_match(p, rule):
                b[rule["name"]].append(p)
                placed = True
                break
        if not placed:
            b["other"].append(p)
    return b


def _uncommitted(root: Path) -> list:
    paths = []
    for line in _git(root, "status", "--porcelain").splitlines():
        if not line.strip():
            continue
        p = line[3:].strip()
        if " -> " in p:                 # rename: take the destination
            p = p.split(" -> ", 1)[1]
        paths.append(p.strip('"'))
    return paths


def _unpushed_range(root: Path):
    """(range, has_upstream). Prefer the branch's own upstream; else fall back to
    origin/main so a not-yet-pushed feature branch still reports its commits."""
    up = _git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    if up:
        return f"{up}..HEAD", True
    if _git(root, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/main"):
        return "refs/remotes/origin/main..HEAD", False
    return None, False


def reconcile(root: Path) -> dict:
    branch = current_branch(root)
    uncommitted = _uncommitted(root)
    buckets = _bucket(uncommitted, root)

    rng, has_upstream = _unpushed_range(root)
    unpushed, unpushed_files = [], []
    if rng:
        for line in _git(root, "log", "--format=%h\x1f%s", rng).splitlines():
            if "\x1f" in line:
                sha, subj = line.split("\x1f", 1)
                unpushed.append({"sha": sha, "subject": subj})
        unpushed_files = [f for f in _git(root, "diff", "--name-only", rng).splitlines() if f.strip()]
    mig_rule = _migration_rule(root)
    unpushed_migrations = sorted({f for f in unpushed_files if _is_migration(f, mig_rule)})

    # Squash-merge rewrites SHAs and this repo merges with --delete-branch, so a LANDED
    # branch loses its remote ref and every one of its commits then reads as unpushed —
    # forever. `sweep_branches` already reconciles that for OTHER branches; the current
    # branch needs the same treatment or `check` nags about work that is already on main.
    # `None` (no gh / offline) means "cannot tell" — fall back to the un-reconciled signal
    # rather than ever claiming a branch is safe (sweep_branches' contract).
    landed = None
    if unpushed and branch:
        heads = _merged_pr_heads(root)
        if heads is None:
            landed = None
        else:
            tip = _git(root, "rev-parse", branch).strip()
            landed = bool(tip) and _tip_merged(tip, heads)

    open_items = []
    for it in load_ledger(root):
        if it.get("status") in OPEN_STATUSES:
            enriched = dict(it)
            enriched["_idle_days"] = _idle_days(it.get("updatedAt"))
            open_items.append(enriched)

    return {
        "branch": branch,
        "uncommitted": uncommitted,
        "uncommitted_count": len(uncommitted),
        "buckets": buckets,
        "uncommitted_migrations": buckets.get("migration", []),
        "unpushed_commits": unpushed,
        "unpushed_count": len(unpushed),
        "unpushed_files": unpushed_files,
        "unpushed_migrations": unpushed_migrations,
        "has_upstream": has_upstream,
        # True = branch is a merged-PR head (work is on main); False = genuinely unpushed;
        # None = undeterminable (gh unavailable) or nothing unpushed to classify.
        "landed": landed,
        "open_items": open_items,
        "clean": len(uncommitted) == 0 and (len(unpushed) == 0 or landed is True),
    }


# ---------------------------------------------------------------------------
# stale local-branch sweep — repo-wide unpushed-only work (abandoned branches)
# ---------------------------------------------------------------------------

def _checked_out_branches(root: Path) -> set:
    """Short names of branches checked out in any worktree (incl. the main checkout)."""
    res = set()
    for line in _git(root, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("branch "):
            ref = line.split(" ", 1)[1].strip()
            res.add(ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref)
    return res


def _tip_merged(tip: str, heads: dict) -> bool:
    """True when `tip` is the head SHA of ANY merged PR — name-independent, deliberately.

    Keying the lookup by branch name misses a local branch whose NAME differs from the PR's
    head ref: a branch can sit at exactly the SHA a PR merged, under a different name, and a
    name-keyed lookup reported it as unpushed work at risk. A SHA that a merged PR carried is
    on `main` whatever the local ref is called, so the name is metadata, not the test.
    """
    if not tip:
        return False
    return any(tip in shas for shas in heads.values())


def _merged_pr_heads(root: Path) -> dict | None:
    """`{head branch name: {merged head SHAs}}` for merged PRs, cached. `None` when
    unavailable (no `gh`, not authenticated, offline) — callers then fall back to the
    un-reconciled signal.

    THE SHAs ARE NOT OPTIONAL — an earlier version of this returned only NAMES. A branch name
    is reused across PRs and keeps accumulating commits after each squash-merge, so
    "a merged PR had this name" does not mean "this branch's work is on main". Measured in
    practice: a branch was reported SAFE TO DELETE while sitting well past the newest PR
    that merged under its name, in a live worktree, with most of its added lines absent
    from `main` — the tip commit holding unpushed, safety-critical work. More than one
    branch was misreported that way in a single sweep. A branch is landed only when its
    TIP is one of the SHAs a merged PR carried.

    Why this exists: see `sweep_branches`. One `gh` call covers every merged PR, so it is
    cheap; the cache exists only because the sweep runs on SessionStart/Stop hooks and
    should not pay a network round-trip on every turn.
    """
    cache = _coord(root) / "merged-pr-heads.json"
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    try:
        if cache.exists():
            blob = json.loads(cache.read_text(encoding="utf-8"))
            # `heads_by_sha` (not the old `heads` list) — an old name-only cache is ignored
            # rather than misread as authoritative.
            if (now - float(blob.get("fetched_at", 0))) < MERGED_PR_CACHE_TTL \
                    and isinstance(blob.get("heads_by_sha"), dict):
                return {k: set(v) for k, v in blob["heads_by_sha"].items()}
    except (OSError, ValueError, TypeError):
        pass  # unreadable/stale cache — re-fetch
    cmd = ["gh", "pr", "list", "--state", "merged", "--limit", str(MERGED_PR_SCAN),
           "--json", "headRefName,headRefOid",
           "-q", r'.[] | "\(.headRefName)\t\(.headRefOid)"']

    def _run(env=None):
        return subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                              timeout=60, env=env)

    try:
        out = _run()
        if out.returncode != 0:
            # `gh` reads GH_TOKEN/GITHUB_TOKEN *before* its keyring. A stale or revoked token
            # in the environment therefore beats a keyring credential that works, and every
            # call fails with "Bad credentials" — which used to degrade this sweep silently.
            # Retry once with those cleared so a shadowing env var cannot cost us the
            # reconciliation. (Observed in practice: a revoked PAT exported from an older
            # shell made the sweep report most branches at-risk when only a minority were
            # real.)
            if any(os.environ.get(v) for v in ("GH_TOKEN", "GITHUB_TOKEN")):
                env = {k: v for k, v in os.environ.items()
                       if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
                out = _run(env=env)
            if out.returncode != 0:
                return None
        heads: dict = {}
        for ln in out.stdout.splitlines():
            name, _, oid = ln.strip().partition("\t")
            if name and oid:
                heads.setdefault(name, set()).add(oid)
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({
            "fetched_at": now,
            "heads_by_sha": {k: sorted(v) for k, v in heads.items()},
        }), encoding="utf-8")
    except OSError:
        pass  # the cache is an optimisation, not a requirement
    return heads


# Machine-generated paths that testify to NOTHING about whether a branch's work reached main.
#
# An example: a project's build-system manifest file (e.g. an Xcode `project.pbxproj`) gets
# rewritten wholesale, in a different order, by every session that adds a file. So a branch
# that registered a file weeks ago always "leads" there — it holds bytes main does not — no
# matter how completely its actual work landed. Because both reconcilers below require the
# branch to lead NOWHERE, that single file vetoes the whole branch.
#
# Measured once over a sweep's at-risk list: several branches had every real file
# byte-IDENTICAL to main and were reported "work exists nowhere else" on the strength of that
# one generated file alone. The majority of a sampled at-risk list were verifiably safe once
# it was excluded — a bucket that is ~100% noise stops being read, which is precisely how a
# large backlog of stale local branches can accumulate unnoticed.
#
# Scope is deliberately narrow, and hand-edited files stay in evidence even when they churn
# hard (a doctrine file, a generated-index source file, a project census script each blocked a
# branch here in practice). Those can genuinely carry unlanded work; a generated file cannot.
# Excluding them would trade a noisy warning for a silent loss, which is the one error this
# sweep must never make.
#
# The actual suffix list is the `additive_files` setting (default empty — the project this
# shipped from used a single build-manifest entry, which doesn't generalize) so a
# project names its own generated/rewrite-wholesale files via `.claude/coord.json`.
def _generated_evidence_paths(root: Path | None = None) -> tuple:
    return tuple(coord_config.get("additive_files", root=root))


def _evidence_paths(files: list, generated: tuple | None = None, root: Path | None = None) -> list:
    """`files` minus the machine-generated paths whose bytes cannot testify about landing.

    Both callers are positive-evidence-only — they may move a branch from at-risk to safe and
    never the reverse — so dropping a path that carries no signal cannot create a false at-risk.
    It could in principle create a false SAFE, and the guard against that is that both callers
    return False on an empty list: a branch whose only change IS a generated file has no
    evidence left and is never cleared.

    `generated` lets a caller (tests, mainly) pin the suffix list explicitly instead of going
    through config/filesystem. `root` scopes the config lookup to the repo under examination
    (both callers already carry one) rather than the current process cwd, which may differ.
    """
    gen = generated if generated is not None else _generated_evidence_paths(root)
    return [f for f in files if f and not f.endswith(gen)]


def _content_landed(root: Path, name: str, files: list) -> bool:
    """True when every file this branch touched is byte-identical on `origin/main`.

    Tip-equality answers "did a PR merge FROM this branch", which is not the same question as
    "did this work reach main". Work lands under a different head branch, or is re-applied as
    a fresh commit, and the tip then matches nothing — measured in practice: branches have
    been reported at-risk ("work exists nowhere else") while every substantive file was
    already identical on main, one of them in ALL its files.

    Content equality is sound where ancestry and patch-id are not: squash-merge rewrites SHAs,
    but it does not change the bytes that land. The asymmetry is deliberate — all-identical
    PROVES the content is on main, whereas a difference proves nothing (a generated index or
    an append-only registry drifts constantly), so this can only move a branch from at-risk to
    safe on positive evidence, never the reverse.
    """
    paths = _evidence_paths(files, root=root)
    if not paths:
        return False          # nothing to compare is not evidence of anything
    return _git(root, "diff", "--name-only", name, "origin/main", "--", *paths).strip() == ""


def _main_strictly_newer(root: Path, name: str, files: list) -> bool:
    """True when `origin/main` holds every line this branch wrote, and has moved on past it.

    `_content_landed` clears a branch only when every touched file is byte-IDENTICAL, which is
    the rare shape. The common one is that main advanced: the branch's content is all present
    and main has since added to it. Measured in practice across several branches reported as
    "work exists nowhere else" — main led on every substantive file and the branch led on
    none, yet they all stayed at-risk, because by then no file was still identical.

    Three file shapes, each asking a DIFFERENT question. Answering with the wrong one clears a
    branch that genuinely leads, which is the one error this must never make:

      · both sides have it  → main must contain it wholly: the diff adds and never deletes. One
        deleted line means the branch holds something main does not.
      · only the BRANCH has it → main DELETED it (and is therefore newer) if the merge-base had
        it; if the merge-base did NOT, the branch CREATED it and that is unpushed work. This is
        the shape that makes the naive check wrong: ~150 such files, byte-identical across four
        independent branches, were main's own deletions — an additions-only test reads every one
        of them as the branch leading, and clears nothing.
      · only MAIN has it → the mirror. Fine when the merge-base lacked it (main added it), but
        if the merge-base HAD it then the branch deleted it, which is the branch's own work.

    Renames are decomposed (`--no-renames`) into an add plus a delete so both halves get the
    test above rather than a similarity score. Binary files report no line counts and so are
    never cleared. Like `_content_landed` this is positive-evidence-only: it can move a branch
    out of at-risk, never into it.
    """
    paths = sorted(set(_evidence_paths(files, root=root)))
    if not paths:
        return False              # nothing to compare is not evidence of anything
    base = _git(root, "merge-base", name, "origin/main")
    if not base:
        return False
    status = _git(root, "diff", "--no-renames", "--name-status", name, "origin/main", "--", *paths)
    if not status:
        # Ambiguous: `_git` returns '' for a FAILED command as well as for no-differences, and
        # the all-identical case is `_content_landed`'s to claim. Either way, not ours.
        return False
    removed: dict = {}
    for line in _git(root, "diff", "--no-renames", "--numstat",
                     name, "origin/main", "--", *paths).splitlines():
        cols = line.split("\t")
        if len(cols) >= 3:
            removed[cols[2]] = cols[1]      # '-' for binary, which never equals '0'
    base_files = set(_git(root, "ls-tree", "-r", "--name-only", base).splitlines())
    for line in status.splitlines():
        cols = line.split("\t")
        if len(cols) < 2:
            continue
        code, path = cols[0][:1], cols[-1]
        if code == "D":                     # on the branch, absent from main
            if path not in base_files:
                return False                # branch CREATED it — genuinely unpushed
        elif code == "A":                   # on main, absent from the branch
            if path in base_files:
                return False                # branch DELETED it — the branch's own work
        elif removed.get(path) != "0":      # modified: additions-only, or nothing doing
            return False
    return True


def sweep_branches(root: Path) -> list:
    """Local branches carrying commits that exist on NO remote — true unpushed-only
    work at risk of loss (abandoned feature branches). Read-only: flags, never deletes.

    The raw signal is `git rev-list --count <branch> --not --remotes`: commits reachable
    from the branch but from no remote-tracking ref. It correctly ignores
    pushed-but-unmerged branches (their commits are on `origin/<branch>`).

    **It does NOT, on its own, ignore the squash-merge case** — and this docstring used to
    claim it did, on the assumption that "a merged branch whose remote ref still exists is
    on a remote". That assumption is false here: PRs merge with `--delete-branch`, so the
    remote ref is gone the moment the PR lands. From then on the local branch's commits are
    on no remote and it flags as drift forever. Measured in practice: the large majority of
    flagged branches were false positives, which is how a long tail of stale local branches
    accumulates unnoticed. A drift report that is mostly noise gets ignored, and the real
    findings with it.

    So the raw signal is reconciled against **merged-PR head names** (`landed`). Neither
    ancestry nor patch-id works as that test — squash-merge rewrites SHAs, so a landed
    branch is never an ancestor of `main` and none of its commits has a patch-equivalent
    there (both were tried, and both wrongly called a merged branch unpreserved).

    Every branch is still returned; `landed` marks the ones whose work reached `main`, and
    `_stale_branches` drops those from the at-risk nudge. When `gh` is unavailable `landed`
    is `None` and behaviour falls back to the old un-reconciled signal.

    Excludes the current HEAD — that's reconcile()'s job.
    """
    current = current_branch(root)
    checked_out = _checked_out_branches(root)
    merged_heads = _merged_pr_heads(root)
    mig_rule = _migration_rule(root)
    fmt = "%(refname:short)\t%(committerdate:iso8601-strict)"
    raw = _git(root, "for-each-ref", "--format=" + fmt, "refs/heads")
    out = []
    for line in raw.splitlines()[:MAX_BRANCH_SCAN]:
        if "\t" not in line:
            continue
        name, cdate = line.split("\t", 1)
        # Skip the current HEAD (reconcile's job) and the trunk branches — main/master
        # are governed by the pull --ff-only discipline + the board's main push-nudge,
        # not the abandoned-feature-branch sweep.
        if not name or name == current or name in ("main", "master"):
            continue
        try:
            n = int(_git(root, "rev-list", "--count", name, "--not", "--remotes") or "0")
        except ValueError:
            n = 0
        if n <= 0:
            continue
        files = _git(root, "log", "--format=", "--name-only", name, "--not", "--remotes").splitlines()
        migs = sorted({f for f in files if _is_migration(f, mig_rule)})
        out.append({
            "branch": name,
            "idle_days": _idle_days(cdate),
            "unpushed_count": n,
            "unpushed_migrations": migs,
            "checked_out": name in checked_out,
            # True  → a PR with this head branch merged, so the work reached main (the
            #         local commits only LOOK unpushed because the remote ref was deleted).
            # False → no merged PR: genuinely at risk.
            # None  → `gh` unavailable; reconciliation was skipped, treat as unknown.
            # Tip-equality, never name-equality — see `_merged_pr_heads` and `_tip_merged`.
            # Three independent positive-evidence signals, cheapest first. Each can only
            # clear a branch; none can mark one at-risk that the raw signal did not.
            "landed": None if merged_heads is None else (
                _tip_merged(_git(root, "rev-parse", name).strip(), merged_heads)
                or _content_landed(root, name, files)
                or _main_strictly_newer(root, name, files)),
        })
    out.sort(key=lambda b: b["idle_days"], reverse=True)
    return out


def _stale_branches(root: Path, branches=None) -> list:
    """At-risk branches eligible for the passive nudge: idle >= threshold, not live in a
    worktree, and **not already landed via a merged PR**.

    The `landed` filter is what makes this list actionable — without it the nudge reported
    216 branches where 32 carried work that exists nowhere else (see `sweep_branches`).
    `landed is None` (gh unavailable) is NOT filtered: unknown-preservation is reported, so
    a missing `gh` degrades to the old noisier behaviour rather than hiding real risk.
    """
    branches = sweep_branches(root) if branches is None else branches
    thr = _stale_branch_days(root)
    return [b for b in branches
            if b["idle_days"] >= thr and not b["checked_out"] and not b.get("landed")]


# ---------------------------------------------------------------------------
# surfaces
# ---------------------------------------------------------------------------

def surface_line(root: Path) -> str:
    """One-liner for SessionStart aggregation. '' when nothing needs attention."""
    r = reconcile(root)
    parts = []
    if r["uncommitted_count"]:
        seg = f"{r['uncommitted_count']} uncommitted"
        if r["uncommitted_migrations"]:
            seg += f" ({len(r['uncommitted_migrations'])} migration)"
        parts.append(seg)
    if r["unpushed_count"]:
        seg = f"{r['unpushed_count']} unpushed commit(s) on {r['branch']}"
        if r["unpushed_migrations"]:
            seg += f" (incl. {len(r['unpushed_migrations'])} migration)"
        parts.append(seg)
    if r["open_items"]:
        oldest = max((it.get("_idle_days", 0) for it in r["open_items"]), default=0)
        seg = f"{len(r['open_items'])} open item(s)"
        if oldest:
            seg += f", oldest idle {oldest}d"
        parts.append(seg)
    stale = _stale_branches(root)
    if stale:
        parts.append(f"{len(stale)} stale branch(es) w/ unpushed work")
    if not parts:
        return ""
    return "⚠ Closeout: " + " · ".join(parts) + " → /coord:closeout check"


def _matches_item(item: dict, files: list) -> bool:
    pats = item.get("paths") or []
    if not pats:
        return False
    import fnmatch
    return any(fnmatch.fnmatch(f, pat) for f in files for pat in pats)


def check(root: Path) -> str:
    r = reconcile(root)
    branches = sweep_branches(root)
    out = ["═══ Closeout check ═══", f"branch: {r['branch'] or '(detached)'}"]

    if r["clean"] and not r["open_items"] and not branches:
        out.append("✓ Working tree clean, nothing unpushed, no open ledger items, no stray branches.")
        return "\n".join(out)

    # Uncommitted, bucketed. Configured bucket names first (in config order), "other" last —
    # the keys are no longer a fixed four (migration/source/docs/other): they come from the
    # `closeout_buckets` setting, so an unconfigured project just has "other".
    if r["uncommitted_count"]:
        out.append(f"\nUncommitted ({r['uncommitted_count']}):")
        kinds = [rule["name"] for rule in _bucket_rules(root)] + ["other"]
        for kind in kinds:
            files = r["buckets"].get(kind, [])
            if files:
                tag = "  ⚠ MIGRATION" if kind == "migration" else f"  {kind}"
                out.append(f"{tag} ({len(files)}):")
                out.extend(f"      {f}" for f in files[:20])
        if r["uncommitted_migrations"]:
            out.append("  → An uncommitted migration never reaches the shared DB. Commit + push it.")

    # Unpushed commits — unless the branch already landed via a merged PR.
    if r["unpushed_count"] and r.get("landed"):
        out.append(
            f"\nLanded via merged PR — {r['unpushed_count']} local commit(s) on "
            f"'{r['branch']}' are squashed into main."
        )
        out.append("  (The remote ref was auto-deleted on merge, so they read as unpushed.)")
        out.append(f"  → Nothing to push. Clean up: git branch -D {r['branch']}")
    elif r["unpushed_count"]:
        out.append(f"\nCommitted but NOT pushed ({r['unpushed_count']}) on '{r['branch']}':")
        out.extend(f"      {c['sha']}  {c['subject'][:70]}" for c in r["unpushed_commits"][:20])
        if r["unpushed_migrations"]:
            out.append(f"  ⚠ Includes {len(r['unpushed_migrations'])} migration(s) — unpushed schema change:")
            out.extend(f"      {f}" for f in r["unpushed_migrations"])
        out.append("  → Push the branch and open a PR to land it, so the work isn't local-only.")

    # Ledger cross-reference
    live_files = list(r["uncommitted"]) + list(r["unpushed_files"])
    if r["open_items"]:
        out.append("\nOpen ledger items:")
        for it in r["open_items"]:
            idle = it.get("_idle_days", 0)
            tied = _matches_item(it, live_files)
            note = ""
            if it.get("paths") and not tied:
                note = "  — no live changes on its paths; finished? mark /coord:closeout done, or /coord:closeout drop"
            out.append(f"   • [{it['id']}] {it.get('title','')}  ({it.get('status')}, idle {idle}d){note}")

    # Drift not attributable to any item
    untracked = [f for f in r["uncommitted"]
                 if not any(_matches_item(it, [f]) for it in r["open_items"])]
    if untracked and r["open_items"]:
        out.append(f"\n{len(untracked)} uncommitted file(s) not tied to any ledger item "
                   f"— consider /coord:closeout start to track this unit of work.")

    # Stale local-branch sweep — split by whether the work actually reached main.
    # Before the merged-PR reconciliation these were one undifferentiated list, which can
    # produce many times more flagged entries than real problems; the noise makes the whole
    # report unusable. Landed branches are cleanup, not risk — they must not share a bucket.
    if branches:
        thr = _stale_branch_days(root)
        landed = [b for b in branches if b.get("landed")]
        unknown = [b for b in branches if b.get("landed") is None]
        at_risk = [b for b in branches if b.get("landed") is False]

        def _line(b, mark=""):
            note = "  (checked out in a worktree)" if b["checked_out"] else ""
            mig = f", {len(b['unpushed_migrations'])} migration" if b["unpushed_migrations"] else ""
            return (f"   ⎇ {b['branch']}  ({b['unpushed_count']} local-only commit(s){mig}, "
                    f"idle {b['idle_days']}d){note}{mark}")

        if at_risk:
            out.append(f"\n⚠ Branches carrying work that exists NOWHERE ELSE ({len(at_risk)}):")
            for b in sorted(at_risk, key=lambda x: -x["unpushed_count"]):
                mark = "  ⚠ STALE" if (b["idle_days"] >= thr and not b["checked_out"]) else ""
                out.append(_line(b, mark))
            out.append("  → Nothing on main carries this work. Push the branch (open a PR) to "
                       "preserve it, or /coord:closeout drop <id> to log an intentional abandonment. "
                       "Read it before deleting — the sweep never deletes.")

        if unknown:
            out.append(f"\n? Preservation UNKNOWN — `gh` unavailable, so merged PRs could not "
                       f"be reconciled ({len(unknown)}). Treat as at-risk until checked.")
            for b in sorted(unknown, key=lambda x: -x["unpushed_count"])[:10]:
                out.append(_line(b))

        if landed:
            out.append(f"\n✓ Work is on main — safe to delete ({len(landed)}):")
            out.append(f"   {', '.join(sorted(b['branch'] for b in landed)[:8])}"
                       + (f"  … +{len(landed) - 8} more" if len(landed) > 8 else ""))
            out.append("  → Their commits only LOOK unpushed: the PR merged with "
                       "--delete-branch, so the remote ref is gone. `git branch -D <name>`.")

    return "\n".join(out)


def list_items(root: Path) -> str:
    items = load_ledger(root)
    out = ["═══ Closeout ledger ═══"]
    if not items:
        out.append("  (empty — /coord:closeout start \"<title>\" to add a work item)")
    for it in items:
        flag = {"open": "○", "committed": "◐", "pushed": "◑",
                "done": "✓", "dropped": "✗"}.get(it.get("status"), "?")
        line = f"  {flag} [{it['id']}] {it.get('title','')}  ({it.get('status')})"
        out.append(line)
        meta = []
        if it.get("branch"):
            meta.append(f"branch {it['branch']}")
        if it.get("planRef"):
            meta.append(f"plan {Path(it['planRef']).name}")
        if it.get("notes"):
            meta.append(it["notes"])
        if meta:
            out.append(f"        {' · '.join(meta)}")
    out.append("")
    out.append(surface_line(root) or "✓ No outstanding drift.")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# mutations
# ---------------------------------------------------------------------------

def start(root: Path, title: str, plan=None, paths=None) -> dict:
    items = load_ledger(root)
    item = {
        "id": _next_id(items),
        "title": title,
        "planRef": plan or _active_plan(root),
        "branch": current_branch(root),
        "status": "open",
        "paths": paths or [],
        "createdAt": _utcnow_iso(),
        "updatedAt": _utcnow_iso(),
        "notes": "",
    }
    items.append(item)
    save_ledger(root, items)
    return item


def _mirror_to_role_ledger(root: Path, item: dict, status: str, note) -> None:
    """Publish a dropped item into the owning discipline's durable ledger.

    THIS ledger is per-worktree and gitignored, so an intentional abandonment — "we are not
    doing this, and here is why" — currently dies with the checkout that recorded it. That is
    precisely the future-scoped intent a later session most needs and is least able to
    reconstruct: git records what was done, never what was deliberately not done.

    Only `dropped` is mirrored. Completed work is already in git and mirroring it would
    duplicate the derived channel; open items are still live here and would arrive as noise.
    Best-effort by design — a coordination convenience must never be able to fail a closeout.
    """
    if status != "dropped":
        return
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import role_ledger
        import roles as _roles
        paths = [p for p in (item.get("paths") or []) if p]
        # Resolve per path rather than via `disciplines_for`, which strips the companion
        # domains (docs, testing-infra, system-infra). Stripping is right for judging whether
        # a change is CROSS-discipline; it is wrong for attributing ownership — it would send
        # a dropped item living entirely under `scripts/` to nobody, silently, and companions
        # own real work here (system-infra alone carries 12 open debt items).
        owners = set()
        for p in paths:
            r = _roles.resolve(p)
            top = _roles.parent_of(r.get("domain") or "") if r else ""
            if top:
                owners.add(top)
        if not owners:
            return
        for owner in sorted(owners):
            role_ledger.record(
                owner, "queued", item.get("title", "") or "(untitled closeout item)",
                refs=paths[:5], item_id=f"closeout-{item.get('id', '')}",
                note=f"dropped from closeout: {note or 'no reason given'}")
    except Exception:
        # Deliberately swallowed: see the docstring. A failure here must not block a closeout.
        pass


def _set_status(root: Path, item_id: str, status: str, note=None):
    items = load_ledger(root)
    hit = None
    for it in items:
        if it.get("id") == item_id:
            it["status"] = status
            it["updatedAt"] = _utcnow_iso()
            if note is not None:
                it["notes"] = note
            hit = it
    if hit:
        save_ledger(root, items)
        _mirror_to_role_ledger(root, hit, status, note)
    return hit


def autoseed(root: Path):
    """Idempotent: create one open item for the current feature branch if it has
    drift and no existing open item. Called by SessionStart so the running list
    populates with zero discipline."""
    r = reconcile(root)
    branch = r["branch"]
    if not branch or branch in ("main", "HEAD", ""):
        return None
    if r["clean"]:
        return None
    for it in load_ledger(root):
        if it.get("branch") == branch and it.get("status") in OPEN_STATUSES:
            return None
    plan = _active_plan(root)
    item = start(root, _title_from_plan(plan) or branch, plan=plan)
    item = _set_status(root, item["id"], "open", note="auto-seeded on session start")
    return item


# ---------------------------------------------------------------------------
# Stop-hook backstop — stale-gated, deduped
# ---------------------------------------------------------------------------

def _actionable_unpushed(r: dict) -> int:
    """Unpushed commits that still need the author to DO something.

    A branch whose PR merged has no remote ref (this repo merges with --delete-branch) and
    squash rewrote its SHAs, so its commits read as unpushed forever. Those need no action,
    so they must not drive the Stop-hook nudge — otherwise the backstop nags about work that
    is already on main, which is exactly how a real warning gets tuned out.
    """
    return 0 if r.get("landed") else r["unpushed_count"]


def _fingerprint(r: dict) -> str:
    key = "|".join(sorted(r["uncommitted"])) + f"::{r['unpushed_count']}::{r['branch']}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _stop_reminder(r: dict) -> str:
    bits = []
    if r["uncommitted_count"]:
        m = len(r["uncommitted_migrations"])
        bits.append(f"{r['uncommitted_count']} uncommitted file(s)" + (f" incl. {m} migration" if m else ""))
    if _actionable_unpushed(r):
        m = len(r["unpushed_migrations"])
        bits.append(f"{r['unpushed_count']} committed-but-unpushed commit(s) on '{r['branch']}'"
                    + (f" incl. {m} migration" if m else ""))
    return ("⚠ Closeout reminder: " + "; ".join(bits) + " have sat idle across several turns. "
            "If this unit of work is complete, close it out now: commit it, "
            "push the branch / open a PR, or /coord:closeout drop <id> if intentionally abandoned. "
            "Details: /coord:closeout check.")


def stopcheck(root: Path) -> str:
    """Return a reminder string to emit, or '' . Manages the watch-file staleness
    counter + dedup so the same idle state is never re-warned."""
    r = reconcile(root)
    drift = r["uncommitted_count"] + _actionable_unpushed(r)
    wpath = _watch_path(root)
    w = _load_json(wpath) or {}

    if drift == 0:                       # clean / changed -> reset, stay quiet
        if w:
            _save_json(wpath, {})
        return ""

    fp = _fingerprint(r)
    if w.get("fingerprint") == fp:
        w["stale_turns"] = int(w.get("stale_turns", 0)) + 1
    else:
        w["fingerprint"] = fp
        w["stale_turns"] = 1

    threshold = STALE_TURNS_HIGH_STAKES if (_actionable_unpushed(r) or r["unpushed_migrations"]) \
        else STALE_TURNS_DEFAULT
    fire = w["stale_turns"] >= threshold and w.get("last_emitted_fingerprint") != fp

    msg = ""
    if fire:
        msg = _stop_reminder(r)
        w["last_emitted_fingerprint"] = fp
        w["last_emitted_turn"] = w["stale_turns"]
    _save_json(wpath, w)
    return msg


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Closeout hygiene tracker")
    ap.add_argument("command", nargs="?", default="check",
                    choices=["check", "list", "branches", "start", "done", "drop",
                             "autoseed", "stopcheck"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--plan", default=None)
    ap.add_argument("--paths", nargs="*", default=None)
    ap.add_argument("--surface", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    root = _repo_root()

    if a.surface:
        line = surface_line(root)
        if line:
            print(line)
        return 0
    if a.json:
        print(json.dumps({"reconcile": reconcile(root), "ledger": load_ledger(root),
                          "branches": sweep_branches(root)},
                         indent=2, default=str))
        return 0

    cmd = a.command
    if cmd == "check":
        print(check(root))
    elif cmd == "list":
        print(list_items(root))
    elif cmd == "branches":
        bs = sweep_branches(root)
        if not bs:
            print("✓ No local branches with unpushed-only commits.")
        else:
            # Uses the SAME landed/unknown/at-risk split as `check`. It previously printed one
            # undifferentiated list captioned "on no remote = at risk", which made the
            # dedicated branch command strictly less informative than the incidental one — and
            # meant a reader could not tell a reconciled result from an unreconciled one. That
            # gap between the unreconciled count and the true at-risk count can be large.
            thr = _stale_branch_days(root)
            landed = [b for b in bs if b.get("landed")]
            unknown = [b for b in bs if b.get("landed") is None]
            at_risk = [b for b in bs if b.get("landed") is False]

            def _row(b, mark=""):
                note = "  (checked out)" if b["checked_out"] else ""
                mig = (f", {len(b['unpushed_migrations'])} migration"
                       if b["unpushed_migrations"] else "")
                return (f"  ⎇ {b['branch']}  ({b['unpushed_count']} local-only{mig}, "
                        f"idle {b['idle_days']}d){note}{mark}")

            if at_risk:
                print(f"⚠ Work that exists NOWHERE ELSE ({len(at_risk)}):")
                for b in sorted(at_risk, key=lambda x: -x["unpushed_count"]):
                    mark = ("  ⚠ STALE" if (b["idle_days"] >= thr and not b["checked_out"])
                            else "")
                    print(_row(b, mark))
                print("  → Nothing on main carries this. Push the branch (open a PR) to "
                      "preserve it, or /coord:closeout drop <id> to log an intentional abandonment.")

            if unknown:
                print(f"\n? Preservation UNKNOWN ({len(unknown)}) — `gh` could not list merged "
                      "PRs, so NOTHING below was reconciled. This is not a result: treat every "
                      "entry as unverified, and re-run once `gh auth status` is clean.")
                for b in sorted(unknown, key=lambda x: -x["unpushed_count"]):
                    print(_row(b))

            if landed:
                print(f"\n✓ Work is on main — safe to delete ({len(landed)}):")
                print(f"  {', '.join(sorted(b['branch'] for b in landed))}")
    elif cmd == "start":
        if not a.args:
            print("usage: start \"<title>\" [--plan F] [--paths A B ...]"); return 2
        it = start(root, " ".join(a.args), plan=a.plan, paths=a.paths)
        print(f"added [{it['id']}] {it['title']}  (open, branch {it['branch']})")
    elif cmd == "done":
        if not a.args:
            print("usage: done <id>"); return 2
        it = _set_status(root, a.args[0], "done")
        print(f"marked done: {it['id']}" if it else f"no item {a.args[0]}")
    elif cmd == "drop":
        if not a.args:
            print("usage: drop <id> \"<reason>\""); return 2
        reason = " ".join(a.args[1:]) or "dropped"
        it = _set_status(root, a.args[0], "dropped", note=f"dropped: {reason}")
        print(f"dropped {it['id']} — {reason}" if it else f"no item {a.args[0]}")
    elif cmd == "autoseed":
        it = autoseed(root)
        print(f"seeded [{it['id']}] {it['title']}" if it else "(no seed needed)")
    elif cmd == "stopcheck":
        msg = stopcheck(root)
        if msg:
            print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
