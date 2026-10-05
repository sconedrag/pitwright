#!/usr/bin/env bash
# session_end_cleanup.sh — Coordination Harness v2, Component 8.
# SessionEnd hook: when a session ends, release its file locks and archive its
# manifest as "completed". Attacks the root cause of orphan-lock accumulation —
# sessions that never call /coord:complete-session. Fire-and-forget (SessionEnd cannot
# block), so always exit 0.
#
# Resolves the coordination session id from $TERM_SESSION_ID (the id the rest of
# the system locks on) — NOT the SessionEnd stdin `session_id`, which is Claude's
# own UUID and does not match the lock/manifest keys.
#
# Wired by the plugin's hooks/hooks.json.
set +e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${CLAUDE_PROJECT_DIR:-$(pwd)}"
cd "$ROOT" 2>/dev/null || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

SID="$(printf '%s' "${TERM_SESSION_ID:-}" | tr -cd 'A-Za-z0-9_-')"
[ -z "$SID" ] && exit 0
[ -f ".claude/coordination/sessions/$SID.json" ] || exit 0

# Release this session's locks + archive its manifest as completed.
python3 "$SCRIPT_DIR/reap_coordination.py" "$SID" --complete >/dev/null 2>&1
exit 0
