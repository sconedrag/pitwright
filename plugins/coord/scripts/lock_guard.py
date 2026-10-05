#!/usr/bin/env python3
"""lock_guard.py — PreToolUse hook (Edit|Write|MultiEdit|NotebookEdit): make file locks real.

Without this hook the lock files written by `/coord:claim-files` are advisory: nothing stops a
second session from editing a file the first one holds. This hook enforces them at edit
time, and claims the file for the editing session on first touch, so a session that never
ran `/coord:claim-files` still shows its working set on the board.

Policy, per edit:
  - session not registered (no manifest under .claude/coordination/sessions/) -> allow, no lock.
    Locking is opt-in per session; the SessionStart hook registers sessions by default.
  - path outside the project                                                 -> allow.
  - unlocked, stale, or already held by this session                         -> claim, allow.
  - held by ANOTHER session whose owner is live (or whose liveness is unknown) -> BLOCK (exit 2)
    with instructions for asking the owner. `COORD_LOCKS_ADVISORY=1` (or
    `"locks_advisory": true` in .claude/coord.json) turns the block into a warning.

Locks are keyed by worktree, so two sessions in DIFFERENT worktrees never block each other —
they edit different physical files, and their collision (if any) happens at merge time,
which `/coord:adjacency` and `/coord:worktree-overlap` report.

Every decision about ownership and staleness goes through `coord_locks` — the same code
`/coord:claim-files` uses — so the hook and the skills cannot disagree about who holds a file.

Failure policy: an unexpected error ALLOWS the edit and prints a one-line notice. A broken
guard that blocked every edit would be worse than no guard; a broken guard that is silent
would be indistinguishable from a working one.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

PATH_KEYS = ("file_path", "notebook_path")


def _target_path(payload: dict) -> str:
    tool_input = payload.get("tool_input") or {}
    for key in PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _inside(root: Path, path: str) -> bool:
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _owner_label(meta: dict) -> str:
    sid = meta.get("sessionId", "?")
    try:
        import session_registry  # optional: a human name if the session set one
        entry = session_registry._read_store().get("sessions", {}).get(sid, {})
        return entry.get("humanName") or sid
    except Exception:  # noqa: BLE001 - a nicer label is never worth failing over
        return sid


def _blocked_message(rel: str, meta: dict, advisory: bool) -> str:
    who = _owner_label(meta)
    head = "FILE LOCKED (advisory — edit allowed)" if advisory else "FILE LOCKED"
    return "\n".join([
        f"{head} — {rel}",
        f"  held by: {who}  [{meta.get('domain', 'unknown')}]  "
        f"in worktree '{meta.get('worktree', '?')}'",
        "",
        "  Ask the owner for it — they can release it and reply:",
        f"     /coord:ask-lock {rel} \"<why you need it>\"",
        "  Or see who is blocking you and whether they are still live:  /coord:blocked " + rel,
        "",
        "  If you are a SUBAGENT, do not negotiate: you share your parent's session identity,",
        "  so a peer cannot tell you apart. Report the block to your parent instead.",
        "",
        "  Do NOT delete the lock file. A lock whose owning process is alive is never stale —",
        "  the owner may be mid-edit. (Different worktrees never block each other.)",
    ])


def decide(payload: dict) -> tuple[int, str]:
    """Return (exit_code, stderr_text). Pure enough to test without a subprocess."""
    import coord_config
    import coord_locks

    path = _target_path(payload)
    if not path:
        return 0, ""

    root = coord_config.project_root()
    if not _inside(root, path):
        return 0, ""

    sid = coord_locks.session_id()
    if not sid:
        return 0, ""
    manifest = coord_locks._session_manifest(sid)
    if manifest is None:
        return 0, ""  # unregistered session: locking is off for it

    ok, reason = coord_locks.claim(path, sid, domain=manifest.get("domain") or "unknown")
    if ok:
        # An edit is activity: refresh the heartbeat so a session whose pid could not be
        # recorded is not judged stale (and its locks reclaimed) while it is working.
        try:
            import session_manifest
            session_manifest.touch(str(root), session_id=sid)
        except Exception:  # noqa: BLE001 - bookkeeping, never a reason to block
            pass
        return 0, ""

    rel = coord_locks.rel_path(path)
    meta = coord_locks.read_lock(path)
    if meta is None:
        # Lost a race to a claim that has since vanished; the next attempt will succeed.
        return 2, f"Lock race on {rel} — another session acquired it first. Retry the edit."

    advisory = bool(coord_config.get("locks_advisory"))
    return (0 if advisory else 2), _blocked_message(rel, meta, advisory)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        code, message = decide(payload)
    except Exception as exc:  # noqa: BLE001 - see "Failure policy" in the module docstring
        print(f"[coord lock_guard] internal error, edit allowed: {exc}", file=sys.stderr)
        return 0
    if message:
        print(message, file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
