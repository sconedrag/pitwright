#!/usr/bin/env bash
# closeout_stop_check.sh — Stop-hook backstop for closeout hygiene.
#
# Fires at the end of each assistant turn. Catches the "abandoned just before
# commit/push" failure mode: a completed unit of work left uncommitted or
# committed-but-unpushed while attention moved on to the next branch of the plan.
#
# ADVISORY ONLY — always exit 0, never blocks the stop (blocking Stop is hostile
# UX). The reminder is STALE-GATED + DEDUPED by closeout_ledger.py
# (stopcheck): it speaks up only when the same drift has sat idle across several
# turns, and never re-warns the identical state. The reliable model-facing catch
# is the SessionStart board (which surfaces the same signal next session); this
# hook is the early warning within a session.
#
# Wired by the plugin's hooks/hooks.json.
#
# Fire-and-forget: missing python/git or any error -> exit 0, silent.
set +e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${CLAUDE_PROJECT_DIR:-$(pwd)}"
cd "$ROOT" 2>/dev/null || exit 0
command -v python3 >/dev/null 2>&1 || exit 0

# Drain any hook stdin (Stop payload) so the pipe never blocks.
cat >/dev/null 2>&1

MSG="$(python3 "$SCRIPT_DIR/closeout_ledger.py" stopcheck 2>/dev/null)"
if [ -n "$MSG" ]; then
  # Advisory: surface in the hook output (stderr). Never block.
  echo "$MSG" >&2
fi

# Unanswered cross-session requests — the messaging analogue of closeout drift.
# A session going quiet with an inbound request open leaves a PEER blocked (this is
# exactly how a real "/coord:release-files would unblock me" ask sat unanswered for hours);
# one going quiet with an outbound request open is itself waiting and should escalate
# rather than poll. Advisory, never blocking.
COORD_SCRIPT_DIR="$SCRIPT_DIR" python3 - <<'PY' >&2 2>/dev/null
try:
    import os, sys
    sys.path.insert(0, os.environ["COORD_SCRIPT_DIR"])
    import agent_message as am

    inbound = [m for m in am.inbox(show_all=True, mark=False) if m["_status"] == "open"]
    outbound = [m for m in am.outbox(open_only=True)]
    if inbound:
        print(f"\n[inbox] {len(inbound)} peer request(s) still unanswered — a peer is blocked on you:")
        for m in inbound[:3]:
            who = m["from"].get("humanName") or m["from"].get("sessionId", "?")
            print(f"   [{m['intent']}] {m['subject']} — from {who} ({m['msgId']})")
        print("   Read and reply with /coord:inbox.")
    if outbound:
        print(f"\n[outbox] {len(outbound)} request(s) you sent are still unanswered.")
        print("   If it is blocking you, escalate to your operator rather than waiting.")
except Exception:
    pass
PY
exit 0
