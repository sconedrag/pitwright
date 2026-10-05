#!/usr/bin/env python3
"""
session_registry.py — cross-worktree registry of Claude Code sessions.

The per-worktree coordination dir (`.claude/coordination/`) is gitignored and cannot
carry state between worktrees. This module owns the ONE writable, cross-worktree
registry of Claude sessions — their native UUID, human name, worktree, branch and
liveness — so a human juggling parallel sessions can see "which session is which" and
jump back to one.

Single source of truth (writable):
    <git-common-dir>/agent-coordination/sessions-registry.json
(resolved via _agent_channel.channel_dir(), shared across every worktree of the repo).

Discovery source (read-only): Claude Code's native transcripts at
    ~/.claude/projects/<dash-encoded-cwd>/<session-uuid>.jsonl
`list --all` merges these in (source="native") but NEVER writes names back to them.

Concurrency: read-modify-write under a named lock in the SHARED channel (reusing
`_coord_lock` with locks_dir=channel/"locks"), then atomic temp-file + os.replace.

CLI:
    session_registry.py register --session-id <uuid> [--cwd <p>] [--pid <n>]
    session_registry.py name "<name>" --session-id <uuid>
    session_registry.py rename <id-or-oldname> "<new name>"
    session_registry.py heartbeat --session-id <uuid> [--activity "<summary>"]
    session_registry.py list [--all] [--json]
    session_registry.py gc
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
import _agent_channel  # noqa: E402  channel_dir(), worktree_id()
import _coord_lock  # noqa: E402  acquire/release/holder, _pid_alive
import coord_config  # noqa: E402

SCHEMA_VERSION = 1
REGISTRY_NAME = "sessions-registry.json"
LOCK_NAME = "session-registry"

# Liveness thresholds (mirror render_board.py / reap_coordination.py conventions).
IDLE_BADGE_SECONDS = coord_config.get("idle_badge_seconds")
REAP_SECONDS = coord_config.get("stale_seconds")
# How long since a heartbeat before a session stops counting as REPRESENTING a role.
# Deliberately separate from REAP_SECONDS: reaping deletes a record, this only decides whether
# a session can still be speaking for a discipline. Generous, because the cost is asymmetric —
# a false "dead" lets a stand-in spawn beside a live owner (two voices for one role), while a
# false "alive" only blocks a spawn. Only became meaningful once heartbeats were actually
# wired: before that, `lastHeartbeat` recorded when a session REGISTERED, never that it was
# still working, so every long session looked stale and every dead one looked as fresh as the
# day it died.
REPRESENTATION_STALE_SECONDS = coord_config.get("representation_stale_seconds")

# Bound the file: keep at most this many entries (most-recently-active retained).
MAX_ENTRIES = coord_config.get("registry_cap")


# --------------------------------------------------------------------------- paths

def _channel() -> Path:
    # SESSION_REGISTRY_CHANNEL overrides the shared channel (hermetic tests / explicit routing).
    override = os.environ.get("SESSION_REGISTRY_CHANNEL")
    if override:
        d = Path(override)
        d.mkdir(parents=True, exist_ok=True)
        return d
    return _agent_channel.channel_dir()


def _registry_path() -> Path:
    return _channel() / REGISTRY_NAME


def _channel_locks_dir() -> Path:
    return _channel() / "locks"


# --------------------------------------------------------------------------- time

def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def _age_seconds(iso: str) -> float:
    """Age of an ISO timestamp in seconds; `inf` if unparseable.

    Handles BOTH the naive `...Z` form this module writes and an offset-aware `...+00:00`
    form. It previously assumed the former and raised TypeError on the latter — a crash, not
    a fallback, inside the function every liveness answer and the whole `/coord:board` render depend
    on. It never fired only because every current writer happens to use `_utcnow()`; one
    writer using `datetime.isoformat()` on an aware value would have taken all of them down.
    """
    if not iso:
        return float("inf")
    try:
        parsed = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    now = datetime.datetime.now(datetime.timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return (now - parsed).total_seconds()


# --------------------------------------------------------------------------- git

def _git(*args: str) -> str:
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _git_context(cwd: str | None) -> dict:
    """Resolve worktree/branch/repoPath for `cwd` (defaults to the process cwd)."""
    prev = os.getcwd()
    moved = False
    try:
        if cwd and os.path.isdir(cwd):
            os.chdir(cwd)
            moved = True
        branch = _git("rev-parse", "--abbrev-ref", "HEAD") or None
        top = _git("rev-parse", "--show-toplevel") or None
        wt = _agent_channel.worktree_id() if top else None
    finally:
        if moved:
            os.chdir(prev)
    return {"worktree": wt, "branch": branch, "repoPath": top}


# --------------------------------------------------------------------------- store

def _empty_store() -> dict:
    return {"schemaVersion": SCHEMA_VERSION, "updatedAt": _utcnow(), "sessions": {}}


def _read_store() -> dict:
    path = _registry_path()
    if not path.is_file():
        return _empty_store()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return _empty_store()
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
        return _empty_store()
    data.setdefault("schemaVersion", SCHEMA_VERSION)
    return data


def _write_store(store: dict) -> None:
    """Atomic write: temp file in the channel dir + os.replace (same filesystem)."""
    store["updatedAt"] = _utcnow()
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp-{os.getpid()}")
    try:
        tmp.write_text(json.dumps(store, indent=2))
        os.replace(str(tmp), str(path))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _with_lock(mutator):
    """Run `mutator(store) -> store` under the shared cross-worktree registry lock."""
    locks = _channel_locks_dir()
    if not _coord_lock.acquire(LOCK_NAME, timeout=30, stale_after=60, quiet=True, locks_dir=locks):
        sys.stderr.write("[session-registry] could not acquire registry lock; aborting write.\n")
        return None
    try:
        store = _read_store()
        result = mutator(store)
        if result is not None:
            _write_store(result)
        return result
    finally:
        _coord_lock.release(LOCK_NAME, locks_dir=locks)


# --------------------------------------------------------------------------- ops

def _owning_pid(session_id: str, cwd: str, explicit: int | None) -> int:
    """Resolve the session's LONG-LIVED owning pid.

    `os.getppid()` is wrong here and was the bug (found in practice): this script is normally
    invoked from a throwaway shell, so getppid() records that shell — which exits a moment
    later. Every registry entry was therefore born looking dead, which silently disabled the
    registry's central safety property, "a session whose pid is alive is never reaped,
    however long it has idled". Liveness degraded to heartbeat-age alone.

    Order: an explicit --pid, then the per-worktree session manifest (written by
    /coord:start-session, which captures $PPID from a shell whose parent IS the claude process,
    and is verified to hold the real long-lived pid), then getppid() as a last resort.
    """
    if explicit and explicit > 0:
        return explicit
    manifest = Path(cwd) / ".claude" / "coordination" / "sessions" / f"{session_id}.json"
    try:
        pid = int(json.loads(manifest.read_text()).get("pid", 0) or 0)
        if pid > 0 and _coord_lock._pid_alive(pid):
            return pid
    except (OSError, ValueError, TypeError):
        pass
    ppid = os.getppid()
    # NEVER record pid 1. When the real parent has already exited, the process is reparented to
    # init and `getppid()` returns 1 — a pid that answers `os.kill(1, 0)` forever. Such a record
    # reads ALIVE permanently and, if it carries a domain, blocks that role's stand-in for good.
    # Observed in practice: a stray record with pid 1 and a domain sat in the shared
    # registry doing exactly that. 0 means "no usable pid", which liveness then handles
    # honestly instead of confidently.
    return 0 if ppid <= 1 else ppid


def cmd_register(session_id: str, cwd: str | None, pid: int | None) -> int:
    cwd = cwd or os.getcwd()
    owner_pid = _owning_pid(session_id, cwd, pid)
    ctx = _git_context(cwd)
    now = _utcnow()

    def mutator(store: dict) -> dict:
        sessions = store["sessions"]
        entry = sessions.get(session_id, {})
        entry.update({
            "sessionId": session_id,
            "humanName": entry.get("humanName", ""),
            "worktree": ctx["worktree"],
            "branch": ctx["branch"],
            "repoPath": ctx["repoPath"],
            "cwd": cwd,
            "pid": owner_pid,
            # A pid is only meaningful on the machine that recorded it: os.kill(pid, 0)
            # against a foreign pid probes an unrelated local process and answers
            # CONFIDENTLY WRONG. Stamping the host makes liveness checkable rather than
            # conventional — the same reason session_manifest records it.
            "host": _machine_id(),
            "startedAt": entry.get("startedAt", now),
            "lastHeartbeat": now,
            "lastActivity": entry.get("lastActivity", ""),
            "status": "active",
            "source": "registry",
        })
        sessions[session_id] = entry
        return _prune(store)

    return 0 if _with_lock(mutator) is not None else 1


def cmd_name(session_id: str, name: str) -> int:
    def mutator(store: dict) -> dict | None:
        entry = store["sessions"].get(session_id)
        if entry is None:
            sys.stderr.write(f"[session-registry] no session '{session_id}' to name.\n")
            return None
        entry["humanName"] = name.strip()
        entry["lastHeartbeat"] = _utcnow()
        return store

    return 0 if _with_lock(mutator) is not None else 1


def cmd_domain(session_id: str, domain: str) -> int:
    """Record this session's discipline in the SHARED registry.

    Session manifests are per-worktree and gitignored, so a domain declared there is
    invisible to every other worktree. Measured in practice: none of the registry entries
    carried a domain, which made "is this role represented by a live session?" unanswerable
    across worktrees — and that question is what stops two stand-ins being spawned for one
    role and writing divergent positions into one ledger.

    The registry is the only shared, cross-worktree view of who is live, so the discipline
    belongs here too.
    """
    def mutator(store: dict) -> dict | None:
        entry = store["sessions"].get(session_id)
        if entry is None:
            sys.stderr.write(f"[session-registry] no session '{session_id}' to scope.\n")
            return None
        entry["domain"] = domain.strip()
        entry["lastHeartbeat"] = _utcnow()
        return store

    return 0 if _with_lock(mutator) is not None else 1


def cmd_rename(target: str, name: str) -> int:
    def mutator(store: dict) -> dict | None:
        sid = _resolve_target(store, target)
        if sid is None:
            sys.stderr.write(f"[session-registry] no session matching '{target}'.\n")
            return None
        store["sessions"][sid]["humanName"] = name.strip()
        return store

    return 0 if _with_lock(mutator) is not None else 1


def cmd_heartbeat(session_id: str, activity: str | None) -> int:
    def mutator(store: dict) -> dict | None:
        entry = store["sessions"].get(session_id)
        if entry is None:
            return None
        entry["lastHeartbeat"] = _utcnow()
        if activity is not None:
            entry["lastActivity"] = activity.strip()
        return store

    return 0 if _with_lock(mutator) is not None else 1


def cmd_gc() -> int:
    def mutator(store: dict) -> dict:
        return _prune(store)

    return 0 if _with_lock(mutator) is not None else 1


def _resolve_target(store: dict, target: str) -> str | None:
    """Resolve a session by exact UUID, short-UUID prefix, or exact human name."""
    sessions = store["sessions"]
    if target in sessions:
        return target
    matches = [sid for sid in sessions if sid.startswith(target)]
    if len(matches) == 1:
        return matches[0]
    named = [sid for sid, e in sessions.items() if e.get("humanName") == target]
    if len(named) == 1:
        return named[0]
    return None


def _prune(store: dict) -> dict:
    """Drop dead-PID entries past REAP_SECONDS, then cap to MAX_ENTRIES
    most-recently-active. Live PIDs are never pruned."""
    sessions = store["sessions"]
    survivors = {}
    for sid, e in sessions.items():
        # A record whose checkout is gone can never be a live session — a deleted worktree, or
        # a test's temp repo that escaped its sandbox. Definitive, so it is pruned regardless
        # of pid or age; guarded on a non-empty path so a record that simply never recorded one
        # is left to the ordinary rules below.
        repo = str(e.get("repoPath") or "")
        if repo and not Path(repo).exists():
            continue
        pid = int(e.get("pid", 0) or 0)
        # pid <= 1 is NOT protection. init always answers `os.kill(1, 0)`, so treating it as a
        # live pid made such a record permanently unprunable AND permanently "alive" — the two
        # halves of the same hole. Fall through to the heartbeat, which is the honest signal.
        alive = pid > 1 and _coord_lock._pid_alive(pid)
        age = _age_seconds(e.get("lastHeartbeat", ""))
        if (not alive) and age > REAP_SECONDS:
            continue
        survivors[sid] = e
    # Cap: keep the most-recently-active entries.
    if len(survivors) > MAX_ENTRIES:
        ordered = sorted(survivors.items(),
                         key=lambda kv: kv[1].get("lastHeartbeat", ""), reverse=True)
        survivors = dict(ordered[:MAX_ENTRIES])
    store["sessions"] = survivors
    return store


# --------------------------------------------------------------------------- listing

def _machine_id() -> str:
    try:
        import coord_locks
        return coord_locks.machine_id()
    except Exception:
        import socket
        return socket.gethostname()


LIVE_ALIVE, LIVE_DEAD, LIVE_UNKNOWN = "alive", "dead", "unknown"


def liveness(entry: dict) -> dict:
    """Three-state liveness for a REGISTRY record, computed from the record's own fields.

    Why this exists rather than reusing `coord_locks.liveness`
    ---------------------------------------------------------
    That function resolves a session id to `sessions/<id>.json`, a per-worktree manifest keyed
    by TERM_SESSION_ID. Registry records are keyed by the NATIVE session UUID. They are
    different id namespaces, so the lookup misses for every record — not merely for records
    from another worktree, but for every session that has ever existed, including the local
    live one. It therefore answered `unknown` 100% of the time, by construction, which read as
    "cannot determine" and (correctly, per the fail-closed rule) blocked every stand-in spawn
    forever. Measured in practice: every record came back with reason "no session manifest".

    `unknown` is never resolved toward the risky action: a session we cannot classify may be
    representing a role right now, and spawning beside it produces the second voice the role
    lock exists to prevent.
    """
    pid = int(entry.get("pid", 0) or 0)
    host = str(entry.get("host") or "")
    age = _age_seconds(entry.get("lastHeartbeat", ""))

    if host and host != _machine_id():
        return {"state": LIVE_UNKNOWN,
                "reason": f"recorded on host {host[:12]}; a pid is only probeable on the "
                          f"machine that recorded it"}
    if age > REPRESENTATION_STALE_SECONDS:
        # Stale beats a live-looking pid on purpose. Nothing refreshes a dead session's
        # heartbeat, but pids ARE recycled, so an old record whose pid now answers is far more
        # likely a reused pid than a session that has sat silent for hours.
        # `age` is +inf when the field is missing or unparseable, and `inf // 3600` is NaN,
        # so formatting it as an integer raised — turning a correct classification into a
        # crash inside the function every liveness answer depends on. The VERDICT was always
        # right (a record that has never beaten is not representing anything); only the
        # explanation could fail, which is the worst place for it.
        span = "ever" if age == float("inf") else f"{int(age // 3600)}h"
        return {"state": LIVE_DEAD, "reason": f"no heartbeat ({span})"}
    if pid <= 1:
        # No usable pid — absent, or the reparented-to-init case guarded above. We are already
        # past the staleness check, so the heartbeat is fresh, and only a running session beats
        # it. That is weaker evidence than a live pid (its resolution is the staleness bound,
        # not seconds), so the reason says so rather than implying precision we do not have.
        # Returning `unknown` would be more cautious and strictly worse: it blocks the role
        # forever, which is the inert failure this layer has already produced once.
        return {"state": LIVE_ALIVE,
                "reason": f"heartbeat {int(age)}s ago (no usable pid; heartbeat evidence only)"}
    if not _coord_lock._pid_alive(pid):
        return {"state": LIVE_DEAD, "reason": f"pid {pid} is gone"}
    if not host:
        # Pre-dates host stamping. The heartbeat is fresh and the pid answers, so it is very
        # likely alive — but "likely" is exactly what must not resolve toward spawning.
        return {"state": LIVE_UNKNOWN,
                "reason": f"pid {pid} answers but the record has no host to verify it against"}
    return {"state": LIVE_ALIVE, "reason": f"pid {pid} alive, heartbeat {int(age)}s ago"}


def _decorate(entry: dict) -> dict:
    """Add computed liveness fields for display."""
    pid = int(entry.get("pid", 0) or 0)
    alive = _coord_lock._pid_alive(pid) if pid else False
    age = _age_seconds(entry.get("lastHeartbeat", ""))
    out = dict(entry)
    out["live"] = alive
    out["idle"] = bool(alive and age > IDLE_BADGE_SECONDS)
    out["stale"] = bool((not alive) and age > REAP_SECONDS)
    out["heartbeatAgeSeconds"] = None if age == float("inf") else int(age)
    return out


def _native_sessions() -> list[dict]:
    """Read-only enumeration of Claude Code's native transcripts (machine-wide)."""
    base = Path.home() / ".claude" / "projects"
    out = []
    if not base.is_dir():
        return out
    try:
        project_dirs = [d for d in base.iterdir() if d.is_dir()]
    except OSError:
        return out
    for pdir in project_dirs:
        try:
            transcripts = list(pdir.glob("*.jsonl"))
        except OSError:
            continue
        for tf in transcripts:
            try:
                mtime = tf.stat().st_mtime
            except OSError:
                continue
            ts = datetime.datetime.utcfromtimestamp(mtime).isoformat() + "Z"
            out.append({
                "sessionId": tf.stem,
                "humanName": "",
                "worktree": None,
                "branch": None,
                "repoPath": None,
                "cwd": pdir.name,  # dash-encoded project path (lossy to decode; shown as-is)
                "pid": 0,
                "startedAt": "",
                "lastHeartbeat": ts,
                "lastActivity": "",
                "status": "unknown",
                "source": "native",
            })
    return out


