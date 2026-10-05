#!/usr/bin/env python3
"""coord_locks.py — the ONE canonical implementation of coordination file locks.

Why this exists
---------------
Lock keying and lock-file format must agree everywhere a lock is taken, read, or judged
stale — the skills (`/coord:claim-files`, `/coord:release-files`, `/coord:start-session
--auto-claim`), the PreToolUse edit hook (`lock_guard.py`), and `blocker_owner.py`'s
same-checkout lock lookup. Re-deriving a lock key or lock-file format independently in
more than one of those places invites exactly the drift this module exists to prevent:

  1. KEY MISMATCH — hashing `<path>` alone in one caller while another hashes
     `<worktree-id>:<path>` addresses two *different namespaces*, so a lock taken by one
     is invisible to the other and provides no protection whatsoever. The
     "claim files before you work" discipline would be inert.
  2. FORMAT MISMATCH — writing plain-text lines where a reader expects
     `json.load(...)['sessionId']` makes the parse fail; a failed parse can make the
     staleness probe return 'stale' and delete another session's lock out from under it.

Both classes of bug come from the same root cause: a lock format documented in prose in
several places instead of implemented once. This module is the single implementation;
every caller — including `lock_guard.py` — imports it directly rather than re-deriving
the key or format, so the code paths cannot drift by construction.
`scripts/tests/test_coord_locks.py` is the regression test for that invariant.

Liveness semantics match `scripts/reap_coordination.py`: a lock whose owning session
PID is still alive is NEVER stale, however long it has idled — a peer must never steal
the lock of a developer who merely walked away.

CLI
---
    python3 scripts/coord_locks.py key      <path>
    python3 scripts/coord_locks.py owner    <path>
    python3 scripts/coord_locks.py claim    <path> [<path> ...]
    python3 scripts/coord_locks.py release  <path> [<path> ...]
    python3 scripts/coord_locks.py release  --all
    python3 scripts/coord_locks.py list
All commands accept `--json` for machine-readable output.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import coord_config  # noqa: E402

try:
    import _coord_lock  # noqa: E402  — reuse its PID liveness probe
except ImportError:  # pragma: no cover
    _coord_lock = None

STALE_SECONDS_DEFAULT = coord_config.get("stale_seconds")


# --------------------------------------------------------------------------- helpers
def _git(*args: str) -> str:
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def project_root() -> Path:
    return coord_config.project_root()


def worktree_id() -> str:
    """'main' for the primary checkout, else the worktree-root basename.

    Must stay byte-identical to `_agent_channel.worktree_id()` (and to any other
    implementation deriving the same worktree id, e.g. a shell hook) — the lock key
    depends on it.
    """
    git_dir = _git("rev-parse", "--absolute-git-dir")
    top = _git("rev-parse", "--show-toplevel")
    if git_dir and "/worktrees/" in git_dir and top:
        wt = os.path.basename(top)
    else:
        wt = "main"
    return re.sub(r"[^A-Za-z0-9._+-]", "_", wt) or "main"


def coord_dir() -> Path:
    return project_root() / ".claude" / "coordination"


def locks_dir() -> Path:
    d = coord_dir() / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def sessions_dir() -> Path:
    return coord_dir() / "sessions"


def session_id() -> str:
    sid = os.environ.get("TERM_SESSION_ID", "")
    sid = re.sub(r"[^A-Za-z0-9_-]", "", sid)
    if sid:
        return sid
    cached = Path(f"/tmp/.claude-session-{os.getppid()}.id")
    if cached.exists():
        try:
            return re.sub(r"[^A-Za-z0-9_-]", "", cached.read_text().strip())
        except OSError:
            pass
    return ""


def rel_path(p: str) -> str:
    """Normalise any path to repo-relative POSIX form (the guard's REL_PATH)."""
    root = project_root()
    cand = Path(p)
    if not cand.is_absolute():
        cand = (Path.cwd() / cand)
    try:
        return str(cand.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(Path(p))  # outside the repo; caller decides


def lock_key(path: str, wt: str | None = None) -> str:
    """sha256("<worktree>:<repo-relative-path>")[:16] — the guard's exact formula."""
    wt = wt or worktree_id()
    return hashlib.sha256(f"{wt}:{rel_path(path)}".encode()).hexdigest()[:16]


def lock_path(path: str) -> Path:
    return locks_dir() / f"{lock_key(path)}.lock"


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _pid_alive(pid: int) -> bool:
    if _coord_lock is not None:
        try:
            return _coord_lock._pid_alive(pid)
        except Exception:
            pass
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


def _session_manifest(sid: str) -> dict | None:
    f = sessions_dir() / f"{sid}.json"
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return None


def is_stale(meta: dict, stale_seconds: int = STALE_SECONDS_DEFAULT) -> bool:
    """A lock is stale only if its owning session is NOT live.

    Live = recorded owning PID still alive (never stale, however long idle).
    Fallback for a dead/absent PID: heartbeat age > stale_seconds.
    """
    manifest = _session_manifest(meta.get("sessionId", ""))
    if manifest is None:
        return True
    if _pid_alive(int(manifest.get("pid", 0) or 0)):
        return False
    age = _heartbeat_age(manifest)
    if age is None:
        return True
    return age > stale_seconds


# ------------------------------------------------------------------ liveness (3-state)
#
# `is_stale` above is BINARY and that is correct for what it does: decide whether a lock
# may be reclaimed. It is too coarse to authorise INHERITING a session's open work,
# because it resolves every uncertainty to "stale". Two cases it cannot distinguish:
#
#   - a manifest with no recorded pid (`pid: 0`) — falls through to heartbeat age, so an
#     idle-but-alive session reads as stale;
#   - a pid recorded on ANOTHER machine — `os.kill(pid, 0)` probes whatever local process
#     happens to hold that number, so the answer is not merely unknown, it is confidently
#     wrong in either direction.
#
# Inheriting a live session's work is the one outcome worse than staying blocked, so the
# verdict used to authorise it carries an explicit third state and fails toward "alive".
# `unknown` is a real answer here, not a placeholder — the convention is that a
# cross-machine manifest omits its pid, and `host` makes that checkable rather than
# assumed.

def machine_id() -> str:
    """A machine identity that survives a network change.

    `socket.gethostname()` does NOT. macOS rewrites the mDNS name as the network
    changes: the SAME machine can report `MacBook-Pro.local` and `Mac.lan` hours apart
    (observed in practice). Comparing a stored hostname against the current one then
    yields "another host" for the same box, collapsing every liveness verdict to
    `unknown`. That is safe — unknown never authorises inheritance — but it silently
    disables the feature for the ordinary laptop case, which is indistinguishable
    from the feature working and finding nothing.

    Order: the hardware UUID (stable across networks, reinstalls and renames), then
    the Linux machine-id, then the hostname. The fallback is deliberately the old
    behaviour rather than an error: an unstable id degrades to `unknown`, while no
    id at all would too, so there is nothing to gain by failing loudly here.

    Legacy manifests carry a hostname in `host`. Those compare unequal to a hardware
    UUID and therefore read as `unknown` — safe, and self-healing as sessions
    re-register.
    """
    global _MACHINE_ID
    if _MACHINE_ID is not None:
        return _MACHINE_ID
    _MACHINE_ID = _probe_machine_id()
    return _MACHINE_ID


def _probe_machine_id() -> str:
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True, text=True, timeout=5, check=False).stdout
            m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
            if m:
                return f"hw:{m.group(1)}"
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            mid = Path("/etc/machine-id").read_text().strip()
            if mid:
                return f"hw:{mid}"
        except OSError:
            pass
    return socket.gethostname()


_MACHINE_ID: str | None = None
HOST = machine_id()

LIVE_ALIVE = "alive"
LIVE_DEAD = "dead"
LIVE_UNKNOWN = "unknown"


def liveness(sid: str, stale_seconds: int = STALE_SECONDS_DEFAULT) -> dict:
    """Three-state liveness verdict for a session id.

    Returns {state, reason, pid, host, heartbeatAgeSeconds, inheritable}. Only
    `state == "dead"` may authorise inheritance; `unknown` is treated as alive by
    every caller.
    """
    manifest = _session_manifest(sid)
    if manifest is None:
        # No manifest at all: nothing to inherit FROM, and no evidence of death either.
        return {"state": LIVE_UNKNOWN, "reason": "no session manifest",
                "pid": None, "host": None, "heartbeatAgeSeconds": None,
                "inheritable": False}

    pid = int(manifest.get("pid", 0) or 0)
    host = manifest.get("host")
    age = _heartbeat_age(manifest)

    if host and host != HOST:
        return {"state": LIVE_UNKNOWN,
                "reason": f"recorded on another host ({host}); pid is not probeable here",
                "pid": pid or None, "host": host, "heartbeatAgeSeconds": age,
                "inheritable": False}

    if pid <= 0:
        # By convention a cross-machine manifest omits its pid, so a missing pid is
        # ambiguous between "remote" and "legacy local". Ambiguity is not death.
        return {"state": LIVE_UNKNOWN, "reason": "no pid recorded",
                "pid": None, "host": host, "heartbeatAgeSeconds": age,
                "inheritable": False}

    if _pid_alive(pid):
        # Authoritative. Never overridden by heartbeat age — a session idle for a week
        # whose process is up is alive, and its work is not available for adoption.
        return {"state": LIVE_ALIVE, "reason": f"pid {pid} is running",
                "pid": pid, "host": host, "heartbeatAgeSeconds": age,
                "inheritable": False}

    if age is None:
        return {"state": LIVE_UNKNOWN, "reason": "pid gone but heartbeat unreadable",
                "pid": pid, "host": host, "heartbeatAgeSeconds": None,
                "inheritable": False}

    if age > stale_seconds:
        return {"state": LIVE_DEAD,
                "reason": f"pid {pid} gone and heartbeat {int(age)}s old "
                          f"(> {stale_seconds}s)",
                "pid": pid, "host": host, "heartbeatAgeSeconds": age,
                "inheritable": True}

    # Process gone but heartbeat recent: mid-restart or a crash we have not confirmed.
    return {"state": LIVE_UNKNOWN,
            "reason": f"pid {pid} gone but heartbeat only {int(age)}s old",
            "pid": pid, "host": host, "heartbeatAgeSeconds": age,
            "inheritable": False}


def _parse_heartbeat(value) -> datetime.datetime | None:
    """Naive-UTC heartbeat, whichever of the two written formats it is in."""
    return coord_config.parse_utc(value)


def _heartbeat_age(manifest: dict) -> float | None:
    ts = _parse_heartbeat(manifest.get("lastHeartbeat", ""))
    if ts is None:
        return None
    return (coord_config.utcnow_naive() - ts).total_seconds()


def locked_files(sid: str, worktree: str | None = None) -> list[str]:
    """Files this session holds locks for, derived from the lock files ON DISK.

    The manifest's `lockedFiles` array is lossy in both directions and must not be the
    authority for ownership: `claim()` above returns before appending when a lock
    already exists, and `reap_coordination.py` resets the array wholesale. A session can
    therefore hold real locks while its array reads empty — which made the worktree gate
    report a just-edited file as owned by somebody else.

    The lock files are the state that the guard actually enforces against, so they are the
    truth. Derive from them.
    """
    wt = worktree or worktree_id()
    out = []
    for meta in list_locks():
        if meta.get("sessionId") == sid and meta.get("worktree", wt) == wt:
            f = meta.get("file")
            if f:
                out.append(f)
    return sorted(set(out))


# ------------------------------------------------------------------------ operations
def _parse_legacy(text: str) -> dict | None:
    """Parse an older plain-text lock: sessionId / timestamp / path, one per line.

    Without this, an old text lock is unparseable JSON — and the previous behaviour was to
    treat 'unparseable' as 'stale' and `rm -f` it, silently destroying a LIVE peer's lock.
    We recover the session id instead so the normal liveness rules apply.
    """
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return None
    sid = re.sub(r"[^A-Za-z0-9_-]", "", lines[0])
    if not sid:
        return None
    return {
        "file": lines[2] if len(lines) > 2 else "",
        "worktree": worktree_id(),
        "sessionId": sid,
        "domain": "legacy",
        "lockedAt": lines[1] if len(lines) > 1 else "",
        "_legacy": True,
    }


def read_lock(path: str) -> dict | None:
    """Read a lock record in either the current JSON form or the legacy text form."""
    try:
        text = lock_path(path).read_text()
    except OSError:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return _parse_legacy(text)


def owner(path: str) -> dict | None:
    """Return the lock record holding `path`, or None if unlocked/stale."""
    meta = read_lock(path)
    if meta is None or is_stale(meta):
        return None
    return meta


def _declare_from_claim(domain: str) -> None:
    """A lock claim carrying a real domain IS a declaration — record it.

    Step 4 of the session/role unification asks for "declaration enforcement". At this point
    enforcement is better served by ACHIEVING the declaration than by refusing the claim:
    `/coord:claim-files` is the highest-traffic coordination act, the caller has already named its
    domain in the argument, and blocking here would obstruct real work to collect a fact the
    command is already carrying.

    Refusal is reserved for the two acts where an undeclared caller actually misleads a peer —
    spawning a stand-in and opening a negotiation — because those PUBLISH a position on a
    discipline's behalf. A file lock does not.

    `set_domain(inferred=False)` overwrites an inferred guess but the manifest keeps whatever a
    human declared; and it publishes to the shared registry, so the claim makes this session
    visible to `representation()` cross-worktree. Best-effort: never fail a lock over it.
    """
    if not domain or domain in ("unknown", "unscoped"):
        return
    try:
        import session_manifest as _sm
        cwd = str(Path.cwd())
        directory = _sm.sessions_dir(cwd)
        sid = _sm.session_id_from_env()
        if directory is None or not sid:
            return
        current = _sm.read(directory / f"{sid}.json") or {}
        # Never let a claim silently move a domain a human already declared for this session.
        if current.get("domain") not in (None, "", _sm.UNSCOPED) \
                and not current.get("domainInferred"):
            return
        _sm.set_domain(cwd, domain, session_id=sid)
    except Exception:
        pass


def claim(path: str, sid: str | None = None, domain: str = "unknown") -> tuple[bool, str]:
    """Atomically take the lock. Returns (ok, reason)."""
    sid = sid or session_id()
    if not sid:
        return False, "no session id (TERM_SESSION_ID unset and no PPID cache)"
    rel = rel_path(path)
    f = lock_path(path)
    record = {
        "file": rel,
        "worktree": worktree_id(),
        "sessionId": sid,
        "domain": domain,
        "lockedAt": _utcnow(),
    }
    payload = json.dumps(record, indent=2) + "\n"

    for _ in range(2):
        try:
            fd = os.open(str(f), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            existing = read_lock(path)  # handles both JSON and legacy text form
            if existing is None:
                # Genuinely unattributable (empty/corrupt) — no session to protect.
                f.unlink(missing_ok=True)
                continue
            if existing.get("sessionId") == sid:
                return True, "already held by this session"
            if is_stale(existing):
                f.unlink(missing_ok=True)
                continue
            return False, f"held by {existing.get('sessionId','?')} ({existing.get('domain','?')})"
        else:
            with os.fdopen(fd, "w") as fh:
                fh.write(payload)
            _manifest_add(sid, rel)
            return True, "claimed"
    return False, "lost the acquire race"


def release(path: str, sid: str | None = None) -> tuple[bool, str]:
    sid = sid or session_id()
    f = lock_path(path)
    if not f.exists():
        return True, "not locked"
    try:
        meta = json.loads(f.read_text())
    except (OSError, ValueError):
        meta = {}
    if meta.get("sessionId") not in (sid, None, ""):
        return False, f"held by another session ({meta.get('sessionId')})"
    f.unlink(missing_ok=True)
    _manifest_remove(sid, rel_path(path))
    return True, "released"


def _manifest_mutate(sid: str, fn) -> None:
    mf = sessions_dir() / f"{sid}.json"
    try:
        data = json.loads(mf.read_text())
    except (OSError, ValueError):
        return
    files = list(data.get("lockedFiles", []))
    data["lockedFiles"] = fn(files)
    try:
        mf.write_text(json.dumps(data, indent=2) + "\n")
    except OSError:
        pass


def _manifest_add(sid: str, rel: str) -> None:
    _manifest_mutate(sid, lambda f: f if rel in f else f + [rel])


def _manifest_remove(sid: str, rel: str) -> None:
    _manifest_mutate(sid, lambda f: [x for x in f if x != rel])


def list_locks() -> list[dict]:
    out = []
    for f in sorted(locks_dir().glob("*.lock")):
        try:
            meta = json.loads(f.read_text())
        except (OSError, ValueError):
            out.append({"file": "<unparseable>", "lockFile": f.name, "stale": True})
            continue
        meta["lockFile"] = f.name
        meta["stale"] = is_stale(meta)
        out.append(meta)
    return out


# ------------------------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(description="Canonical coordination file locks.")
    ap.add_argument("action", choices=["key", "owner", "claim", "release", "list"])
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--all", action="store_true", help="release: every lock this session holds")
    ap.add_argument("--domain", default="unknown")
    ap.add_argument("--json", action="store_true")
    # parse_known_args, not parse_args: argparse cannot reliably backfill a nargs="*"
    # positional that follows a value-taking optional, so the natural call
    # `claim --domain docs path/one.py` errored with "unrecognized arguments". Callers
    # (including this repo's own skill docs) write it that way, so accept it.
    args, extra = ap.parse_known_args()
    stray = [a for a in extra if not a.startswith("-")]
    if len(stray) != len(extra):
        ap.error(f"unrecognized option(s): {[a for a in extra if a.startswith('-')]}")
    args.paths = list(args.paths) + stray

    if args.action == "list":
        rows = list_locks()
        print(json.dumps(rows, indent=2) if args.json else
              "\n".join(f"{'STALE' if r.get('stale') else 'held '} "
                        f"{r.get('file','?')}  [{r.get('domain','?')}] {r.get('sessionId','?')}"
                        for r in rows) or "(no locks)")
        return 0

    if args.action == "release" and args.all:
        sid = session_id()
        manifest = _session_manifest(sid) or {}
        args.paths = list(manifest.get("lockedFiles", []))
        if not args.paths:
            print("(no locks held)")
            return 0

    if not args.paths:
        ap.error(f"{args.action} needs at least one path")

    results = []
    for p in args.paths:
        if args.action == "key":
            results.append({"path": rel_path(p), "key": lock_key(p)})
        elif args.action == "owner":
            results.append({"path": rel_path(p), "owner": owner(p)})
        elif args.action == "claim":
            ok, why = claim(p, domain=args.domain)
            if ok:
                _declare_from_claim(args.domain)
            results.append({"path": rel_path(p), "ok": ok, "reason": why})
        else:
            ok, why = release(p)
            results.append({"path": rel_path(p), "ok": ok, "reason": why})

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            if "key" in r:
                print(f"{r['key']}  {r['path']}")
            elif "owner" in r:
                o = r["owner"]
                print(f"{r['path']}: " + (f"held by {o['sessionId']} ({o['domain']})" if o else "free"))
            else:
                print(f"{'OK  ' if r['ok'] else 'FAIL'} {r['path']} — {r['reason']}")
    return 0 if all(r.get("ok", True) for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
