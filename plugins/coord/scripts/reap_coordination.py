#!/usr/bin/env python3
"""
reap_coordination.py — Coordination Harness v2, Component 7.

Cleans up two classes of stale coordination state that accumulate because
sessions rarely call /coord:complete-session:

  1. STALE SESSIONS  — a registered session that is NOT live. Liveness is
     decided by the recorded owning PID (same-machine `os.kill(pid, 0)`): a
     session whose process is still alive is never stale, however long it has
     been idle (a developer who walked away must not be reaped). Only sessions
     with a dead/absent PID AND a heartbeat older than the fallback threshold
     are reaped — locks released, manifest archived to history/ with
     status="reaped", SESSION_REAPED logged. See _session_is_stale().

  2. ORPHAN LOCKS    — a lock file in locks/ whose owning sessionId has no
     manifest in sessions/ (the session was reaped/completed/never archived
     cleanly, but its locks lingered). These are deleted.

Verified pre-existing degradation in practice: large numbers of lock files
accumulate, many of them orphaned, and most registered sessions turn out to be
stale. This script is the reaper for both classes and is also invoked
(sweep-only) from session_start.sh.

Modes:
  --all-stale         Reap every stale session AND sweep orphan locks (default
                      when no session id is given).
  <session-id>        Reap exactly this session (bypasses the liveness/age
                      check — explicit id is an operator override).
  --orphan-locks      Sweep orphan locks only; leave sessions untouched.
  --dry-run           Report what WOULD be reaped without mutating anything.
  --stale-seconds N   Override the heartbeat-age FALLBACK threshold (only used
                      when a session has no live PID). Default 24h, or the
                      COORD_STALE_SECONDS env var.

Exit code is always 0 unless arguments are malformed — reaping is best-effort
cleanup and must never block a caller.
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

# Liveness — not idle-time — decides reaping (mirrors scripts/_coord_lock.py).
# A session whose owning PID is still alive is NEVER stale, regardless of how
# long it has been idle: a developer who walks away from a live session must not
# have their locks reaped or their manifest archived. The heartbeat-age threshold
# below is ONLY a fallback for manifests with no recorded PID (legacy) or a dead/
# cross-machine PID. Default 24h so an overnight or cross-machine gap never false-
# reaps a genuinely-idle session; override with COORD_STALE_SECONDS.
STALE_SECONDS_DEFAULT = coord_config.get("stale_seconds")


def _repo_root() -> Path:
    return coord_config.project_root()


def _coord_dir() -> Path:
    return _repo_root() / ".claude" / "coordination"


def _naive_utcnow() -> datetime.datetime:
    """Naive UTC now — matches the `utcnow().isoformat()+'Z'` format used
    across the coordination system, without the utcnow() deprecation warning."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def _utcnow_iso() -> str:
    return _naive_utcnow().isoformat() + "Z"


def _heartbeat_age_seconds(manifest: dict) -> float:
    """Seconds since lastHeartbeat; +inf if unparseable (treat as stale)."""
    hb = manifest.get("lastHeartbeat", "")
    if not hb:
        return float("inf")
    parsed = coord_config.parse_utc(hb)
    if parsed is None:
        return float("inf")
    return (_naive_utcnow() - parsed).total_seconds()


