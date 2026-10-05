#!/usr/bin/env python3
"""
adjacency_cache.py — make "another branch is editing this file" visible AT THE MOMENT OF
EDIT, without paying for the check every time.

The problem
-----------
File locks do not span worktrees: two sessions in different worktrees edit different
physical copies and never block each other, so the collision surfaces at MERGE. `/coord:adjacency`
exists to warn about that in advance, and it works — but it costs ~20s (measured in
practice), which is exactly why nobody runs it. A check that is too expensive to run is
functionally the same as a check nobody wired.

That cost also rules out calling it inline from the `PreToolUse(Edit|Write)` guard, which
fires on every single edit.

The split
---------
Pay the 20s ONCE, in the background, at session start; read the answer in microseconds at
edit time. This is the pattern the SessionStart board already uses for `ci_health`,
`perf_snapshot` and `freshness_audit` — cached ledger written out of band, surface reads it.

Staleness is STATED, never implied. The cache records when it was computed and the HEAD it
was computed against, and the warning prints both. A cached answer presented as live is
worse than no answer, because it invites trust it has not earned — the peer may have
committed since.

The failure this prevents: a session duplicates a change another worktree's branch is
already making. `/coord:adjacency` would have said so, but only if someone ran it — so the
answer is precomputed and surfaced where the edit happens.

Usage
-----
    python3 scripts/adjacency_cache.py refresh        # ~20s; run detached
    python3 scripts/adjacency_cache.py check <path>   # instant; prints a warning or nothing
    python3 scripts/adjacency_cache.py status

Invoked by: scripts/session_start.sh (background refresh), scripts/lock_guard.py
(instant check at edit time)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import coord_config  # noqa: E402

ROOT = coord_config.project_root()
CACHE = ROOT / ".claude" / "state" / "adjacency-cache.json"

# Past this the cache is reported as stale rather than used silently. Long enough that a
# normal session never recomputes, short enough that a day-old answer is not passed off as
# current.
MAX_AGE_SECONDS = coord_config.get("representation_stale_seconds")

REFRESH_TIMEOUT = 180


def _git(*args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                             text=True, timeout=15)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def refresh() -> dict:
    """Run the real adjacency check and fold it into a path -> peers index."""
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    head = _git("rev-parse", "--short=9", "HEAD")
    payload = {
        "computedAt": time.time(),
        "branch": branch,
        "head": head,
        "collisions": {},
        "ok": False,
    }
    try:
        out = subprocess.run(
            ["python3", str(SCRIPT_DIR / "adjacency.py"), "--json"],
            cwd=ROOT, capture_output=True, text=True, timeout=REFRESH_TIMEOUT)
        data = json.loads(out.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        # Record the failure rather than leaving a stale cache looking fresh. `ok: False`
        # makes `check` say "unknown" instead of the far worse "no collisions".
        _write(payload)
        return payload

    index: dict[str, list[dict]] = {}
    for row in data.get("mine", []) or []:
        peer = row.get("b") if row.get("a") == branch else row.get("a")
        for path in (row.get("files") or []) + (row.get("special") or []):
            entry = {"branch": peer, "special": path in (row.get("special") or [])}
            index.setdefault(path, [])
            if entry not in index[path]:
                index[path].append(entry)

    payload["collisions"] = index
    payload["ok"] = True
    _write(payload)
    return payload


def _write(payload: dict) -> None:
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, CACHE)  # atomic: the guard may read mid-write
    except OSError:
        pass


def read() -> dict | None:
    try:
        with open(CACHE) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _rel(path: str) -> str:
    """Adjacency reports repo-relative paths; the guard is handed absolute ones."""
    try:
        return str(Path(path).resolve().relative_to(ROOT.resolve()))
    except (ValueError, OSError):
        return path.lstrip("./")


def check(path: str) -> str:
    """Return a warning for `path`, or '' when there is nothing to say."""
    cache = read()
    if cache is None or not cache.get("ok"):
        return ""
    peers = cache.get("collisions", {}).get(_rel(path))
    if not peers:
        return ""

    age_min = int((time.time() - float(cache.get("computedAt", 0))) / 60)
    stale = (time.time() - float(cache.get("computedAt", 0))) > MAX_AGE_SECONDS
    branches = ", ".join(sorted({p["branch"] for p in peers if p.get("branch")}))
    special = any(p.get("special") for p in peers)

    lines = [
        "",
        f"[adjacency] {_rel(path)} is ALSO being changed on: {branches}",
        "  Locks do not span worktrees, so nothing will stop you here — this collides at MERGE.",
    ]
    if special:
        lines.append("  This is a high-contention shared file; expect a conflict, not a clean merge.")
    lines.append(
        f"  Computed {age_min} min ago against {cache.get('head') or '?'}"
        + (" — STALE, they may have moved since; re-run "
           "`python3 scripts/adjacency_cache.py refresh`." if stale else ".")
    )
    lines.append("  Coordinate before duplicating work: /coord:adjacency --notify, or agree the "
                 "shared interface with the other branch's owner first.")
    lines.append("")
    return "\n".join(lines)


def status() -> str:
    cache = read()
    if cache is None:
        return "adjacency cache: absent (run: python3 scripts/adjacency_cache.py refresh)"
    if not cache.get("ok"):
        return "adjacency cache: last refresh FAILED — collisions unknown, not 'none'"
    age_min = int((time.time() - float(cache.get("computedAt", 0))) / 60)
    n_files = len(cache.get("collisions", {}))
    peers = {p["branch"] for v in cache.get("collisions", {}).values() for p in v}
    return (f"adjacency cache: {n_files} contended file(s) across {len(peers)} branch(es), "
            f"computed {age_min} min ago against {cache.get('head') or '?'}")


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] == "status":
        print(status())
        return 0
    if args[0] == "refresh":
        result = refresh()
        print(f"adjacency cache: {'ok' if result['ok'] else 'FAILED'}, "
              f"{len(result['collisions'])} contended file(s)")
        return 0
    if args[0] == "check" and len(args) > 1:
        text = check(args[1])
        if text:
            print(text)
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
