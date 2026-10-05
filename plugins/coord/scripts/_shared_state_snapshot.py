#!/usr/bin/env python3
"""_shared_state_snapshot.py — did anything just mutate the LIVE cross-worktree channel?

WHY THIS EXISTS
The tooling tests run against the same machine as ten live sessions, and the shared channel
(`<git-common-dir>/agent-coordination/`) is mutable state every one of them reads. A test that
writes there does not fail — it corrupts a peer.

That is not hypothetical. Adding a registry publish to `session_manifest.set_domain` made an
existing manifest test (which builds a temp repo but runs from the real one) write live-looking
session records into the REAL registry. One read ALIVE and claimed a real domain, which
would have refused every genuine stand-in for it with no way to diagnose it. Every test stayed
green the whole time, because from a test's point of view nothing was wrong.

WHY A CONTENT HASH AND NOT A KEY LIST
My first hand-rolled check compared the registry's KEY SET and reported clean. The offending
test kept overwriting the SAME key, so a key-set diff was structurally incapable of detecting
it — a filter narrow enough that it could not have returned anything but a pass. This hashes
the full content of every shared-state file, so an in-place overwrite is as visible as an
insertion.

Usage
-----
    python3 scripts/_shared_state_snapshot.py            # print a hash of the live channel
    python3 scripts/_shared_state_snapshot.py --verbose  # per-file hashes, to localise a diff

Exit code is always 0; the caller compares two invocations. Deliberately reads the LIVE channel
and ignores AGENT_CHANNEL_DIR/SESSION_REGISTRY_CHANNEL — a snapshot that followed an override
would happily certify a hermetic sandbox while the real channel was being trampled.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

# Only the files a stray write would land in. Locks and the event log churn legitimately during
# a run (another session may build, message, or claim while the suite runs), so including them
# would produce false alarms and the guard would be turned off within a week.
WATCHED = ("sessions-registry.json", "contracts")


def live_channel() -> Path | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    common = out.stdout.strip()
    return Path(common) / "agent-coordination" if common else None


def snapshot() -> list[tuple[str, str]]:
    ch = live_channel()
    if ch is None or not ch.is_dir():
        return []
    rows: list[tuple[str, str]] = []
    for name in WATCHED:
        target = ch / name
        if target.is_file():
            files = [target]
        elif target.is_dir():
            files = sorted(p for p in target.rglob("*") if p.is_file())
        else:
            continue
        for f in files:
            try:
                digest = _digest(f)
            except OSError:
                digest = "unreadable"
            rows.append((str(f.relative_to(ch)), digest))
    return sorted(rows)


# Fields whose value changes on their own while the suite runs, written by OTHER live sessions.
# See `_digest` for why this is not the slippery slope it looks like.
_LIVENESS_FIELDS = ("lastHeartbeat", "last_heartbeat")

# The registry's top-level bookkeeping. `updatedAt` is restamped by ANY write, so a peer's
# heartbeat moves it even though no session record changed. Missing this on the first attempt is
# recorded in `_digest` — it is the more instructive half of this fix.
_REGISTRY_CLOCK_FIELDS = ("updatedAt", "updated_at")


def _digest(path: Path) -> str:
    """Content hash, with peer LIVENESS churn normalized out of the session registry.

    MEASURED FALSE POSITIVE. This guard fired on essentially every run of
    `run_tooling_tests.sh`, and the mutation it reported was not the suite's. Diffing the
    registry across one run showed a single change: `lastHeartbeat` on a peer session in
    another worktree, heartbeating during the several minutes the suite takes. With several
    live sessions that is near-certain every time.

    AND THE FIRST FIX WAS INCOMPLETE, which is the more instructive half. Stripping the
    per-entry `lastHeartbeat` was not enough: the registry also carries a top-level `updatedAt`
    restamped by ANY write, so a peer's heartbeat still moved the digest. The unit tests passed
    anyway, because the fixture they wrote — `{"sessions": {...}}` — did not carry `updatedAt`
    at all. A control built from an imagined shape rather than the real file cannot come out the
    other way, so the tests now assert their fixture's top-level keys against the live registry's.

    It also explains why bisecting for the culprit named a different test on every attempt:
    there was no culprit. The attribution was to whichever test happened to be running when a
    peer's timer fired, which is why an earlier investigation concluded "something in the suite
    writes, but no single test owns it". Nothing in the suite writes. That conclusion was wrong.

    This is the same reasoning the module docstring already applies to locks and the event log
    ("another session may build, message, or claim while the suite runs") — the registry simply
    was not on that list, and one file short of complete is how a guard earns its reputation for
    crying wolf. Its own docstring predicts the consequence: it "would be turned off within a
    week."

    WHAT THIS GIVES UP, STATED PLAINLY. A test that writes ONLY a heartbeat — forging liveness
    for a session id without touching any other field — is now invisible here. That is a real
    hole and it is the narrowest available: every other stray write is still caught, because a
    test creating or claiming a session record changes the key set, the domain, the worktree or
    the branch, and those are hashed exactly as before. The alternative — excluding the registry
    outright — would reopen the bug this guard was built for (a manifest test writing live-looking
    records that claimed a real domain), so the file stays watched and only the clock-driven
    field is normalized.
    """
    raw = path.read_bytes()
    if path.name != "sessions-registry.json":
        return hashlib.sha256(raw).hexdigest()[:16]
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        # Unparseable is itself worth surfacing — hash it as-is rather than silently passing.
        return hashlib.sha256(raw).hexdigest()[:16]
    if isinstance(data, dict):
        for field in _REGISTRY_CLOCK_FIELDS:
            data.pop(field, None)
    sessions = data.get("sessions", data) if isinstance(data, dict) else data
    if isinstance(sessions, dict):
        for entry in sessions.values():
            if isinstance(entry, dict):
                for field in _LIVENESS_FIELDS:
                    entry.pop(field, None)
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    rows = snapshot()
    if a.verbose:
        for name, digest in rows:
            print(f"{digest}  {name}")
        return 0
    combined = hashlib.sha256(
        "\n".join(f"{n}:{d}" for n, d in rows).encode("utf-8")).hexdigest()[:16]
    print(combined)
    return 0


if __name__ == "__main__":
    sys.exit(main())
