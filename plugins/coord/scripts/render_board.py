#!/usr/bin/env python3
"""
render_board.py — Coordination Harness v2, Component 4. The central info board.

Composes ONE human-readable view of all parallel-session activity from the
existing coordination data (sessions/, locks/, events.log) plus the v2 additions
(and, when present, a build-slot queue, pending-verification and last-green-build
files). This is what `/coord:board` prints and what the SessionStart hook surfaces to every new agent
so nobody has to ask "who's working on what".

Usage:
    python3 scripts/render_board.py            # plain-text board
    python3 scripts/render_board.py --json     # machine-readable

Best-effort: every data source is optional. Missing files render as empty
sections, never errors. Opportunistically rotates events.log first.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coord_config  # noqa: E402


def _push_divergence() -> dict:
    """Commits on local main not yet on origin/main. Best-effort.

    Surfaces a real failure mode: broken work
    accumulated in local commits that were never pushed, so CI (which only
    runs on push/PR) never gated it. The board nudges agents to push so CI
    actually runs.
    """
    root = str(_repo_root())
    try:
        ahead = subprocess.run(
            ["git", "-C", root, "rev-list", "--count", "origin/main..main"],
            capture_output=True, text=True, timeout=5,
        )
        branch = subprocess.run(
            ["git", "-C", root, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if ahead.returncode != 0:
            return {"ahead": None, "branch": branch.stdout.strip()}
        return {"ahead": int(ahead.stdout.strip() or 0), "branch": branch.stdout.strip()}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"ahead": None, "branch": "?"}


def _repo_root() -> Path:
    return coord_config.project_root()


def _script_dir() -> Path:
    return Path(__file__).resolve().parent


def _coord() -> Path:
    return _repo_root() / ".claude" / "coordination"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _age_str(iso: str) -> str:
    t = coord_config.parse_utc(iso)
    if t is None:
        return "?"
    secs = (_now() - t).total_seconds()
    if secs < 90:
        return f"{int(secs)}s"
    if secs < 5400:
        return f"{int(secs / 60)}m"
    if secs < 172800:
        return f"{secs / 3600:.1f}h"
    return f"{secs / 86400:.1f}d"


def _age_seconds(iso: str) -> float:
    t = coord_config.parse_utc(iso)
    if t is None:
        return float("inf")
    return (_now() - t).total_seconds()


# Two distinct thresholds, neither of which alone reaps anything (reaping lives
# in reap_coordination.py and is liveness-gated):
#   IDLE_BADGE_SECONDS — cosmetic "·idle" hint on the board (default 1h).
#   REAP_SECONDS       — heartbeat-age fallback used to flag a NON-LIVE session
#                        as a reap candidate (default 24h; matches
#                        reap_coordination + COORD_STALE_SECONDS).
# A session with a live PID is shown active no matter how long idle.
IDLE_BADGE_SECONDS = coord_config.get("idle_badge_seconds")
REAP_SECONDS = coord_config.get("stale_seconds")


def _pid_alive(pid: int) -> bool:
    """Same-machine process liveness; mirrors reap_coordination._pid_alive."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _interfaces() -> dict:
    """Cross-worktree interface state from the SHARED channel ($CHANNEL, git-common-dir):
    the latest overlap-report.json snapshot + ratified-contract count. Best-effort — the
    feature may not be set up; absence renders nothing. (Interface Coordination, Phase 2.)"""
    summary = {"available": False, "divergent": 0, "coModify": 0, "definerDependent": 0,
               "contracts": 0, "top": [], "generatedAt": ""}
    try:
        import sys as _sys
        _sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import _agent_channel  # noqa: E402
        ch = _agent_channel.channel_dir()
    except Exception:
        return summary
    report = ch / "overlap-report.json"
    data = _load(report)
    if isinstance(data, dict):
        dd = data.get("defineDefine", [])
        summary["available"] = True
        summary["generatedAt"] = data.get("generatedAt", "")
        summary["divergent"] = sum(1 for x in dd if x.get("severity") == "divergent")
        summary["coModify"] = sum(1 for x in dd if x.get("severity") != "divergent")
        summary["definerDependent"] = len(data.get("definerDependent", []))
        # a few highest-priority items (divergent first) for the board
        for x in sorted(dd, key=lambda r: 0 if r.get("severity") == "divergent" else 1)[:4]:
            summary["top"].append(
                f"{'‼️' if x.get('severity') == 'divergent' else '•'} "
                f"[{x.get('kind')}] {x.get('interfaceId')} — {len(x.get('participants', []))} wts"
            )
    try:
        cdir = ch / "contracts"
        if cdir.is_dir():
            summary["contracts"] = len(list(cdir.glob("*.json")))
            summary["available"] = True
    except OSError:
        pass
    return summary


