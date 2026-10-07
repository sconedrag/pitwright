#!/usr/bin/env python3
"""
session_registry_hook.py — SessionStart hook that registers the session.

Wire on the SessionStart event (user-applied via /update-config; settings.json is
deny-listed). On each session start it:
  1. registers the session in the cross-worktree registry (scripts/session_registry.py),
     recording the native session UUID, cwd, and the long-lived Claude parent PID; and
  2. writes a per-worktree self-marker `.claude/coordination/current-session.json`
     = {"sessionId": "<uuid>", "pid": <ppid>} so the /coord:sessions skill can identify
     *itself* for self-naming; and
  3. writes the `sessions/<session id>.json` manifest that `lock_guard.py`
     reads, so file locking is ON BY DEFAULT.

Step 3 exists because the guard used to fail open — no manifest meant no locking, so
skipping `/coord:start-session` opted a session out of coordination entirely, for itself and for
every peer trying to see it (most worktrees had no sessions/ dir at all). Steps 1 and 3
keyed on DIFFERENT identifiers in v0.1 — the native UUID and TERM_SESSION_ID respectively —
which is why registering in step 1 never satisfied the guard. Since v0.2 the manifest key
comes from `_identity`, normally the same native UUID. `session_manifest.ensure`
is idempotent and will not flatten a manifest that `/coord:start-session` already declared.

Reads the hook payload as JSON on stdin: {session_id, cwd, transcript_path, ...}.
Always exits 0 — a registry hiccup must never block a session from starting.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

try:
    import session_manifest
except ImportError:  # pragma: no cover - never let a missing helper block session start
    session_manifest = None


def _git_toplevel(cwd: str) -> Path | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             cwd=cwd, capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _write_self_marker(cwd: str, session_id: str, pid: int) -> None:
    top = _git_toplevel(cwd)
    if top is None:
        return
    coord = top / ".claude" / "coordination"
    try:
        coord.mkdir(parents=True, exist_ok=True)
        (coord / "current-session.json").write_text(
            json.dumps({"sessionId": session_id, "pid": pid}, indent=2))
    except OSError:
        pass


def _register(session_id: str, cwd: str, pid: int) -> None:
    try:
        subprocess.run(
            ["python3", str(SCRIPT_DIR / "session_registry.py"), "register",
             "--session-id", session_id, "--cwd", cwd, "--pid", str(pid)],
            cwd=cwd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pass


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}
    session_id = str(payload.get("session_id", "")).strip()
    cwd = str(payload.get("cwd", "") or os.getcwd())
    if not session_id:
        return 0  # nothing to register without a UUID
    pid = os.getppid()  # long-lived Claude parent (matches the $PPID owner convention)
    _register(session_id, cwd, pid)
    _write_self_marker(cwd, session_id, pid)
    _ensure_manifest(cwd, session_id, pid)
    return 0


def _ensure_manifest(cwd: str, session_id: str, pid: int) -> None:
    """Turn on file locking for this session. Best-effort by design: a session that starts
    without a manifest is the pre-existing behaviour, not a regression, so a failure here
    degrades to the old fail-open rather than blocking the session from starting."""
    if session_manifest is None:
        return
    try:
        # Key the manifest with the SAME resolver every reader uses, not the payload id.
        # Under current Claude Code the two are equal (CLAUDE_CODE_SESSION_ID is the payload
        # id); if they ever diverge, keying on the payload would put the manifest where no
        # skill script looks — the two-namespace bug again. Record the divergence instead.
        resolved = session_manifest.session_id_from_env(pid)
        if resolved and resolved != session_manifest.sanitize_session_id(session_id):
            _log_identity_mismatch(resolved, session_id)
        session_manifest.ensure(cwd, pid, native_session_id=session_id)
    except Exception:  # noqa: BLE001 - a SessionStart hook must never raise
        pass


def _log_identity_mismatch(resolved: str, payload_id: str) -> None:
    """Expected only when CLAUDE_CODE_SESSION_ID is absent (an older Claude Code)."""
    try:
        import _agent_channel
        _agent_channel.append_event(
            "IDENTITY_MISMATCH",
            f"coordination id {resolved} != hook payload session_id {payload_id}",
            sid=resolved)
    except Exception:  # noqa: BLE001 - diagnostics only
        pass


if __name__ == "__main__":
    sys.exit(main())