def collect_sessions(include_all: bool = False) -> list[dict]:
    """Return decorated registry entries; with include_all, merge read-only native entries
    (by UUID) that the writable registry does not already track."""
    store = _read_store()
    entries = {sid: _decorate(e) for sid, e in store["sessions"].items()}
    if include_all:
        for nat in _native_sessions():
            sid = nat["sessionId"]
            if sid not in entries:
                entries[sid] = _decorate(nat)
    return sorted(entries.values(),
                  key=lambda e: e.get("lastHeartbeat", ""), reverse=True)


def cmd_list(include_all: bool, as_json: bool) -> int:
    rows = collect_sessions(include_all)
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("(no sessions registered)")
        return 0
    for e in rows:
        badge = "●live" if e["live"] else ("○stale" if e["stale"] else "○")
        if e["idle"]:
            badge += " ·idle"
        name = e.get("humanName") or "(unnamed)"
        loc = e.get("worktree") or e.get("cwd") or "?"
        branch = f" [{e['branch']}]" if e.get("branch") else ""
        src = "" if e.get("source") == "registry" else f" ({e.get('source')})"
        print(f"  {badge:>12}  {name:<28}  {e['sessionId'][:8]}  {loc}{branch}{src}")
    return 0


# --------------------------------------------------------------------------- cli

