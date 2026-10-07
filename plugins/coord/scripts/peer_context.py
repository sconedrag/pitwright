#!/usr/bin/env python3
"""peer_context.py — read-only "who else is touching this?" probe.

Safe to hand to a SUBAGENT. It mutates nothing: no locks taken or released, no messages
sent, no cursors advanced. That matters because subagents are deliberately NOT coordination
principals — they inherit their parent's session id (see _identity.session_id(),
which resolves identity from that env var), so a subagent shares its parent's lock identity
and does not appear in ListAgents at all. Letting each subagent negotiate would mean N
agents speaking for one session, with the peer unable to tell them apart.

The division of labour:
    subagent  → reads this, and ESCALATES to its parent (SendMessage to "main")
    parent    → the single coordination principal; it negotiates via agent_message.py

That is a standard escalation ladder (agent self → peer → orchestrator → user) —
often written down in project doctrine long before it has an actual mechanism behind it.

Usage
-----
    python3 scripts/peer_context.py <path> [<path> ...]   # who owns these files
    python3 scripts/peer_context.py --sessions            # who is live, and where
    python3 scripts/peer_context.py <path> --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _agent_channel  # noqa: E402
import coord_locks  # noqa: E402

try:
    import agent_message  # noqa: E402
except ImportError:  # pragma: no cover
    agent_message = None


def _registry() -> dict:
    try:
        p = _agent_channel.channel_dir() / "sessions-registry.json"
        return json.loads(p.read_text()).get("sessions", {})
    except (OSError, ValueError):
        return {}


def live_sessions() -> list[dict]:
    """Registered sessions, annotated with a real liveness verdict.

    Liveness is the PID probe, never a status string: sessions in this repo routinely show
    'idle' in ListAgents after 7 days, and a lock whose owning process is alive must never
    be treated as abandoned.
    """
    out = []
    for sid, rec in _registry().items():
        pid = int(rec.get("pid", 0) or 0)
        alive = coord_locks._pid_alive(pid)
        # Two-tier, mirroring reap_coordination.py: a dead-or-absent pid does NOT prove the
        # session is gone. An older registry entry may have recorded a throwaway shell pid
        # that always looks dead; fall back to heartbeat freshness rather than declaring a
        # working peer abandoned.
        basis = "pid"
        if not alive:
            basis = "heartbeat"
            hb = coord_locks._parse_heartbeat(rec.get("lastHeartbeat", ""))
            if hb is not None:
                age = (coord_locks.coord_config.utcnow_naive() - hb).total_seconds()
                alive = age <= coord_locks.STALE_SECONDS_DEFAULT
        out.append({
            "sessionId": sid,
            "humanName": rec.get("humanName", ""),
            "worktree": rec.get("worktree", ""),
            "branch": rec.get("branch", ""),
            "lastHeartbeat": rec.get("lastHeartbeat", ""),
            "alive": alive,
            "livenessBasis": basis,
        })
    out.sort(key=lambda r: (not r["alive"], r["humanName"]))
    return out


def _open_threads_for(rel: str) -> list[dict]:
    """Open mailbox requests that reference this file."""
    if agent_message is None:
        return []
    try:
        msgs = agent_message._decorate(agent_message._all_messages())
    except Exception:
        return []
    hits = []
    for m in msgs:
        if m.get("_status") != "open":
            continue
        if rel in (m.get("refs", {}).get("files") or []):
            frm = m.get("from", {})
            hits.append({
                "msgId": m.get("msgId"),
                "type": m.get("type"),
                "intent": m.get("intent"),
                "subject": m.get("subject"),
                "from": frm.get("humanName") or frm.get("sessionId"),
            })
    return hits


def context_for(path: str) -> dict:
    rel = coord_locks.rel_path(path)
    holder = coord_locks.owner(path)          # None when free or genuinely stale
    raw = coord_locks.read_lock(path)         # present even when stale
    info: dict = {
        "path": rel,
        "lockKey": coord_locks.lock_key(path),
        "locked": holder is not None,
        "openRequests": _open_threads_for(rel),
    }
    if holder:
        sid = holder.get("sessionId", "")
        rec = _registry().get(sid, {})
        info["owner"] = {
            "sessionId": sid,
            "humanName": rec.get("humanName", ""),
            "worktree": holder.get("worktree") or rec.get("worktree", ""),
            "domain": holder.get("domain", ""),
            "lockedAt": holder.get("lockedAt", ""),
        }
    elif raw:
        info["staleLockPresent"] = True
        info["note"] = ("A lock record exists but its owning session is gone; it will be "
                        "reclaimed on the next claim attempt.")
    return info


def render(rows: list[dict]) -> str:
    out = []
    for r in rows:
        if not r["locked"]:
            line = f"{r['path']}: free"
            if r.get("staleLockPresent"):
                line += "  (stale lock present — reclaimable)"
            out.append(line)
        else:
            o = r["owner"]
            who = o["humanName"] or o["sessionId"]
            out.append(
                f"{r['path']}: HELD by {who} [{o['domain']}] in worktree "
                f"'{o['worktree']}' since {o['lockedAt']}"
            )
        for t in r["openRequests"]:
            out.append(f"    open {t['type']} ({t['intent']}) from {t['from']}: "
                       f"{t['subject']}  [{t['msgId']}]")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only peer/lock context. Mutates nothing.")
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--sessions", action="store_true", help="list live sessions instead")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    if a.sessions or not a.paths:
        rows = live_sessions()
        if a.json:
            print(json.dumps(rows, indent=2))
            return 0
        if not rows:
            print("no registered sessions (a session only registers via /coord:start-session)")
            return 0
        for r in rows:
            mark = "live" if r["alive"] else "dead"
            print(f"[{mark}] {r['humanName'] or r['sessionId']}  "
                  f"worktree={r['worktree']}  branch={r['branch']}")
        return 0

    rows = [context_for(p) for p in a.paths]
    if a.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(render(rows))
    if any(r["locked"] for r in rows):
        print("\nHeld by a live peer. If you are a SUBAGENT, do not negotiate — report this "
              "to your parent session (SendMessage to \"main\") and let it decide.\n"
              "If you ARE the session, ask the owner: /coord:ask-lock <path> \"<why>\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
