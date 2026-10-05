#!/usr/bin/env python3
"""blocker_owner.py — "who is responsible for what is blocking me, and are they still here?"

When a session hits a roadblock that is not its own — a file locked by a peer, a
cross-worktree collision on the same file, uncommitted work it did not write — it needs
three answers in order: WHO owns it, are they ALIVE, and therefore may I ping them or
adopt what they left. Every one of those answers already existed somewhere in this repo,
in four different files, and two of the three blocker types answered none of them.

`lock_guard.py` (via `coord_locks`) is the reference implementation and the reason this
module has a shape to copy: it resolves the lock's owner, checks liveness, and then either
blocks and routes you to `/coord:ask-lock` naming the human, or — if the owner's process is
gone — reclaims the lock and proceeds. Identify -> liveness -> ping-or-inherit, already
shipped, for exactly one blocker type. This generalises it.

The authority boundary
----------------------
A session must not absorb a peer's work: a session that absorbs a peer's work holds two
objectives and will rightly prioritise its own, so the absorbed one loses the only agent
advocating for it.

Inheritance does not contradict that principle, it depends on it. The principle protects a LIVE
peer's advocacy. A dead session has no advocate, so the harm cannot occur — which makes
liveness the authority boundary, not merely an optimisation to avoid dead letters. Two
consequences, both load-bearing:

  - `unknown` liveness is treated as ALIVE everywhere. Inheriting a live session's work is
    the only outcome worse than staying blocked, so uncertainty must never resolve toward
    adoption. `coord_locks.liveness()` returns three states for this reason.
  - Reversibility still gates auto-action INDEPENDENTLY of liveness. Inheritance changes
    WHO MAY ACT, never WHAT MAY BE AUTO-ACTED. Adopting an orphaned ledger item is
    reversible and automatic; committing a dead session's WIP or deleting their branch is
    not, and goes to the operator regardless of how certainly dead they are.

Every verdict carries `evidence` naming the source that answered it. An owner asserted
without provenance is precisely the failure this repo keeps re-learning.

Usage
-----
    python3 scripts/blocker_owner.py --path <p> [<p> ...]   # who owns these files
    python3 scripts/blocker_owner.py --branch <b>           # who owns this branch
    python3 scripts/blocker_owner.py --dirty                # who owns uncommitted work
    python3 scripts/blocker_owner.py --dirty --json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import coord_locks  # noqa: E402

try:
    import _agent_channel  # noqa: E402
except ImportError:  # pragma: no cover
    _agent_channel = None

# Actions the caller may take. Deliberately a closed vocabulary: a caller that cannot map
# a verdict to one of these has hit a case this module has not thought about, and should
# surface that rather than improvise.
ACT_PING = "ping"                    # owner alive -> ask them, act on nothing
ACT_INHERIT = "inherit"              # owner dead, transfer is reversible -> auto, logged
ACT_PROPOSE = "propose-to-operator"  # owner dead, transfer is NOT reversible -> ask user
ACT_NONE = "none"                    # not blocked, or the blocker is mine
ACT_ROUTE = "route-to-role"          # nobody owns it — address the DISCIPLINE, not a session


def _registry() -> dict:
    if _agent_channel is None:
        return {}
    try:
        p = _agent_channel.channel_dir() / "sessions-registry.json"
        return json.loads(p.read_text()).get("sessions", {})
    except (OSError, ValueError):
        return {}


def human_name(sid: str) -> str:
    """A name a person can act on. Falls back to the id rather than inventing one."""
    rec = _registry().get(sid) or {}
    return rec.get("humanName") or sid


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True,
                              check=False).stdout.strip()
    except OSError:
        return ""


def _verdict(sid: str, evidence: str, *, reversible: bool, blocker: str,
             detail: str = "") -> dict:
    """Assemble owner + liveness + the action that combination authorises."""
    live = coord_locks.liveness(sid)
    if live["state"] == coord_locks.LIVE_DEAD:
        action = ACT_INHERIT if reversible else ACT_PROPOSE
    else:
        # alive OR unknown. Unknown fails toward alive on purpose (see module docstring).
        action = ACT_PING
    return {
        "blocker": blocker,
        "detail": detail,
        "sessionId": sid,
        "humanName": human_name(sid),
        "liveness": live["state"],
        "livenessReason": live["reason"],
        "evidence": evidence,
        "reversible": reversible,
        "recommendedAction": action,
    }


# ------------------------------------------------------------------ blocker: file lock

def owner_of_path(path: str) -> dict | None:
    """Who holds the same-checkout lock on this file, if anyone.

    Reversible: a lock is a claim, not work. Transferring it destroys nothing, which is
    why `lock_guard.py` already reclaims dead owners' locks unattended.
    """
    meta = coord_locks.read_lock(path)
    if not meta:
        return None
    sid = meta.get("sessionId", "")
    if not sid or sid == coord_locks.session_id():
        return None  # unheld, or mine
    return _verdict(sid, f"lock file {coord_locks.lock_path(path).name}",
                    reversible=True, blocker="file-lock", detail=path)


# ------------------------------------------- blocker: cross-worktree collision / branch

def owner_of_branch(branch: str) -> dict | None:
    """Which session is working on this branch (hence this worktree).

    NOT reversible: the remedy touches another session's git state — committing their
    work, rebasing, deleting a branch. Even a certainly-dead owner routes to the operator.
    """
    for sid, rec in _registry().items():
        if rec.get("branch") == branch and sid != coord_locks.session_id():
            return _verdict(sid, f"sessions-registry branch=={branch}",
                            reversible=False, blocker="worktree-collision",
                            detail=branch)
    return None


# ------------------------------------------------------- blocker: stale uncommitted work

def owners_of_dirty(paths: list[str] | None = None) -> list[dict]:
    """Attribute uncommitted files to the sessions holding locks on them.

    Ownership comes from `coord_locks.locked_files()` — the lock files on DISK — never
    from a manifest's `lockedFiles` array, which is lossy in both directions and made the
    worktree gate report a just-edited file as somebody else's.

    NOT reversible: committing or discarding work someone else wrote is unrecoverable if
    the attribution is wrong, and attribution is exactly the part that has been unreliable.
    """
    dirty = paths if paths is not None else [
        line[3:].split(" -> ")[-1]
        for line in _git("status", "--porcelain").splitlines() if line.strip()
    ]
    if not dirty:
        return []

    by_session: dict[str, list[str]] = {}
    me = coord_locks.session_id()
    for meta in coord_locks.list_locks():
        sid = meta.get("sessionId", "")
        f = meta.get("file")
        if sid and sid != me and f in dirty:
            by_session.setdefault(sid, []).append(f)

    return [
        _verdict(sid, f"{len(files)} lock file(s) on disk", reversible=False,
                 blocker="stale-uncommitted-work", detail=", ".join(sorted(files)[:6]))
        for sid, files in sorted(by_session.items())
    ]


def role_route(path: str) -> dict:
    """Where to address a blocker that NO session owns.

    The gap this closes: `resolve()` returns nothing when nobody holds a lock, which is the
    common case — someone must edit a file whose discipline has no live representative. The
    honest answer is not "unowned, proceed" but "no session owns it; address the
    DISCIPLINE", because the role outlives every session that ever represents it.

    That is what `topic:<role>` was always for. It had zero producers and zero consumers
    until now — a durable inbox nobody wrote to and nobody read, so work could only ever be
    addressed to a particular session that might be gone within the hour.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import roles as _roles
        hit = _roles.resolve(path)
    except Exception:
        return {}
    if not hit or not hit.get("role"):
        return {}
    return {
        "blocker": "unowned-path",
        "detail": path,
        "role": hit["role"],
        "domain": hit["domain"],
        "topic": f"topic:{hit['role']}",
        "evidence": f"role ownership {hit['kind']} glob {hit['glob']}",
        "recommendedAction": ACT_ROUTE,
    }


