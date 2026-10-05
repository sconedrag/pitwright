#!/usr/bin/env python3
"""
rotate_events_log.py — Coordination Harness v2, Component 6.

`.claude/coordination/events.log` is append-only and otherwise grows forever
(already ~128 KB at v2 authoring). This rotates it when it exceeds a size
threshold: the current log is archived to
`.claude/coordination/history/events-<UTC-date>.log` and a fresh log is started
with a `LOG_ROTATED` marker pointing at the archive.

Invoked opportunistically (best-effort, never fatal):
  - at the top of render_board.py (so the board read stays fast)
  - from .githooks/post-commit

Manual:
    python3 scripts/rotate_events_log.py [--threshold-bytes N] [--force]

Exit code is always 0 — rotation is housekeeping and must never block a caller.

Note on lastEventOffset: session manifests track a byte offset into events.log
for incremental reads (session-file-guard.sh). After rotation the live log is
smaller than a stale offset; consumers already guard with
`current_size > last_offset`, so a post-rotation offset simply yields no new
events until the log grows past it again — safe, no crash.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import coord_config  # noqa: E402

THRESHOLD_BYTES_DEFAULT = 256 * 1024  # 256 KB


def _coord_dir() -> Path:
    return coord_config.project_root() / ".claude" / "coordination"


def _utcnow_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat() + "Z"


def _date_stamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def rotate(threshold_bytes: int, force: bool) -> bool:
    """Rotate if over threshold (or forced). Returns True if rotated."""
    coord = _coord_dir()
    log = coord / "events.log"
    if not log.is_file():
        return False
    try:
        size = log.stat().st_size
    except OSError:
        return False
    if not force and size <= threshold_bytes:
        return False

    history = coord / "history"
    history.mkdir(parents=True, exist_ok=True)

    # Unique archive name even if rotated twice in one day.
    base = history / f"events-{_date_stamp()}.log"
    archive = base
    n = 1
    while archive.exists():
        archive = history / f"events-{_date_stamp()}.{n}.log"
        n += 1

    try:
        log.rename(archive)
    except OSError as exc:
        sys.stderr.write(f"[rotate-events] archive failed: {exc}\n")
        return False

    marker = f"{_utcnow_iso()}|LOG_ROTATED|system|coordination|Rotated {size} bytes to {archive.name}\n"
    try:
        log.write_text(marker)
    except OSError:
        pass
    print(f"[rotate-events] rotated {size} bytes -> history/{archive.name}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Rotate coordination events.log.")
    parser.add_argument("--threshold-bytes", type=int, default=THRESHOLD_BYTES_DEFAULT)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    rotate(args.threshold_bytes, args.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
