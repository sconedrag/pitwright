#!/usr/bin/env python3
"""
test_mailbox_identity_keys.py — a session must read mail addressed to EITHER of its
identifiers.

A session has two identities and peers reasonably use either:

  TERM_SESSION_ID  the terminal's id — what the coordination layer keyed on historically
  native UUID      what the HARNESS advertises: `ListAgents`, the transcript path,
                   `.claude/coordination/current-session.json`

A peer looking at `ListAgents` sees the native UUID and addresses that. If the mailbox
listens only on TERM_SESSION_ID, such a message is a DEAD LETTER — written to a file,
delivered to nobody, forever, with no error on either side.

An `interface-proposal` addressed to the native UUID (`session:44c6bba1-…`) while this
layer read only the TERM_SESSION_ID form (`session:222DB9FA-…`) once surfaced solely
because the native SendMessage doorbell reached a human who relayed it — the durable
mailbox, whose whole purpose is to survive exactly that relay not happening, silently
swallowed it.

That is one instance of a recurring root cause: the same split has made `/coord:start-session`
manifests unreadable by the edit guard ("unregistered = no locking" for a session that had in
fact registered) and put a lock in a namespace no reader looked in. Two identity systems that
do not agree is a reliable bug generator, which is why this is a test and not a comment.

Run: python3 scripts/tests/test_mailbox_identity_keys.py   (exit 0 = pass)
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import agent_message as am  # noqa: E402
import coord_locks  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


class _Marker:
    """Point `current-session.json` at a chosen native id for the duration of a test."""

    def __init__(self, native: str | None):
        self.native = native
        self.tmp = tempfile.TemporaryDirectory()
        self.orig = coord_locks.coord_dir

    def __enter__(self):
        d = Path(self.tmp.name)
        if self.native is not None:
            (d / "current-session.json").write_text(
                json.dumps({"sessionId": self.native, "pid": 1}))
        coord_locks.coord_dir = lambda: d
        return self

    def __exit__(self, *a):
        coord_locks.coord_dir = self.orig
        self.tmp.cleanup()


def test_native_uuid_key_is_read() -> None:
    with _Marker("44c6bba1-58da-4b21-af41-6a665725560e"):
        keys = am.my_keys("TERM-ABC")
        check("session:44c6bba1-58da-4b21-af41-6a665725560e" in keys,
              "mail addressed to the NATIVE uuid is readable")
        check("session:TERM-ABC" in keys,
              "mail addressed to TERM_SESSION_ID is still readable")


def test_absent_marker_degrades_quietly() -> None:
    """An unregistered session has no native key; the TERM key must still work."""
    with _Marker(None):
        keys = am.my_keys("TERM-ABC")
        check("session:TERM-ABC" in keys, "no marker → TERM key still present")
        check(not any(k.startswith("session:") and k != "session:TERM-ABC" for k in keys),
              "no marker → no phantom native key invented")


def test_identical_ids_are_not_duplicated() -> None:
    with _Marker("SAME"):
        keys = am.my_keys("SAME")
        check(keys.count("session:SAME") == 1,
              "when both identifiers match, the key is not listed twice")


def test_broadcast_and_worktree_keys_survive() -> None:
    """The fix must not displace the other delivery channels."""
    with _Marker("NATIVE-1"):
        keys = am.my_keys("TERM-1")
        check("all" in keys, "the broadcast key is still read")
        check(any(k.startswith("worktree:") for k in keys),
              "the worktree key is still read")


def test_marker_is_sanitised() -> None:
    """A malformed marker must not inject a path separator into a mailbox key."""
    with _Marker("../../etc/passwd"):
        keys = am.my_keys("TERM-1")
        bad = [k for k in keys if "/" in k or ".." in k]
        check(not bad, f"a hostile marker cannot produce a traversal key (got {bad})")


def main() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    print(f"test_mailbox_identity_keys.py — {len(tests)} tests")
    for _, fn in tests:
        fn()
    print(f"\n{'FAILED: ' + ', '.join(FAILURES) if FAILURES else 'ALL PASS'}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
