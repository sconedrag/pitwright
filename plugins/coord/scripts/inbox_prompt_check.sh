#!/usr/bin/env bash
# inbox_prompt_check.sh — UserPromptSubmit hook: surface peer requests that are blocking someone.
#
# Why this event specifically: it is the ONLY trigger that fires every turn regardless of
# which tools are used. The Edit|Write guard misses a session that spends an hour running
# builds and git; SessionStart fires once. This is the sole delivery path for the
# long-running non-editing session, which is exactly the case that left a real
# "/coord:release-files would unblock me" request unanswered for hours.
#
# Because it fires every turn it is deliberately QUIET:
#   - only OPEN inbound requests (a peer is blocked on you) — never mere unread traffic
#   - deduped by fingerprint: a NEW/CHANGED set surfaces immediately, an unchanged one
#     re-surfaces at most every REMIND_SECONDS. Repeating an identical nag every turn is
#     precisely how agents learn to ignore a channel.
#
# Wired by the plugin's hooks/hooks.json.
#
# Fire-and-forget: any error -> exit 0, silent. Never blocks a prompt.
set +e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${CLAUDE_PROJECT_DIR:-$(pwd)}"
cd "$ROOT" 2>/dev/null || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

# Drain hook stdin so the pipe never blocks.
cat >/dev/null 2>&1

COORD_SCRIPT_DIR="$SCRIPT_DIR" python3 - <<'PY' 2>/dev/null
import hashlib, json, os, sys, time
sys.path.insert(0, os.environ["COORD_SCRIPT_DIR"])

REMIND_SECONDS = 1200  # 20 min between repeats of an UNCHANGED open set
STATE = ".claude/coordination/inbox-watch.json"

try:
    import agent_message as am
    open_msgs = [m for m in am.inbox(show_all=True, mark=False) if m["_status"] == "open"]
except Exception:
    sys.exit(0)

if not open_msgs:
    # Clear state so the next new request surfaces immediately rather than being
    # suppressed by a stale fingerprint.
    try:
        os.path.exists(STATE) and os.remove(STATE)
    except OSError:
        pass
    sys.exit(0)

fp = hashlib.sha256(
    "|".join(sorted(m["msgId"] for m in open_msgs)).encode()
).hexdigest()[:16]

try:
    with open(STATE) as fh:
        state = json.load(fh)
except (OSError, ValueError):
    state = {}

now = time.time()
same = state.get("fingerprint") == fp
if same and (now - float(state.get("emittedAt", 0) or 0)) < REMIND_SECONDS:
    sys.exit(0)

lines = [f"📬 {len(open_msgs)} peer request(s) awaiting your reply — a peer session is blocked:"]
for m in open_msgs[:5]:
    who = m["from"].get("humanName") or m["from"].get("sessionId", "?")
    lines.append(f"  [{m['intent']}] {m['subject']} — from {who}  ({m['msgId']})")
if len(open_msgs) > 5:
    lines.append(f"  … and {len(open_msgs) - 5} more.")
lines.append("  Read and reply with /coord:inbox.")
lines.append("  You may auto-release your OWN lock if `git status --porcelain -- <path>` is clean; "
             "anything touching code or git needs your operator's confirmation first.")

try:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as fh:
        json.dump({"fingerprint": fp, "emittedAt": now}, fh)
except OSError:
    pass

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "\n".join(lines),
    }
}))
PY
exit 0