def resolve(*, paths: list[str] | None = None, branch: str | None = None,
            dirty: bool = False) -> list[dict]:
    out = []
    for p in paths or []:
        v = owner_of_path(p)
        if v:
            out.append(v)
        else:
            # No session holds it. Do not report "unblocked" — name the discipline that
            # does, so the request reaches a durable inbox instead of nobody.
            r = role_route(p)
            if r:
                out.append(r)
    if branch:
        v = owner_of_branch(branch)
        if v:
            out.append(v)
    if dirty:
        out.extend(owners_of_dirty())
    return out


_ACTION_HELP = {
    ACT_PING: "owner is reachable — ask them, do not act on their state:\n"
              "     /coord:ask-lock <path> \"<why>\"   or   /coord:msg <peer> \"<what you need>\"",
    ACT_INHERIT: "owner is gone and the transfer is reversible — safe to adopt.",
    ACT_PROPOSE: "owner is gone but the remedy is NOT reversible (their commits, branch,\n"
                 "     or uncommitted work). Surface it to your operator; do not act.",
    ACT_ROUTE: "no SESSION owns this, but a discipline does. Address the role, whose inbox\n"
               "     outlives any session:\n"
               "     python3 scripts/agent_message.py send <topic> REQUEST --intent handoff \\\n"
               "         --subject \"...\" --body \"...\"",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--path", nargs="+", default=[], help="file path(s) blocking you")
    ap.add_argument("--branch", help="branch whose owner you want")
    ap.add_argument("--dirty", action="store_true",
                    help="attribute the uncommitted working tree")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not (args.path or args.branch or args.dirty):
        ap.print_help()
        return 0

    rows = resolve(paths=args.path, branch=args.branch, dirty=args.dirty)

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    if not rows:
        print("No peer owns what you named — nothing blocking you here.")
        return 0

    for r in rows:
        print(f"\n{r['blocker']}: {r['detail'][:88]}")
        if r["recommendedAction"] == ACT_ROUTE:
            # A role route has no session to name — that IS the finding.
            print(f"  owner    (no session) — discipline `{r['role']}` owns it")
            print(f"  address  {r['topic']}")
        else:
            print(f"  owner    {r['humanName']}  [{r['sessionId'][:12]}]")
            print(f"  liveness {r['liveness']} — {r['livenessReason']}")
        print(f"  evidence {r['evidence']}")
        print(f"  action   {r['recommendedAction']}")
        help_text = _ACTION_HELP.get(r["recommendedAction"])
        if help_text:
            print(f"     {help_text}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
