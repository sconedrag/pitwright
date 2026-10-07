#!/usr/bin/env python3
"""
test_mailbox_identity_keys.py — a session reads mail addressed to every key PROVEN to be
its own, and never a key that might be a peer's.

Why the first half exists. A peer looking at `ListAgents` sees the native UUID and
addresses that. If the mailbox listens on a different key, such a message is a DEAD LETTER
— written to a file, delivered to nobody, with no error on either side. An
`interface-proposal` sent to `session:44c6bba1-…` (native) while this layer read only
`session:222DB9FA-…` (TERM_SESSION_ID) once surfaced solely because a human relayed it.
Since v0.2 the native UUID is the key itself (`_identity`); when coord runs WITHOUT it
(fallback to TERM_SESSION_ID), the native id recorded on our own manifest is still read.

Why the second half exists. A second key is read only with proof, because an unproven one
can belong to a live peer: `TERM_SESSION_ID` is shared by every tmux pane in a tab, and the
`current-session.json` self-marker is per-worktree, last-writer-wins. Reading a peer's key
means consuming its mail (found in adversarial review of v0.2).

Run: python3 scripts/tests/test_mailbox_identity_keys.py   (exit 0 = pass)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
os.environ.pop("CLAUDE_CODE_SESSION_ID", None)  # hermetic: tests pin identity themselves

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import agent_message as am  # noqa: E402
import coord_locks  # noqa: E402

FAILURES: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {label}")
    if not cond:
        FAILURES.append(label)


class _Session:
    """A scratch git repo holding this session's manifest (and optionally a self-marker),
    with identity pinned through the environment and cwd inside the repo."""

    def __init__(self, *, term: str = "", native: str = "", manifest: dict | None = None,
                 marker: dict | None = None):
        self.term, self.native = term, native
        self.manifest, self.marker = manifest, marker
        self.tmp = tempfile.TemporaryDirectory()
        self.saved_env = {k: os.environ.get(k)
                          for k in ("TERM_SESSION_ID", "CLAUDE_CODE_SESSION_ID")}
        self.saved_cwd = os.getcwd()
        self.saved_coord_dir = coord_locks.coord_dir

    def __enter__(self):
        repo = Path(self.tmp.name).resolve()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        coord = repo / ".claude" / "coordination"
        (coord / "sessions").mkdir(parents=True)
        me = self.native or self.term
        if self.manifest is not None:
            (coord / "sessions" / f"{me}.json").write_text(
                json.dumps(dict({"sessionId": me}, **self.manifest)))
        if self.marker is not None:
            (coord / "current-session.json").write_text(json.dumps(self.marker))
        for k in self.saved_env:
            os.environ.pop(k, None)
        if self.term:
            os.environ["TERM_SESSION_ID"] = self.term
        if self.native:
            os.environ["CLAUDE_CODE_SESSION_ID"] = self.native
        os.chdir(repo)
        coord_locks.coord_dir = lambda: coord
        return self

    def __exit__(self, *a):
        coord_locks.coord_dir = self.saved_coord_dir
        os.chdir(self.saved_cwd)
        for k, v in self.saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()


NATIVE = "44c6bba1-58da-4b21-af41-6a665725560e"


def _session_keys(keys: list[str]) -> list[str]:
    return sorted(k for k in keys if k.startswith("session:"))


def test_native_is_the_key_under_v02() -> None:
    with _Session(term="TERM-ABC", native=NATIVE, manifest={"pid": 1}):
        keys = am.my_keys(am.session_id())
        check(_session_keys(keys) == [f"session:{NATIVE}"],
              f"v0.2: the native UUID is the key, the shared TERM id is NOT read ({keys})")


def test_fallback_mode_still_reads_mail_to_the_native_uuid() -> None:
    """The original dead-letter fix, without CLAUDE_CODE_SESSION_ID (older Claude Code)."""
    with _Session(term="TERM-ABC", manifest={"nativeSessionId": NATIVE, "pid": 1}):
        keys = am.my_keys(am.session_id())
        check(f"session:{NATIVE}" in keys,
              "fallback: mail addressed to the native UUID on our manifest is readable")
        check("session:TERM-ABC" in keys, "fallback: the TERM key is still read")


def test_unverified_marker_is_not_read() -> None:
    """A marker written by ANOTHER session in this checkout (pid mismatch) is a peer's id."""
    peer = "99999999-0000-4000-8000-000000000000"
    with _Session(term="TERM-ABC", manifest={"pid": 1234},
                  marker={"sessionId": peer, "pid": 5678}):
        keys = am.my_keys(am.session_id())
        check(f"session:{peer}" not in keys,
              "a last-writer-wins marker naming a different pid is NOT read")
    # Positive control: the same marker WITH a matching pid is read, so the negative above
    # proves the pid check rather than a marker path that never runs.
    with _Session(term="TERM-ABC", manifest={"pid": 1234},
                  marker={"sessionId": peer, "pid": 1234}):
        keys = am.my_keys(am.session_id())
        check(f"session:{peer}" in keys, "control: a marker corroborated by pid IS read")


def test_keys_for_another_session_get_none_of_ours() -> None:
    with _Session(term="TERM-ABC", manifest={"nativeSessionId": NATIVE, "pid": 1}):
        keys = am.my_keys("someone-else")
        check(_session_keys(keys) == ["session:someone-else"],
              f"keys computed for another session carry none of ours ({keys})")


def test_no_manifest_degrades_quietly() -> None:
    with _Session(term="TERM-ABC"):
        keys = am.my_keys(am.session_id())
        check(_session_keys(keys) == ["session:TERM-ABC"],
              "no manifest: only the session's own key, no phantom keys")


def test_broadcast_and_worktree_keys_survive() -> None:
    with _Session(native=NATIVE, manifest={"pid": 1}):
        keys = am.my_keys(am.session_id())
        check("all" in keys, "the broadcast key is still read")
        check(any(k.startswith("worktree:") for k in keys), "the worktree key is still read")


def test_marker_is_sanitised() -> None:
    """A malformed marker must not inject a path separator into a mailbox key."""
    with _Session(term="TERM-ABC", manifest={"pid": 1234},
                  marker={"sessionId": "../../etc/passwd", "pid": 1234}):
        keys = am.my_keys(am.session_id())
        bad = [k for k in keys if "/" in k or ".." in k]
        check(not bad, f"a hostile marker cannot produce a traversal key (got {bad})")
        check("session:etcpasswd" in keys, "control: the sanitised marker id IS read")


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
