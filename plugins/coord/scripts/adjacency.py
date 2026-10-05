#!/usr/bin/env python3
"""adjacency.py — tell a peer when you are both editing the same code, before merge day.

Why a separate tool from worktree_overlap.py
--------------------------------------------
Locks are worktree-scoped, so across worktrees there is no lock at all — the real collision
happens at MERGE. `worktree_overlap.py` already finds shared files, but its raw output is
not something you can message anyone about:

  - It compared against LOCAL main, which was 255 commits stale, so every branch's diff also
    contained everything that had landed on main since. This worktree reported 1675 changed
    files where it had genuinely changed 25. (Fixed in worktree_overlap.py: it now prefers
    origin/main.)
  - Even corrected, most "shared" files are generated artifacts every branch rewrites:
    .claude/state/last-green-build.txt appeared in 210 pair-intersections, geometry report
    JSON in 15 each. Messaging on those would bury the one real signal — measured over 29
    branches, only 67 distinct files are shared at all, and the interesting one is
    MyApp/Tools/Core/PlannerFrontierSelector.swift, co-edited by 15 branches.

So this module's job is SUBTRACTION: strip churn until what remains is worth interrupting a
peer about. An adjacency warning that is mostly artifacts is worse than none — it is how the
last generation of this harness trained everyone to ignore it.

Churn is filtered two ways: known generated/state paths, and a breadth heuristic (a file
touched by more than BREADTH_FRACTION of active branches is infrastructure, not a
collision). Files named in the `additive_files` setting (e.g. a project file every branch
touches) are called out separately — they are real contention but merge mechanically, so
they need different advice.

Usage
-----
    python3 scripts/adjacency.py                 # what my branch collides with
    python3 scripts/adjacency.py --all           # every pair, ranked
    python3 scripts/adjacency.py --notify        # message the peers I collide with (deduped)
    python3 scripts/adjacency.py --json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _agent_channel  # noqa: E402
import coord_config  # noqa: E402
import worktree_overlap as wo  # noqa: E402

try:
    import agent_message  # noqa: E402
except ImportError:  # pragma: no cover
    agent_message = None

try:
    import blocker_owner as _blocker_owner  # noqa: E402  — liveness gate before pinging
except ImportError:  # pragma: no cover
    _blocker_owner = None

# Generated, machine-written, or per-run state that THIS plugin itself creates — co-editing
# these is not a collision regardless of what project is being coordinated. Project-specific
# churn (a docs/report dir, a baseline-file convention, …) is NOT built in; it comes from the
# `churn_globs` setting (default empty — the app this shipped from had several, none of which
# generalize) and is ADDED to this list, never replaces it.
BUILTIN_CHURN_PATTERNS = (
    r"^\.claude/state/",
    r"^\.claude/coordination/",
)
# Real contention, but it has its own serialization — advise differently. Filename suffixes
# from the `additive_files` setting (default empty), e.g. an Xcode `project.pbxproj`.
BREADTH_FRACTION = 0.5
MIN_BRANCHES_FOR_BREADTH = 6   # below this the fraction is statistically meaningless

STATE = ".claude/coordination/adjacency-sent.json"


def churn_patterns() -> tuple:
    return BUILTIN_CHURN_PATTERNS + tuple(coord_config.get("churn_globs"))


def is_churn(path: str) -> bool:
    return any(re.search(p, path) for p in churn_patterns())


def is_additive(path: str) -> bool:
    suffixes = tuple(coord_config.get("additive_files"))
    return bool(suffixes) and path.endswith(suffixes)


def collect() -> dict:
    """Per-branch changed files, churn-filtered, plus the branch->worktree map."""
    entries = wo.worktrees()
    per: dict[str, set[str]] = {}
    meta: dict[str, dict] = {}
    for e in entries:
        br, path = e.get("branch", ""), e.get("path", "") or e.get("worktree", "")
        if not br:
            continue
        files = {f for f in wo.changed_files(br, path) if not is_churn(f)}
        per[br] = files
        meta[br] = {"path": path}

    # Breadth filter: drop files that most active branches touch.
    if len(per) >= MIN_BRANCHES_FOR_BREADTH:
        counts: dict[str, int] = {}
        for files in per.values():
            for f in files:
                counts[f] = counts.get(f, 0) + 1
        cutoff = max(2, int(len(per) * BREADTH_FRACTION))
        wide = {f for f, c in counts.items() if c > cutoff}
        for br in per:
            per[br] -= wide
    return {"perBranch": per, "meta": meta}


def pairs(per: dict[str, set[str]]) -> list[dict]:
    out = []
    names = sorted(per)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            shared = per[a] & per[b]
            if not shared:
                continue
            special = sorted(f for f in shared if is_additive(f))
            real = sorted(f for f in shared if not is_additive(f))
            if not real and not special:
                continue
            out.append({"a": a, "b": b, "files": real, "special": special,
                        "count": len(real)})
    out.sort(key=lambda r: -r["count"])
    return out


def _registry() -> dict:
    try:
        p = _agent_channel.channel_dir() / "sessions-registry.json"
        return json.loads(p.read_text()).get("sessions", {})
    except (OSError, ValueError):
        return {}


def session_for_branch(branch: str) -> dict | None:
    for sid, rec in _registry().items():
        if rec.get("branch") == branch:
            return dict(rec, sessionId=sid)
    return None


def _sent_state() -> dict:
    try:
        return json.loads(Path(STATE).read_text())
    except (OSError, ValueError):
        return {}


def _save_sent(state: dict) -> None:
    try:
        Path(STATE).parent.mkdir(parents=True, exist_ok=True)
        Path(STATE).write_text(json.dumps(state, indent=2))
    except OSError:
        pass


def fingerprint(pair: dict) -> str:
    key = f"{pair['a']}|{pair['b']}|" + "|".join(sorted(pair["files"]))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def notify(my_branch: str, rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Message each LIVE peer I newly collide with. Deduped by (pair, file-set).

    Returns (sent, orphaned). `orphaned` are collisions whose owning session is gone —
    reported to the caller instead of messaged, because writing to a dead session's
    mailbox produces a message that is delivered to nobody, forever, with no error on
    either side. That failure is silent in both directions and is exactly what made the
    peer-to-peer channel unreliable: the durable mailbox exists so a
    request survives the recipient being busy, which is worthless if it also silently
    absorbs requests to recipients who no longer exist.

    A branch collision is never auto-resolved even when the owner is certainly dead —
    the remedy touches their git state, which is not reversible. It routes to the
    operator via `blocker_owner.ACT_PROPOSE`.
    """
    if agent_message is None:
        return [], []
    state = _sent_state()
    sent, orphaned = [], []
    for r in rows:
        other = r["b"] if r["a"] == my_branch else r["a"]
        fp = fingerprint(r)
        if state.get(other) == fp:
            continue  # same collision already announced; silence is correct
        peer = session_for_branch(other)
        if not peer:
            continue  # nobody registered on that branch — nothing to wake
        # Liveness gates the ping. Not deduped: an unresolved orphan stays visible every
        # run, because unlike a delivered message nobody else is holding it.
        if _blocker_owner is not None:
            verdict = _blocker_owner.owner_of_branch(other)
            if verdict and verdict["liveness"] == "dead":
                orphaned.append({**verdict, "files": r["files"]})
                continue
        shown = r["files"][:8]
        more = len(r["files"]) - len(shown)
        body = (
            f"Our branches both changed {len(r['files'])} file(s). These will conflict at "
            f"merge unless we sequence them.\n\n  mine:  {my_branch}\n  yours: {other}\n\n"
            + "\n".join(f"  - {f}" for f in shown)
            + (f"\n  … and {more} more" if more > 0 else "")
            + "\n\nNo action needed if you are landing first — tell me and I will rebase "
              "onto your work. If I should land first, say so and I will go now."
        )
        msg = agent_message.send(
            f"session:{peer['sessionId']}", "FYI", intent="adjacency",
            subject=f"branch adjacency: {len(r['files'])} shared file(s) with {my_branch}",
            body=body, files=r["files"][:20], branch=other,
        )
        state[other] = fp
        sent.append({"peer": peer.get("humanName") or peer["sessionId"],
                     "branch": other, "msgId": msg["msgId"],
                     "doorbell": agent_message.doorbell_for(msg)})
    _save_sent(state)
    return sent, orphaned


