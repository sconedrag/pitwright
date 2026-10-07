#!/usr/bin/env bash
# session_end_cleanup.sh — Coordination Harness v2, Component 8.
# SessionEnd hook: when a session ends, release its file locks and archive its
# manifest as "completed". Attacks the root cause of orphan-lock accumulation —
# sessions that never call /coord:complete-session. Fire-and-forget (SessionEnd cannot
# block), so always exit 0.
#
# Releases exactly ONE key: this session's id from scripts/_identity.py — the same id
# the edit guard locks under ($CLAUDE_CODE_SESSION_ID, else the v0.1 $TERM_SESSION_ID).
# Never "every id we might answer to": every tmux pane inherits ONE TERM_SESSION_ID, so a
# session ending in one pane would otherwise archive a still-running v0.1 session's
# manifest in another and release its locks.
#
# Wired by the plugin's hooks/hooks.json.
set +e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${CLAUDE_PROJECT_DIR:-$(pwd)}"
cd "$ROOT" 2>/dev/null || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

SID="$(python3 "$SCRIPT_DIR/_identity.py" 2>/dev/null | tr -cd 'A-Za-z0-9_-')"
[ -z "$SID" ] && exit 0
[ -f ".claude/coordination/sessions/$SID.json" ] || exit 0

# Release this session's locks + archive its manifest as completed.
python3 "$SCRIPT_DIR/reap_coordination.py" "$SID" --complete >/dev/null 2>&1
exit 0