def collect() -> dict:
    coord = _coord()
    sessions_dir = coord / "sessions"
    locks_dir = coord / "locks"

    # Locks held per session, counted from the lock files ON DISK. The manifest's
    # `lockedFiles` array is lossy in both directions (the edit guard's already-locked
    # fast path returns before appending; reap_coordination resets it wholesale), so a
    # session holding real locks can show lockCount 0 on the board — which reads as
    # "claiming nothing" when it is in fact holding several.
    locks_by_session: dict[str, int] = {}
    if locks_dir.is_dir():
        for _lf in locks_dir.glob("*.lock"):
            _m = _load(_lf)
            if _m and _m.get("sessionId"):
                locks_by_session[_m["sessionId"]] = locks_by_session.get(_m["sessionId"], 0) + 1

    # --- sessions ---
    sessions = []
    active_ids = set()
    if sessions_dir.is_dir():
        for p in sorted(sessions_dir.glob("*.json")):
            d = _load(p)
            if d is None:
                continue
            active_ids.add(p.stem)
            hb = d.get("lastHeartbeat", "")
            age = _age_seconds(hb)
            alive = _pid_alive(int(d.get("pid", 0) or 0))
            sessions.append({
                "id": p.stem,
                "shortId": p.stem[:8],
                "humanName": d.get("humanName") or d.get("description", "")[:40],
                "domain": d.get("domain", "?"),
                "planFile": Path(d.get("planFile", "")).name if d.get("planFile") else "",
                "taskIds": d.get("taskIds", []),
                "activity": d.get("activity", ""),
                "heartbeatAge": _age_str(hb),
                "alive": alive,
                "idle": age > IDLE_BADGE_SECONDS,
                # Reap candidate ONLY if not live AND past the fallback age.
                # A live PID is never stale, regardless of idle time.
                "stale": (not alive) and age > REAP_SECONDS,
                "lockCount": locks_by_session.get(p.stem, 0),
            })

    # --- locks (orphans = owning session absent) ---
    orphan_locks = 0
    lock_total = 0
    if locks_dir.is_dir():
        for lf in locks_dir.glob("*.lock"):
            lock_total += 1
            meta = _load(lf)
            if meta is None or meta.get("sessionId") not in active_ids:
                # __named__ advisory locks (e.g. a shared project file or a build lock)
                # have no session manifest but ARE legitimate; only count file-locks
                # as orphans.
                if not lf.name.startswith("__"):
                    orphan_locks += 1

    # --- global build cap + FIFO queue (scripts/_build_semaphore.py) ---
    # Snapshot the N-slot semaphore: who is building now (active, holding a slot)
    # and who is next in line (waiting, FIFO-ordered). Read-only snapshot — no
    # reaping here (that happens during real admission), so the board never
    # contends on the admit-lock.
    build = {"available": False, "max": 2, "active": [], "waiting": []}
    try:
        # _build_semaphore ships (if at all) in a sibling plugin, not this one — look
        # next to THIS script, not under the project root. Degrades silently when absent.
        sys.path.insert(0, str(_script_dir()))
        import _build_semaphore  # noqa: E402
        max_n = _build_semaphore.resolve_max(None)
        snap = _build_semaphore._snapshot(max_n)
        build = {
            "available": True,
            "max": max_n,
            "active": [{"shortId": t.get("sessionId", "?")[:8], "worktree": t.get("worktree", "?"),
                        "age": _age_str(t.get("admittedAt") or t.get("enqueuedAt", ""))}
                       for t in snap["active"]],
            "waiting": [{"shortId": t.get("sessionId", "?")[:8], "worktree": t.get("worktree", "?"),
                         "age": _age_str(t.get("enqueuedAt", ""))}
                        for t in snap["waiting"]],
        }
    except Exception:
        pass

    # --- pending V&V ---
    vnv = {"pending": 0, "oldest": None}
    vnv_data = _load(coord / "pending-vnv.json")
    if isinstance(vnv_data, list):
        pend = [e for e in vnv_data if e.get("status") == "pending"]
        vnv["pending"] = len(pend)
        if pend:
            vnv["oldest"] = _age_str(pend[0].get("committedAt", ""))

    # --- last green build ---
    last_green = None
    lg = _repo_root() / ".claude" / "state" / "last-green-build.txt"
    if lg.is_file():
        try:
            parts = lg.read_text().splitlines()
            last_green = {
                "sha": (parts[0][:8] if parts else "?"),
                "age": _age_str(parts[1]) if len(parts) > 1 else "?",
                "note": parts[2] if len(parts) > 2 else "",
            }
        except OSError:
            pass

    # --- closeout hygiene (uncommitted / unpushed drift + ledger items + stale branches) ---
    closeout = None
    closeout_branches = []
    try:
        # closeout_ledger ships alongside this script, not under the project root.
        sys.path.insert(0, str(_script_dir()))
        import closeout_ledger  # noqa: E402
        closeout = closeout_ledger.reconcile(_repo_root())
        closeout_branches = closeout_ledger._stale_branches(_repo_root())
    except Exception:
        pass

    # --- cross-worktree session registry (scripts/session_registry.py) ---
    # The board's `sessions` above are per-worktree; this is the machine-wide view
    # of named Claude sessions across every worktree of the repo.
    registry_sessions = []
    try:
        # session_registry ships alongside this script, not under the project root.
        sys.path.insert(0, str(_script_dir()))
        import session_registry  # noqa: E402
        registry_sessions = session_registry.collect_sessions(include_all=False)
    except Exception:
        pass

    return {
        "sessions": sessions,
        "lockTotal": lock_total,
        "orphanLocks": orphan_locks,
        "build": build,
        "vnv": vnv,
        "lastGreen": last_green,
        "push": _push_divergence(),
        "interfaces": _interfaces(),
        "closeout": closeout,
        "closeoutBranches": closeout_branches,
        "registrySessions": registry_sessions,
    }