def main() -> int:
    ap = argparse.ArgumentParser(description="Cross-worktree merge-collision early warning.")
    ap.add_argument("--all", action="store_true", help="every pair, not just mine")
    ap.add_argument("--notify", action="store_true", help="message the peers I collide with")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    data = collect()
    rows = pairs(data["perBranch"])
    my_branch = wo._git("rev-parse", "--abbrev-ref", "HEAD").strip()
    mine = [r for r in rows if my_branch in (r["a"], r["b"])]

    if a.json:
        print(json.dumps({"branch": my_branch, "mine": mine,
                          "all": rows if a.all else []}, indent=2))
        return 0

    show = rows if a.all else mine
    label = "all branch pairs" if a.all else f"collisions for {my_branch}"
    if not show:
        print(f"{label}: none — no shared source files after filtering generated artifacts.")
        return 0

    print(f"{label}: {len(show)}\n")
    for r in show[:15]:
        other = r["b"] if r["a"] == my_branch else r["a"]
        who = session_for_branch(other) if not a.all else None
        tag = ""
        if who:
            tag = f"  [{who.get('humanName') or who['sessionId']}]"
        print(f"  {r['count']:>3} file(s)  {r['a']}  <->  {r['b']}{tag}")
        for f in r["files"][:5]:
            print(f"            {f}")
        if r["count"] > 5:
            print(f"            … and {r['count'] - 5} more")
        if r["special"]:
            print(f"            ! {', '.join(r['special'])} — additive file: expect it in "
                  f"both branches and merge it mechanically")
        print()

    if a.notify:
        sent, orphaned = notify(my_branch, mine)
        if not sent and not orphaned:
            print("notify: nothing new to announce (already sent, or no peer registered "
                  "on those branches).")
        if sent:
            print(f"notify: messaged {len(sent)} live peer(s).")
            for s in sent:
                print(f"  {s['peer']} ({s['branch']}) — {s['msgId']}")
            print("\nRing the doorbell for each — call SendMessage with:")
            for s in sent:
                if s["doorbell"]:
                    print(json.dumps(s["doorbell"], indent=2))
        if orphaned:
            print(f"\nNOT messaged — {len(orphaned)} collision(s) whose owner is gone:")
            for o in orphaned:
                print(f"  {o['humanName']} ({o['detail']}) — {o['livenessReason']}")
                print(f"    {len(o['files'])} shared file(s); evidence: {o['evidence']}")
            print("\n  A message to a dead session is delivered to nobody, forever, with")
            print("  no error either side — so these are reported, not sent.")
            print("  Their branch is NOT yours to resolve unattended: rebasing, landing,")
            print("  or deleting another session's work is irreversible. Surface to your")
            print("  operator. Confirm the branch is genuinely abandoned first:")
            print("     /coord:closeout branches      # at-risk vs landed, reconciled with merged PRs")
    elif mine:
        print("Run with --notify to tell those peers (deduped — a repeat of the same "
              "collision is not re-sent).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