def _pid_alive(pid: int) -> bool:
    """True if a process with this PID currently exists (same-machine liveness).
    Mirrors scripts/_coord_lock.py._pid_alive. On a single machine this is the
    authoritative signal that a session is still running; cross-machine manifests
    omit `pid` (or carry a PID that resolves to an unrelated local process) and
    fall back to heartbeat age."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False
    return True


def _session_is_stale(manifest: dict, stale_seconds: int) -> bool:
    """A session is stale only if it is NOT live. Live = a recorded owning PID
    that is still alive — such a session is never stale no matter how long idle.
    With no live PID (legacy manifest, crashed process, or cross-machine), fall
    back to heartbeat age > stale_seconds."""
    pid = int(manifest.get("pid", 0) or 0)
    if _pid_alive(pid):
        return False
    return _heartbeat_age_seconds(manifest) > stale_seconds


def _load_json(path: Path) -> dict | None:
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def _active_session_ids(sessions_dir: Path) -> set[str]:
    if not sessions_dir.is_dir():
        return set()
    return {p.stem for p in sessions_dir.glob("*.json")}


def _append_event(events_log: Path, event: str, sid: str, domain: str, msg: str) -> None:
    line = f"{_utcnow_iso()}|{event}|{sid}|{domain}|{msg}\n"
    try:
        with open(events_log, "a") as handle:
            handle.write(line)
    except OSError:
        pass


def _release_session_locks(locks_dir: Path, sid: str, dry_run: bool) -> int:
    """Delete every lock file owned by sid. Returns count released."""
    if not locks_dir.is_dir():
        return 0
    released = 0
    for lock_file in locks_dir.glob("*.lock"):
        lock = _load_json(lock_file)
        if lock is None:
            continue
        if lock.get("sessionId") == sid:
            if not dry_run:
                try:
                    lock_file.unlink()
                except OSError:
                    continue
            released += 1
    return released


def reap_session(sid: str, coord: Path, dry_run: bool, *, label: str = "reaped") -> dict:
    """Reap one session by id. `label` is the archived status: "reaped" (stale/
    explicit) or "completed" (graceful SessionEnd). Returns a summary dict."""
    sessions_dir = coord / "sessions"
    locks_dir = coord / "locks"
    history_dir = coord / "history"
    events_log = coord / "events.log"

    manifest_path = sessions_dir / f"{sid}.json"
    manifest = _load_json(manifest_path)
    if manifest is None:
        return {"sessionId": sid, "reaped": False, "reason": "no manifest"}

    domain = manifest.get("domain", "unknown")
    released = _release_session_locks(locks_dir, sid, dry_run)

    if not dry_run:
        history_dir.mkdir(parents=True, exist_ok=True)
        manifest["status"] = label
        manifest["reapedAt" if label == "reaped" else "completedAt"] = _utcnow_iso()
        manifest["lockedFiles"] = []
        try:
            with open(history_dir / f"{sid}.json", "w") as handle:
                json.dump(manifest, handle, indent=2)
            manifest_path.unlink()
        except OSError:
            pass
        event = "SESSION_COMPLETE" if label == "completed" else "SESSION_REAPED"
        verb = "Completed" if label == "completed" else "Reaped stale/explicit"
        _append_event(
            events_log, event, sid, domain,
            f"{verb} session. Released {released} locks.",
        )

    return {
        "sessionId": sid,
        "domain": domain,
        "humanName": manifest.get("humanName", ""),
        "reaped": True,
        "locksReleased": released,
    }


def sweep_orphan_locks(coord: Path, dry_run: bool) -> dict:
    """Delete locks whose owning session has no manifest. Returns summary."""
    locks_dir = coord / "locks"
    sessions_dir = coord / "sessions"
    if not locks_dir.is_dir():
        return {"orphansDeleted": 0, "orphanSessionIds": []}

    active = _active_session_ids(sessions_dir)
    deleted = 0
    orphan_sids: set[str] = set()
    for lock_file in locks_dir.glob("*.lock"):
        lock = _load_json(lock_file)
        if lock is None:
            # Corrupt/unparseable lock — also an orphan.
            orphan_sids.add("(corrupt)")
            if not dry_run:
                try:
                    lock_file.unlink()
                except OSError:
                    continue
            deleted += 1
            continue
        sid = lock.get("sessionId", "")
        if sid not in active:
            orphan_sids.add(sid)
            if not dry_run:
                try:
                    lock_file.unlink()
                except OSError:
                    continue
            deleted += 1
    return {"orphansDeleted": deleted, "orphanSessionIds": sorted(orphan_sids)}


def sweep_channel(dry_run: bool) -> dict:
    """GC the SHARED cross-worktree interface channel ($CHANNEL, git-common-dir):
    archive intents for worktrees that no longer exist, then refresh the overlap-report
    (which self-drops removed worktrees since it re-enumerates `git worktree list`).
    Best-effort — absent feature / channel is a no-op. (Interface Coordination, Phase 4.)"""
    out = {"intentsArchived": 0, "reportRefreshed": False}
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _agent_channel
        ch = _agent_channel.channel_dir()
    except Exception:
        return out

    # Live worktree ids = the worktree_id() scheme (dir basename; "main" for primary).
    live = set()
    try:
        wl = subprocess.run(["git", "worktree", "list", "--porcelain"],
                            capture_output=True, text=True, timeout=10).stdout
        primary = True
        for line in wl.splitlines():
            if line.startswith("worktree "):
                p = line[len("worktree "):]
                live.add("main" if primary else os.path.basename(p))
                primary = False
    except (OSError, subprocess.SubprocessError):
        live = None  # unknown → don't prune (fail safe)

    intents = ch / "intents"
    hist = ch / "history"
    if live is not None and intents.is_dir():
        for f in intents.glob("*.json"):
            if f.stem not in live:
                if not dry_run:
                    try:
                        hist.mkdir(parents=True, exist_ok=True)
                        f.replace(hist / f.name)
                    except OSError:
                        continue
                out["intentsArchived"] += 1
                _agent_channel.append_event("INTENT_REAPED", f"worktree {f.stem} gone — intent archived")

    # Refresh the overlap snapshot so removed worktrees drop from the board/guard.
    if not dry_run:
        try:
            import interface_overlap
            report = interface_overlap.detect(interface_overlap.wto.repo_root())
            import json as _json
            (ch / "overlap-report.json").write_text(_json.dumps(report, indent=2) + "\n")
            out["reportRefreshed"] = True
        except Exception:
            pass
    return out


def find_stale_sessions(coord: Path, stale_seconds: int) -> list[str]:
    sessions_dir = coord / "sessions"
    if not sessions_dir.is_dir():
        return []
    stale: list[str] = []
    for manifest_path in sessions_dir.glob("*.json"):
        manifest = _load_json(manifest_path)
        if manifest is None:
            stale.append(manifest_path.stem)
            continue
        if _session_is_stale(manifest, stale_seconds):
            stale.append(manifest_path.stem)
    return stale


def main() -> int:
    parser = argparse.ArgumentParser(description="Reap stale sessions + orphan locks.")
    parser.add_argument("session_id", nargs="?", default=None,
                        help="Reap exactly this session id (operator override — "
                             "bypasses the liveness and heartbeat-age checks).")
    parser.add_argument("--all-stale", action="store_true",
                        help="Reap all stale sessions AND sweep orphan locks.")
    parser.add_argument("--orphan-locks", action="store_true",
                        help="Sweep orphan locks only.")
    parser.add_argument("--complete", action="store_true",
                        help="Archive the named session as 'completed' (graceful end) "
                             "rather than 'reaped'. Requires a session id.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Report without mutating.")
    parser.add_argument("--stale-seconds", type=int, default=STALE_SECONDS_DEFAULT,
                        help="Heartbeat-age FALLBACK threshold in seconds, used only "
                             "when a session has no live PID (default "
                             f"{STALE_SECONDS_DEFAULT}, or $COORD_STALE_SECONDS). "
                             "A session with a live PID is never stale.")
    args = parser.parse_args()

    coord = _coord_dir()
    if not coord.is_dir():
        print("No coordination directory — nothing to reap.")
        return 0

    prefix = "[dry-run] " if args.dry_run else ""
    summaries: list[dict] = []

    if args.session_id:
        label = "completed" if args.complete else "reaped"
        summaries.append(reap_session(args.session_id, coord, args.dry_run, label=label))
    elif args.orphan_locks:
        pass  # orphan sweep handled below
    else:
        # Default + --all-stale: reap stale sessions.
        for sid in find_stale_sessions(coord, args.stale_seconds):
            summaries.append(reap_session(sid, coord, args.dry_run))

    # Orphan-lock sweep runs for: default, --all-stale, --orphan-locks
    # (but NOT for a single named-session reap, to keep that operation scoped).
    orphan_summary = None
    channel_summary = None
    if not args.session_id:
        orphan_summary = sweep_orphan_locks(coord, args.dry_run)
        # GC the shared cross-worktree interface channel too (Interface Coordination, P4).
        channel_summary = sweep_channel(args.dry_run)

    # Report
    reaped = [s for s in summaries if s.get("reaped")]
    print(f"{prefix}Reaped {len(reaped)} session(s):")
    for s in reaped:
        name = s.get("humanName") or s.get("domain", "")
        print(f"  - {s['sessionId']} ({name}) — released {s.get('locksReleased', 0)} locks")
    for s in summaries:
        if not s.get("reaped"):
            print(f"  - {s['sessionId']}: skipped ({s.get('reason', 'unknown')})")
    if orphan_summary is not None:
        print(f"{prefix}Orphan locks deleted: {orphan_summary['orphansDeleted']}")
        if orphan_summary["orphanSessionIds"]:
            print(f"  owning (dead) session ids: {', '.join(orphan_summary['orphanSessionIds'])}")
    if channel_summary is not None and (channel_summary["intentsArchived"] or channel_summary["reportRefreshed"]):
        print(f"{prefix}Interface channel: {channel_summary['intentsArchived']} stale intent(s) archived"
              f"{', overlap-report refreshed' if channel_summary['reportRefreshed'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