def render_text(b: dict) -> str:
    lines = ["", "═══ Coordination Board ═══"]
    sessions = b["sessions"]
    if not sessions:
        lines.append("  (no registered sessions — solo mode)")
    for s in sessions:
        if s["stale"]:
            flag = " ⚠️STALE→reap"          # no live PID + past fallback age
        elif s["idle"]:
            flag = " ·idle" + ("" if s["alive"] else " (no live pid)")
        else:
            flag = ""
        lines.append(f"  ● {s['humanName']}  [{s['domain']}]  {s['shortId']}  hb {s['heartbeatAge']}{flag}")
        meta = []
        if s["planFile"]:
            meta.append(f"plan {s['planFile']}")
        if s["taskIds"]:
            meta.append(f"tasks {','.join(map(str, s['taskIds']))}")
        meta.append(f"{s['lockCount']} locks")
        lines.append(f"      {' · '.join(meta)}")
        if s["activity"]:
            lines.append(f"      ▸ {s['activity']}")

    stale_ct = sum(1 for s in sessions if s["stale"])
    if stale_ct or b["orphanLocks"]:
        lines.append("")
        lines.append(f"  cleanup: {stale_ct} stale session(s), {b['orphanLocks']} orphan lock(s)"
                     f"  → /coord:reap-session --all-stale")

    bld = b["build"]
    if bld.get("available", True):
        lines.append("")
    active = bld.get("active", [])
    waiting = bld.get("waiting", [])
    mx = bld.get("max", 2)
    if active or waiting:
        head = f"  builds: {len(active)}/{mx} active · queue {len(waiting)}"
        if waiting:
            head += f" · next in line: {waiting[0]['shortId']}"
        lines.append(head)
        for t in active:
            lines.append(f"      ● {t['shortId']} [{t['worktree']}] building {t['age']}")
        for i, t in enumerate(waiting):
            lines.append(f"      {i + 1}. {t['shortId']} [{t['worktree']}] waiting {t['age']}")
    elif bld.get("available", True):
        lines.append(f"  builds: 0/{mx} active · queue empty")

    if b["vnv"]["pending"]:
        lines.append(f"  pending V&V: {b['vnv']['pending']} (oldest {b['vnv']['oldest']})")
    if b["lastGreen"]:
        lines.append(f"  last green build: {b['lastGreen']['sha']} ({b['lastGreen']['age']} ago)")
    lines.append(f"  total locks: {b['lockTotal']}")

    # Cross-worktree interface overlaps (Interface Coordination). Only shown when the
    # shared channel has a snapshot AND there is something to report.
    ifc = b.get("interfaces") or {}
    if ifc.get("available") and (ifc["divergent"] or ifc["coModify"] or ifc["definerDependent"] or ifc["contracts"]):
        lines.append("")
        lines.append(f"  interfaces: ‼️{ifc['divergent']} divergent · {ifc['coModify']} co-modify · "
                     f"{ifc['definerDependent']} definer/dependent · {ifc['contracts']} contract(s)")
        for t in ifc.get("top", []):
            lines.append(f"      {t}")
        if ifc["divergent"] or ifc["coModify"]:
            lines.append("      On a shared interface: land it to main first (PR = the contract), then rebase.")

    # Push nudge: unpushed local commits never reach CI (push/PR-triggered).
    push = b.get("push") or {}
    ahead = push.get("ahead")
    if isinstance(ahead, int) and ahead > 0:
        lines.append("")
        lines.append(f"  ⬆ PUSH NUDGE: {ahead} commit(s) on '{push.get('branch','main')}' "
                     f"not yet on origin — CI only runs")
        lines.append("     on push/PR. Push the branch and open a PR so broken HEAD "
                     "can't hide locally. Don't sit on green commits.")

    # Closeout hygiene: uncommitted/unpushed drift + open ledger items ("running
    # list"). Catches work abandoned just before commit/push. Only shown when
    # there is something outstanding.
    co = b.get("closeout") or {}
    open_items = co.get("open_items") or []
    stale_branches = b.get("closeoutBranches") or []
    if co and (not co.get("clean") or open_items or stale_branches):
        lines.append("")
        bits = []
        if co.get("uncommitted_count"):
            seg = f"{co['uncommitted_count']} uncommitted"
            if co.get("uncommitted_migrations"):
                seg += f" ({len(co['uncommitted_migrations'])} migration)"
            bits.append(seg)
        if co.get("unpushed_count"):
            seg = f"{co['unpushed_count']} unpushed commit(s)"
            if co.get("unpushed_migrations"):
                seg += f" (incl. {len(co['unpushed_migrations'])} migration)"
            bits.append(seg)
        lines.append("  📋 closeout: " + (" · ".join(bits) if bits else "tree clean")
                     + "  → /coord:closeout check")
        for it in open_items:
            idle = it.get("_idle_days", 0)
            idle_s = f", idle {idle}d" if idle else ""
            lines.append(f"      ○ [{it.get('id')}] {it.get('title','')[:54]} "
                         f"({it.get('status')}{idle_s})")
        if stale_branches:
            oldest = max((br.get("idle_days", 0) for br in stale_branches), default=0)
            br_mig = sum(1 for br in stale_branches if br.get("unpushed_migrations"))
            mig_s = f", {br_mig} w/ migration" if br_mig else ""
            lines.append(f"      ⎇ {len(stale_branches)} branch(es) with unpushed-only commits "
                         f"(oldest idle {oldest}d{mig_s}) → /coord:closeout branches")
        if co.get("unpushed_migrations") or co.get("uncommitted_migrations"):
            lines.append("      ⚠ a migration is unpushed/uncommitted — schema change won't reach the shared DB.")

    # Cross-worktree session registry: named Claude sessions across every worktree
    # of the repo (machine-wide view; `/coord:sessions` to list/rename, `--all` for native).
    reg = b.get("registrySessions") or []
    if reg:
        lines.append("")
        lines.append(f"  sessions (cross-worktree): {len(reg)}  → /coord:sessions")
        for s in reg:
            badge = "●" if s.get("live") else ("○stale" if s.get("stale") else "○")
            if s.get("idle"):
                badge += "·idle"
            name = s.get("humanName") or "(unnamed)"
            loc = s.get("worktree") or s.get("cwd") or "?"
            branch = f" [{s['branch']}]" if s.get("branch") else ""
            lines.append(f"      {badge} {name}  {s['sessionId'][:8]}  {loc}{branch}")

    lines.append("══════════════════════════")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Render the coordination board.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    # Opportunistic housekeeping (best-effort).
    try:
        # rotate_events_log ships alongside this script, not under the project root.
        sys.path.insert(0, str(_script_dir()))
        import rotate_events_log
        rotate_events_log.rotate(rotate_events_log.THRESHOLD_BYTES_DEFAULT, force=False)
    except Exception:
        pass

    board = collect()
    if args.json:
        print(json.dumps(board, indent=2))
    else:
        print(render_text(board))
    return 0


if __name__ == "__main__":
    sys.exit(main())
