#!/usr/bin/env bash
# session_start.sh — SessionStart hook for the coord plugin.
#
# Runs, in order (sequential on purpose: the board must see this session already registered):
#   1. register the session (session_registry_hook.py, fed this hook's stdin payload) —
#      which also writes the per-worktree manifest that turns file locking on;
#   2. sweep orphan locks (non-destructive: a lock whose owner is live is never touched);
#   3. seed a closeout item for this branch if it carries uncommitted/unpushed work;
#   4. emit, as SessionStart additionalContext: the coordination board, the closeout line,
#      peer requests awaiting a reply, and a worktree advisory when peers share this checkout.
#
# Fire-and-forget: always exits 0 and emits valid JSON (or nothing). A coordination hiccup
# must never stop a session from starting.
set +e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v python3 >/dev/null 2>&1 || exit 0

PAYLOAD="$(cat)"
ROOT="${CLAUDE_PROJECT_DIR:-}"
[ -n "$ROOT" ] && [ -d "$ROOT" ] || ROOT="$(pwd)"
ROOT="$(cd "$ROOT" && git rev-parse --show-toplevel 2>/dev/null || echo "$ROOT")"
cd "$ROOT" 2>/dev/null || exit 0
git rev-parse --git-dir >/dev/null 2>&1 || exit 0   # not a git repo: nothing to coordinate

printf '%s' "$PAYLOAD" | python3 "$SCRIPT_DIR/session_registry_hook.py" >/dev/null 2>&1
python3 "$SCRIPT_DIR/reap_coordination.py" --orphan-locks >/dev/null 2>&1
python3 "$SCRIPT_DIR/closeout_ledger.py" autoseed >/dev/null 2>&1

COORD_SCRIPT_DIR="$SCRIPT_DIR" python3 - <<'PY' 2>/dev/null
import json, os, sys
sys.path.insert(0, os.environ["COORD_SCRIPT_DIR"])

parts = ["Parallel-session coordination (coord plugin):"]

try:
    import render_board
    board = render_board.render_text(render_board.collect())
    if board:
        parts.append(board)
except Exception:
    pass

try:
    import closeout_ledger
    line = closeout_ledger.surface_line(closeout_ledger._repo_root())
    if line:
        parts.append(line)
except Exception:
    pass

# Peer requests are listed individually: an unanswered one is blocking a peer right now.
try:
    import agent_message as am
    open_reqs = [m for m in am.inbox(show_all=True, mark=False) if m["_status"] == "open"]
    if open_reqs:
        lines = [f"\n📬 {len(open_reqs)} peer request(s) awaiting your reply — each is blocking a peer:"]
        for m in open_reqs[:5]:
            who = m["from"].get("humanName") or m["from"].get("sessionId", "?")
            lines.append(f"  [{m['intent']}] {m['subject']}  — from {who}  ({m['msgId']})")
        if len(open_reqs) > 5:
            lines.append(f"  … and {len(open_reqs) - 5} more.")
        lines.append("  Read and reply with /coord:inbox.")
        parts.append("\n".join(lines))
    elif am.peek().get("unreadBytes"):
        parts.append("\n📬 Unread messages from peer sessions — /coord:inbox")
except Exception:
    pass

# Peers sharing THIS checkout (manifests are per worktree, so a session in another worktree
# is not counted — it edits different physical files).
try:
    import coord_locks
    me = coord_locks.session_id()
    sessions = coord_locks.sessions_dir()
    peers = 0
    if sessions.is_dir():
        for f in sessions.glob("*.json"):
            if f.stem == me:
                continue
            if coord_locks.liveness(f.stem)["state"] != coord_locks.LIVE_DEAD:
                peers += 1
    if peers:
        parts.append(
            f"\n⚠️ {peers} other session(s) are active in this checkout. Concurrent sessions in "
            "one checkout contend for the same files; for code work, give each session its own "
            "git worktree (e.g. `claude --worktree <name>`). If you stay here, claim files "
            "before editing (/coord:claim-files) and expect to wait on locks."
        )
except Exception:
    pass

if len(parts) > 1:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": "\n".join(parts),
    }}))
PY
exit 0