def _main() -> int:
    p = argparse.ArgumentParser(description="Cross-worktree Claude session registry.")
    sub = p.add_subparsers(dest="action", required=True)

    pr = sub.add_parser("register")
    pr.add_argument("--session-id", required=True)
    pr.add_argument("--cwd", default=None)
    pr.add_argument("--pid", type=int, default=None)

    pn = sub.add_parser("name")
    pn.add_argument("value")
    pn.add_argument("--session-id", required=True)

    pd = sub.add_parser("domain")
    pd.add_argument("value")
    pd.add_argument("--session-id", required=True)

    prn = sub.add_parser("rename")
    prn.add_argument("target")
    prn.add_argument("value")

    ph = sub.add_parser("heartbeat")
    ph.add_argument("--session-id", required=True)
    ph.add_argument("--activity", default=None)

    pl = sub.add_parser("list")
    pl.add_argument("--all", action="store_true")
    pl.add_argument("--json", action="store_true")

    sub.add_parser("gc")

    args = p.parse_args()
    if args.action == "register":
        return cmd_register(args.session_id, args.cwd, args.pid)
    if args.action == "name":
        return cmd_name(args.session_id, args.value)
    if args.action == "domain":
        return cmd_domain(args.session_id, args.value)
    if args.action == "rename":
        return cmd_rename(args.target, args.value)
    if args.action == "heartbeat":
        return cmd_heartbeat(args.session_id, args.activity)
    if args.action == "list":
        return cmd_list(args.all, args.json)
    if args.action == "gc":
        return cmd_gc()
    return 1


if __name__ == "__main__":
    sys.exit(_main())
