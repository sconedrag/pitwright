#!/usr/bin/env python3
"""
_coord_lock.py — Coordination Harness v2, shared advisory-lock primitive.

A named, cross-session, cross-process advisory lock living in the existing
`.claude/coordination/locks/` directory. Use it for a short critical section shared by every session in a checkout, e.g. a script
that rewrites one shared generated file, or a build step that must not run twice at once.

Design:
  - Atomic acquire via `os.open(..., O_CREAT | O_EXCL)` — the canonical
    race-free file-lock primitive (portable; no `mv -n` macOS caveat).
  - Lock file: `.claude/coordination/locks/__<name>__.lock`, JSON
    `{name, sessionId, pid, acquiredAt}`.
  - Re-entrant per session: if the live lock is already held by THIS session
    id, acquire() returns True without blocking (supports stacked operations).
  - Self-healing: a lock whose holder PID is dead, or whose `acquiredAt` is
    older than `stale_after`, is reaped and re-acquired.
  - Session id from `_identity.session_id()` (CLAUDE_CODE_SESSION_ID → TERM_SESSION_ID →
    /tmp/.claude-session-<ppid>.id), shared with coord_locks; self is plain equality.

Module API:
    from _coord_lock import acquire, release, holder
    if acquire("shared-config", timeout=120, stale_after=300):
        try: ...
        finally: release("shared-config")

CLI:
    python3 scripts/_coord_lock.py acquire shared-config --timeout 120 --stale-after 300
        exit 0 = acquired, exit 1 = timed out
    python3 scripts/_coord_lock.py release shared-config
    python3 scripts/_coord_lock.py holder  shared-config   # prints JSON or "none"
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coord_config  # noqa: E402
import _identity  # noqa: E402


def _repo_root() -> Path:
    return coord_config.project_root()


def _locks_dir() -> Path:
    d = _repo_root() / ".claude" / "coordination" / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _lock_path(name: str, locks_dir: Path | None = None) -> Path:
    safe = "".join(c for c in name if c.isalnum() or c in "_-")
    base = Path(locks_dir) if locks_dir is not None else _locks_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base / f"__{safe}__.lock"


def _utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def session_id() -> str:
    """This session's coordination id — see `_identity` for the resolution order."""
    return _identity.session_id()


def _pid_alive(pid: int) -> bool:
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


def holder(name: str, locks_dir: Path | None = None) -> dict | None:
    path = _lock_path(name, locks_dir)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _age_seconds(meta: dict) -> float:
    try:
        parsed = datetime.datetime.fromisoformat(meta.get("acquiredAt", "").rstrip("Z"))
        now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        return (now - parsed).total_seconds()
    except ValueError:
        return float("inf")


def _is_reapable(meta: dict, stale_after: float) -> bool:
    pid = int(meta.get("pid", 0) or 0)
    if not _pid_alive(pid):
        return True
    return _age_seconds(meta) > stale_after


def _try_create(path: Path, sid: str, name: str) -> bool:
    """Atomic create-if-absent via O_EXCL. Returns True if we created it."""
    payload = json.dumps(
        {"name": name, "sessionId": sid, "pid": os.getpid(), "acquiredAt": _utcnow_iso()},
        indent=2,
    )
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    except OSError:
        return False
    try:
        os.write(fd, payload.encode("utf-8"))
    finally:
        os.close(fd)
    return True


def acquire(name: str, *, timeout: float = 120.0, stale_after: float = 300.0,
            poll: float = 0.5, quiet: bool = False, locks_dir: Path | None = None) -> bool:
    """
    Acquire the named lock. Blocks up to `timeout` seconds.
    Returns True on success, False on timeout. Re-entrant per session.

    `locks_dir` overrides the default per-worktree locks directory — pass the
    shared cross-worktree channel (e.g. `_agent_channel.channel_dir()/"locks"`)
    to get mutual exclusion that spans every worktree of the repo.
    """
    sid = session_id() or f"anon-{os.getpid()}"
    path = _lock_path(name, locks_dir)
    deadline = time.monotonic() + timeout
    announced = False

    while True:
        existing = holder(name, locks_dir)
        if existing is not None:
            if _identity.is_self(existing.get("sessionId"), sid):
                return True  # re-entrant: this session already holds it
            if _is_reapable(existing, stale_after):
                # Holder is dead or stale — reap and retry immediately.
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            # Held by a live peer — wait.
            if not quiet and not announced:
                hpid = existing.get("pid", "?")
                sys.stderr.write(
                    f"[coord-lock] '{name}' held by session {existing.get('sessionId','?')} "
                    f"(pid {hpid}, age {int(_age_seconds(existing))}s). Waiting…\n"
                )
                announced = True
        else:
            if _try_create(path, sid, name):
                return True
            # Lost a creation race — loop and re-evaluate.
            continue

        if time.monotonic() >= deadline:
            if not quiet:
                sys.stderr.write(f"[coord-lock] timed out waiting for '{name}' after {timeout}s.\n")
            return False
        time.sleep(poll)


def release(name: str, locks_dir: Path | None = None) -> bool:
    """Release the named lock if owned by this session. Returns True if released."""
    sid = session_id() or f"anon-{os.getpid()}"
    meta = holder(name, locks_dir)
    if meta is None:
        return False
    if not _identity.is_self(meta.get("sessionId"), sid):
        return False  # not ours — never release a peer's lock
    try:
        _lock_path(name, locks_dir).unlink()
        return True
    except OSError:
        return False


def _main() -> int:
    parser = argparse.ArgumentParser(description="Coordination advisory lock.")
    parser.add_argument("action", choices=["acquire", "release", "holder"])
    parser.add_argument("name")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--stale-after", type=float, default=300.0)
    parser.add_argument("--poll", type=float, default=0.5)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    if args.action == "acquire":
        ok = acquire(args.name, timeout=args.timeout, stale_after=args.stale_after,
                     poll=args.poll, quiet=args.quiet)
        return 0 if ok else 1
    if args.action == "release":
        release(args.name)
        return 0
    # holder
    meta = holder(args.name)
    print(json.dumps(meta) if meta else "none")
    return 0


if __name__ == "__main__":
    sys.exit(_main())
