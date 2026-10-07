#!/usr/bin/env python3
"""Guard: `/coord:adjacency --notify` must not write to a dead session's mailbox.

The cross-worktree collision detector found the peer whose branch overlaps yours and
messaged them with no check that the session still existed. A message to a dead session
is written to a real file in the durable mailbox and delivered to nobody, permanently,
with no error on either side — the sender sees a msgId and believes it landed.

That is the same failure that has made the peer channel unreliable before, and it is worse
here than it looks: the durable mailbox's whole justification is that a request survives
the recipient being busy (a peer parked in a 20-60 min clean-room build won't drain it for
a long time). A mailbox that also silently absorbs requests to recipients who no longer
exist cannot be told apart from one that is merely slow.

`test_dead_peer_is_not_messaged` fails against the pre-fix implementation.

Run: python3 scripts/tests/test_adjacency_liveness_gate.py
"""

from __future__ import annotations
import datetime
import json
import os
import sys
import tempfile
from pathlib import Path
os.environ.pop("CLAUDE_CODE_SESSION_ID", None)  # hermetic: tests pin identity themselves

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _testlib  # noqa: E402

import adjacency  # noqa: E402
import blocker_owner  # noqa: E402
import coord_locks  # noqa: E402

DEAD_PID = _testlib.dead_pid()
LIVE_PID = os.getpid()
MY_BRANCH = "feat/mine"
PEER_BRANCH = "feat/theirs"


def _iso(age):
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return (now - datetime.timedelta(seconds=age)).isoformat() + "Z"


class Harness:
    """Patches the module seams rather than the shared channel, so a failing test cannot
    leave real coordination state behind."""

    def __init__(self, pid, heartbeat_age):
        self.pid = pid
        self.hb = heartbeat_age
        self.tmp = tempfile.TemporaryDirectory()
        self.sent = []

    def __enter__(self):
        root = Path(self.tmp.name)
        (root / "sessions").mkdir()
        (root / "locks").mkdir()
        (root / "sessions" / "peer.json").write_text(json.dumps({
            "sessionId": "peer", "pid": self.pid, "host": coord_locks.HOST,
            "lastHeartbeat": _iso(self.hb),
        }))

        self._saved = {
            "sessions_dir": coord_locks.sessions_dir,
            "locks_dir": coord_locks.locks_dir,
            "session_id": coord_locks.session_id,
            "registry": blocker_owner._registry,
            # adjacency.session_for_branch reads its OWN _registry, not blocker_owner's.
            "adj_registry": adjacency._registry,
            "sent_state": adjacency._sent_state,
            "save_sent": adjacency._save_sent,
            "agent_message": adjacency.agent_message,
        }
        coord_locks.sessions_dir = lambda: root / "sessions"
        coord_locks.locks_dir = lambda: root / "locks"
        coord_locks.session_id = lambda: "me"
        registry = {"peer": {"branch": PEER_BRANCH, "humanName": "Peer session"}}
        blocker_owner._registry = lambda: registry
        adjacency._registry = lambda: registry
        adjacency._sent_state = lambda: {}
        adjacency._save_sent = lambda s: None

        harness = self

        class FakeMessenger:
            @staticmethod
            def send(to, kind, **kw):
                harness.sent.append(to)
                return {"msgId": "fake-1"}

            @staticmethod
            def doorbell_for(msg):
                return None

        adjacency.agent_message = FakeMessenger
        return self

    def __exit__(self, *exc):
        coord_locks.sessions_dir = self._saved["sessions_dir"]
        coord_locks.locks_dir = self._saved["locks_dir"]
        coord_locks.session_id = self._saved["session_id"]
        blocker_owner._registry = self._saved["registry"]
        adjacency._registry = self._saved["adj_registry"]
        adjacency._sent_state = self._saved["sent_state"]
        adjacency._save_sent = self._saved["save_sent"]
        adjacency.agent_message = self._saved["agent_message"]
        self.tmp.cleanup()
        return False


ROWS = [{"a": MY_BRANCH, "b": PEER_BRANCH, "files": ["shared.py", "other.py"]}]


def test_dead_peer_is_not_messaged():
    """THE regression. Fails against the pre-fix notify()."""
    with Harness(DEAD_PID, 48 * 3600) as h:
        sent, orphaned = adjacency.notify(MY_BRANCH, ROWS)
        assert h.sent == [], f"wrote to a dead session's mailbox: {h.sent}"
        assert sent == [], sent
        assert len(orphaned) == 1, orphaned


def test_dead_peer_collision_is_reported_not_silently_dropped():
    """Not messaging is only half right — the collision is still real and must surface,
    with the evidence that justified skipping the ping."""
    with Harness(DEAD_PID, 48 * 3600):
        _, orphaned = adjacency.notify(MY_BRANCH, ROWS)
        o = orphaned[0]
        assert o["humanName"] == "Peer session", o
        assert o["files"] == ["shared.py", "other.py"], o
        assert o["evidence"], o
        assert "heartbeat" in o["livenessReason"], o


def test_dead_peer_branch_is_never_auto_inherited():
    """Their branch is git state — irreversible. Certainty of death does not grant it."""
    with Harness(DEAD_PID, 48 * 3600):
        _, orphaned = adjacency.notify(MY_BRANCH, ROWS)
        assert orphaned[0]["recommendedAction"] == blocker_owner.ACT_PROPOSE, orphaned[0]


def test_live_peer_is_still_messaged():
    """The gate must not suppress the case the feature exists for."""
    with Harness(LIVE_PID, 10) as h:
        sent, orphaned = adjacency.notify(MY_BRANCH, ROWS)
        assert h.sent == ["session:peer"], h.sent
        assert len(sent) == 1, sent
        assert orphaned == [], orphaned


def test_uncertain_peer_is_messaged_not_orphaned():
    """Dead pid but a fresh heartbeat is unknown, not dead. Uncertainty resolves toward
    the peer still being there, so we ping rather than treat their work as abandoned."""
    with Harness(DEAD_PID, 5) as h:
        sent, orphaned = adjacency.notify(MY_BRANCH, ROWS)
        assert h.sent == ["session:peer"], h.sent
        assert orphaned == [], orphaned


def test_every_in_repo_notify_caller_unpacks_two_values():
    """The regression that actually reached CI, pinned at the call site.

    `notify()` gained a second return value when the liveness gate landed. A caller
    that predated the change still did `sent = adj.notify(...)` and then compared it to
    a list — which a tuple never equals. It failed only in the suite belonging to the
    module whose signature I had changed, and that was the one suite I did not run.

    Asserting the return arity here would not have caught it; the break was at the
    CALL SITE, so that is where the guard belongs. Scanning is the point: it covers
    callers that do not exist yet, which is exactly the population a human grep misses
    on the next signature change.
    """
    import re
    root = Path(__file__).resolve().parents[1]
    offenders = []
    pattern = re.compile(r"^\s*(?P<lhs>[\w., ]+?)\s*=\s*[\w.]*\bnotify\s*\(")
    for path in sorted(root.rglob("*.py")):
        if path.name == Path(__file__).name:
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if line.lstrip().startswith("#") or "def notify" in line:
                continue
            m = pattern.match(line)
            if m and "," not in m.group("lhs"):
                offenders.append(f"{path.relative_to(root)}:{n}: {line.strip()}")
    assert not offenders, (
        "notify() returns (sent, orphaned); these call sites bind a single name and "
        "will silently compare a tuple:\n  " + "\n  ".join(offenders)
    )


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
