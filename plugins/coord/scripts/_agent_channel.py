#!/usr/bin/env python3
"""
_agent_channel.py — shared CROSS-WORKTREE coordination channel (Interface Coordination).

The existing `.claude/coordination/` is gitignored + per-worktree, so it cannot carry
state between worktrees. The one location that IS shared across every worktree of a repo
on a machine is the common git dir: `git rev-parse --git-common-dir` returns the SAME
path (the main `.git`) from the primary checkout AND every linked worktree. A directory
under it is therefore auto-shared, never committed (git ignores unknown dirs in `.git`),
and survives `git worktree prune`.

This module is the single source of truth for that channel ($CHANNEL):

    $CHANNEL = <git-common-dir>/agent-coordination/
      overlap-report.json     # latest cross-worktree interface-overlap snapshot
      intents/<wt>.json        # optional plan-level intent (enrichment)
      notes/<iface>.log        # async cross-worktree notes (independent-session comms)
      contracts/<iface>.json   # thin record: agreed interface + merged-commit pointer
      events.log               # shared event stream (same pipe format as the per-worktree one)

API: channel_dir(), worktree_id(), iface_key(name), append_event(...), read_events_since(...).
Reuses `_coord_lock.session_id()` for the session id (same resolution as the rest of the harness).

CLI:
    python3 scripts/_agent_channel.py --print-dir   # absolute $CHANNEL path
    python3 scripts/_agent_channel.py --print-wt    # this worktree's id
    python3 scripts/_agent_channel.py --event TYPE "message"
    python3 scripts/_agent_channel.py --tail [N]    # last N events (default 20)
"""

from __future__ import annotations

import datetime
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _identity  # noqa: E402
try:
    import _coord_lock  # session_id()
except Exception:  # pragma: no cover - defensive
    _coord_lock = None

SUBDIRS = ("intents", "notes", "contracts")


def _git(*args: str) -> str:
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def channel_dir() -> Path:
    """Absolute $CHANNEL path (shared across all worktrees of this repo on the machine).

    `AGENT_CHANNEL_DIR` overrides the location, so tests can run against a hermetic channel
    instead of the live shared one. Mirrors `BUILD_QUEUE_COORD_DIR` in _build_semaphore.py.
    """
    override = os.environ.get("AGENT_CHANNEL_DIR")
    if override:
        ch = Path(override)
        try:
            for sub in SUBDIRS:
                (ch / sub).mkdir(parents=True, exist_ok=True)
            ch.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return ch
    common = _git("rev-parse", "--path-format=absolute", "--git-common-dir")
    if not common:
        # Fallback: relative common dir resolved against cwd, else local .git.
        rel = _git("rev-parse", "--git-common-dir") or ".git"
        common = str(Path(rel).resolve())
    ch = Path(common) / "agent-coordination"
    try:
        for sub in SUBDIRS:
            (ch / sub).mkdir(parents=True, exist_ok=True)
        ch.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return ch


def worktree_id() -> str:
    """Stable id of the current worktree: 'main' for the primary checkout, else the
    worktree-root basename. Must match the documented formula everywhere it is derived
    (e.g. a bash hook deriving the same worktree id)."""
    git_dir = _git("rev-parse", "--absolute-git-dir")
    top = _git("rev-parse", "--show-toplevel")
    if git_dir and "/worktrees/" in git_dir and top:
        wt = os.path.basename(top)
    else:
        wt = "main"
    return re.sub(r"[^A-Za-z0-9._+-]", "_", wt) or "main"


def session_id() -> str:
    if _coord_lock is not None:
        try:
            return _coord_lock.session_id() or "anon"
        except Exception:
            pass
    return _identity.session_id() or "anon"


def iface_key(name: str) -> str:
    """Sanitize an interface id into a filesystem-safe key (Swift symbol or table:NAME)."""
    return re.sub(r"[^A-Za-z0-9._+:-]", "_", (name or "").strip()) or "_"


def _utcnow() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def append_event(event_type: str, msg: str, *, sid: str | None = None, wt: str | None = None) -> None:
    """Append one line to the shared events.log: `ts|TYPE|sid|wt|msg` (pipe-safe)."""
    sid = sid or session_id()
    wt = wt or worktree_id()
    et = re.sub(r"[^A-Za-z0-9_]", "_", event_type).upper() or "EVENT"
    clean = (msg or "").replace("|", "/").replace("\n", " ").strip()
    line = f"{_utcnow()}|{et}|{sid}|{wt}|{clean}\n"
    try:
        with open(channel_dir() / "events.log", "a") as fh:
            fh.write(line)
    except OSError:
        pass


def read_events_since(offset: int):
    """Return (new_text, new_offset) for the shared events.log from byte `offset`."""
    log = channel_dir() / "events.log"
    try:
        size = log.stat().st_size
    except OSError:
        return "", offset
    if size <= offset:
        return "", size
    try:
        with open(log, "rb") as fh:
            fh.seek(max(0, offset))
            data = fh.read()
        return data.decode("utf-8", "replace"), size
    except OSError:
        return "", offset


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] == "--print-dir":
        print(channel_dir())
        return 0
    if args[0] == "--print-wt":
        print(worktree_id())
        return 0
    if args[0] == "--event" and len(args) >= 3:
        append_event(args[1], args[2])
        print(f"event appended: {args[1]}")
        return 0
    if args[0] == "--tail":
        n = int(args[1]) if len(args) > 1 and args[1].isdigit() else 20
        log = channel_dir() / "events.log"
        if log.exists():
            lines = log.read_text(errors="replace").splitlines()[-n:]
            print("\n".join(lines))
        else:
            print("(no events yet)")
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
