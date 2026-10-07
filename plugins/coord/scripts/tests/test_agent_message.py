#!/usr/bin/env python3
"""
test_agent_message.py — protocol invariants for scripts/agent_message.py.

Runs entirely against a hermetic channel (AGENT_CHANNEL_DIR), never the live shared one.

Covers:
  1. ADDRESSING — a message lands in the recipient's mailbox; the sender never sees its own.
  2. CURSORS    — an unread message is delivered once, then marked; --all replays it.
  3. THREADING  — a reply joins the thread and flips the original from open → answered.
  4. TTL        — an unanswered request past its expiry reads as expired, not open.
  5. GUARDS     — 'all' needs --broadcast; DEFER needs a trigger.
  6. CONCURRENCY— N parallel senders lose zero lines (the property the whole durable
                  layer rests on, since delivery must survive session death).
  7. ROTATION   — an oversized mailbox rolls without dropping the live file.

Run: python3 scripts/tests/test_agent_message.py   (exit 0 = pass)
No external deps (plain asserts).
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
os.environ.pop("CLAUDE_CODE_SESSION_ID", None)  # hermetic: tests pin identity themselves

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        FAILURES.append(label)


def _fresh(channel: Path, sid: str):
    """Import agent_message bound to a hermetic channel and a chosen session id."""
    os.environ["AGENT_CHANNEL_DIR"] = str(channel)
    os.environ["TERM_SESSION_ID"] = sid
    for mod in ("agent_message", "_agent_channel", "coord_locks"):
        sys.modules.pop(mod, None)
    import agent_message  # noqa: E402
    agent_message.whoami = lambda: {                      # type: ignore[assignment]
        "sessionId": sid, "humanName": f"name-{sid}",
        "worktree": f"wt-{sid}", "branch": "b",
    }
    return agent_message


def test_addressing_and_cursors(channel: Path) -> None:
    print("\n[1] addressing, cursors, own-message exclusion")
    A = _fresh(channel, "SESSA")
    A.send("session:SESSB", "REQUEST", intent="lock-release",
           subject="need ChatPanelView", body="3-line change")

    a_in = A.inbox()
    check(a_in == [], "sender does not receive its own message")

    B = _fresh(channel, "SESSB")
    b_in = B.inbox()
    check(len(b_in) == 1 and b_in[0]["subject"] == "need ChatPanelView",
          "recipient receives the addressed message")
    check(b_in[0]["_status"] == "open", "a REQUEST arrives 'open'")

    check(B.inbox() == [], "read cursor advances — no redelivery")
    check(len(B.inbox(show_all=True)) == 1, "--all replays already-read messages")


def test_threading(channel: Path) -> None:
    print("\n[2] threading and answered-state")
    A = _fresh(channel, "SESSA")
    msg = A.send("session:SESSB", "REQUEST", intent="lock-release", subject="lock please")

    B = _fresh(channel, "SESSB")
    reply = B.send("session:SESSA", "GRANT", subject="Re: lock please",
                   in_reply_to=msg["msgId"], thread_id=msg["threadId"])
    check(reply["threadId"] == msg["threadId"], "reply inherits the threadId")

    rows = B.thread(msg["threadId"])
    check(len(rows) == 2, f"thread holds both messages (got {len(rows)})")

    original = [m for m in rows if m["msgId"] == msg["msgId"]][0]
    check(original["_status"] == "answered", "a granted REQUEST flips open → answered")

    A2 = _fresh(channel, "SESSA")
    check(A2.outbox(open_only=True) == [], "sender's outbox no longer lists it as open")


def test_ttl(channel: Path) -> None:
    print("\n[3] TTL expiry")
    A = _fresh(channel, "SESSA")
    msg = A.send("session:SESSB", "REQUEST", subject="stale ask", ttl_hours=-1)  # already past
    rows = [m for m in A.outbox() if m["msgId"] == msg["msgId"]]
    check(rows and rows[0]["_status"] == "expired", "past-TTL request reads as expired")

    B = _fresh(channel, "SESSB")
    check(all(m["msgId"] != msg["msgId"] for m in B.inbox(open_only=True)),
          "an expired request is not listed as awaiting reply")

    # This assertion used to be `isinstance(ids, list)` under the label "runs clean" — a
    # type check that passed while `expire_stale()` was UNREACHABLE (its guard demanded
    # _status == "open", which `_decorate` never assigns to a past-expiry message), so it
    # reaped nothing and reported an honest-looking zero for months. A reaper must be
    # tested on what it REAPS, never on what it returns.
    res = A.expire_stale()
    check(set(res) == {"expired", "expiredUnread"},
          "expire_stale() reports read and never-read timeouts separately")
    check(msg["msgId"] in res["expired"] + res["expiredUnread"],
          "the stale request is actually reaped, not merely enumerated")
    check(msg["msgId"] in res["expired"],
          "SESSB read it before it aged out, so it is a timeout and not a dead letter")


def test_guards(channel: Path) -> None:
    print("\n[4] guards against the failure modes that killed v1")
    A = _fresh(channel, "SESSA")

    try:
        A.send("all", "FYI", subject="hi everyone")
        check(False, "'all' without --broadcast must be refused")
    except SystemExit:
        check(True, "'all' without --broadcast is refused (addressed-by-default)")

    ok = A.send("all", "FYI", subject="genuinely everyone", allow_broadcast=True)
    check(ok["to"]["kind"] == "all", "'all' is allowed with the explicit flag")

    try:
        A.send("session:SESSB", "DEFER", subject="later", body="not now")
        check(False, "DEFER without a trigger must be refused")
    except SystemExit:
        check(True, "DEFER without 'trigger:' is refused")

    ok = A.send("session:SESSB", "DEFER", subject="later",
                body="trigger: after the migration lands")
    check(ok["type"] == "DEFER", "DEFER with a trigger is accepted")

    try:
        A.send("session:SESSB", "NONSENSE", subject="x")
        check(False, "unknown type must be refused")
    except SystemExit:
        check(True, "unknown message type is refused")


def test_concurrency(channel: Path) -> None:
    print("\n[5] concurrent senders lose no messages")
    n = 24
    prog = (
        "import sys,os;sys.path.insert(0,%r);"
        "import agent_message as m;"
        "m.whoami=lambda:{'sessionId':os.environ['TERM_SESSION_ID'],'humanName':'h',"
        "'worktree':'w','branch':'b'};"
        "m.send('session:TARGET','FYI',subject=os.environ['MSG_SUBJ'])" % str(SCRIPTS)
    )
    env = dict(os.environ, AGENT_CHANNEL_DIR=str(channel))
    procs = []
    for i in range(n):
        e = dict(env, TERM_SESSION_ID=f"W{i:03d}", MSG_SUBJ=f"msg-{i:03d}")
        procs.append(subprocess.Popen([sys.executable, "-c", prog], env=e,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
    errs = [p.communicate()[1].decode()[:200] for p in procs]
    bad = [e for e in errs if e.strip()]
    check(not bad, f"all {n} senders exited clean" + (f" (errors: {bad[:2]})" if bad else ""))

    T = _fresh(channel, "TARGET")
    got = {m["subject"] for m in T.inbox(show_all=True)}
    expected = {f"msg-{i:03d}" for i in range(n)}
    missing = expected - got
    check(not missing, f"zero lost lines under {n}-way concurrency (missing: {sorted(missing)[:5]})")


def test_rotation(channel: Path) -> None:
    print("\n[6] rotation")
    A = _fresh(channel, "SESSA")
    target = A._mailbox_file("session:BIG")
    target.write_text("x" * (A.MAX_BYTES + 10))
    rotated = A.rotate()
    check(any("BIG" in r for r in rotated), f"oversized mailbox rotates (got {rotated})")
    check(target.exists() and target.stat().st_size == 0, "a fresh empty live file replaces it")
    check(target.with_suffix(target.suffix + ".1").exists(), "previous content preserved as .1")


def main() -> int:
    print("test_agent_message.py")
    for fn in (test_addressing_and_cursors, test_threading, test_ttl,
               test_guards, test_concurrency, test_rotation):
        with tempfile.TemporaryDirectory() as td:
            fn(Path(td) / "agent-coordination")
    print(f"\n{'FAILED: ' + str(len(FAILURES)) if FAILURES else 'ALL PASS'}")
    for f in FAILURES:
        print(f"  - {f}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
