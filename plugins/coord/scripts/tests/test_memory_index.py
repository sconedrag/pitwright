#!/usr/bin/env python3
"""
test_memory_index.py — MEMORY.md must not lose entries under concurrent writers.

The native memory directory is ONE physical location shared by the main checkout and every
worktree, sitting outside the git tree where no coordination mechanism sees it. With ~10
live sessions, a full-file rewrite by two at once silently drops one session's index lines.

  [1] CONCURRENCY — N parallel `add` calls all land (the property that was broken).
  [2] CONTROL     — the same N without the lock DO lose entries, so the test is proving
                    the lock works rather than proving the race is hard to hit.
  [3] IDEMPOTENCE — re-indexing a file updates its line in place, never duplicates.
  [4] SECTIONS    — entries land under the requested heading; a new heading is created.
  [5] INTEGRITY   — the index is never observed half-written (atomic replace).

Runs entirely in temp dirs (CLAUDE_MEMORY_DIR + AGENT_CHANNEL_DIR). Never touches the real
memory directory.

Run: python3 scripts/tests/test_memory_index.py   (exit 0 = pass)
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
os.environ.pop("CLAUDE_CODE_SESSION_ID", None)  # hermetic: tests pin identity themselves

SCRIPTS = Path(__file__).resolve().parent.parent
FAILURES: list[str] = []
N = 16


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        FAILURES.append(label)


def _env(mem: Path, chan: Path) -> dict:
    # CLAUDE_PROJECT_DIR isolates the coordination lock dir too (coord_config.project_root()
    # resolves there instead of the real checkout): `mem` and `chan` are both direct children
    # of the same scratch tempdir, so its parent is a safe, non-git, per-run project root.
    # Without this the "memory-index" lock lands under the REAL repo's
    # .claude/coordination/locks, which is neither isolated nor guaranteed free of
    # contention from other live coordination activity.
    return dict(os.environ, CLAUDE_MEMORY_DIR=str(mem), AGENT_CHANNEL_DIR=str(chan),
                CLAUDE_PROJECT_DIR=str(mem.parent),
                TERM_SESSION_ID=os.environ.get("TERM_SESSION_ID", "TESTSESS"))


def _spawn_add(env: dict, i: int) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, str(SCRIPTS / "memory_index.py"), "add", f"topic_{i:03d}.md",
         "--title", f"Topic {i:03d}", "--hook", f"hook number {i}"],
        env=dict(env, TERM_SESSION_ID=f"S{i:03d}"),
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _seed(mem: Path, i: int) -> None:
    """A memory FILE must exist before it can be indexed.

    MEMORY.md is generated from the files, so a pointer to a file that does not exist is a
    dead link that the next regen would drop anyway. `add` now rejects it outright rather
    than writing a line destined to vanish.
    """
    mem.mkdir(parents=True, exist_ok=True)
    (mem / f"topic_{i:03d}.md").write_text(
        f"---\nname: topic_{i:03d}\ndescription: seeded\nmetadata:\n"
        f"  type: feedback\n---\n\nbody\n", encoding="utf-8")


def test_concurrency(mem: Path, chan: Path) -> None:
    print(f"\n[1] {N} concurrent writers")
    env = _env(mem, chan)
    for i in range(N):
        _seed(mem, i)
    procs = [_spawn_add(env, i) for i in range(N)]
    errs = [p.communicate()[1].decode() for p in procs]
    # "[coord-lock] … Waiting…" is the lock doing its job under contention, not a failure.
    bad = [e[:160] for e in errs
           if any(ln.strip() and "[coord-lock]" not in ln for ln in e.splitlines())]
    check(not bad, f"all {N} writers exited clean" + (f" ({bad[:1]})" if bad else ""))
    check(any("[coord-lock]" in e for e in errs),
          "at least one writer actually contended for the lock (the race was exercised)")

    text = (mem / "MEMORY.md").read_text()
    missing = [i for i in range(N) if f"topic_{i:03d}.md" not in text]
    check(not missing, f"zero lost entries (missing: {missing[:6]})")

    lines = [ln for ln in text.splitlines() if ln.startswith("- [Topic ")]
    check(len(lines) == N, f"exactly {N} entry lines, no duplicates (got {len(lines)})")


def test_control_without_lock(mem: Path) -> None:
    print("\n[2] control — the same writers WITHOUT the lock lose entries")
    idx = mem / "CONTROL.md"
    idx.write_text("# Control\n\n## Active Features\n")
    prog = (
        "import os,sys,time\n"
        "p=os.environ['IDX']\n"
        "t=open(p).read()\n"                 # read
        "time.sleep(0.05)\n"                 # the window a real rewrite leaves open
        "t+='- [Topic %s](topic_%s.md)\\n'%(os.environ['I'],os.environ['I'])\n"
        "open(p,'w').write(t)\n"             # blind full-file rewrite
    )
    procs = [subprocess.Popen([sys.executable, "-c", prog],
                              env=dict(os.environ, IDX=str(idx), I=f"{i:03d}"),
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
             for i in range(N)]
    for p in procs:
        p.wait()
    got = len(re.findall(r"^- \[Topic ", idx.read_text(), re.M))
    check(got < N,
          f"unlocked rewrite loses entries as expected ({got}/{N} survived) — "
          f"confirms [1] is testing a real hazard")


def _seed_named(mem: Path, name: str) -> None:
    (mem / name).write_text(
        f"---\nname: {name[:-3]}\ndescription: seeded\nmetadata:\n"
        f"  type: feedback\n---\n\nbody\n", encoding="utf-8")


def test_idempotence_and_sections(mem: Path, chan: Path) -> None:
    print("\n[3][4] idempotent re-index and section placement")
    env = _env(mem, chan)
    run = lambda *a: subprocess.run(  # noqa: E731
        [sys.executable, str(SCRIPTS / "memory_index.py"), *a],
        env=env, capture_output=True, text=True)

    _seed_named(mem, "dup.md")
    run("add", "dup.md", "--title", "First", "--hook", "one")
    run("add", "dup.md", "--title", "Second", "--hook", "two")
    text = (mem / "MEMORY.md").read_text()
    check(text.count("](dup.md)") == 1, "re-indexing updates in place, no duplicate line")
    check("Second" in text and "First" not in text, "the updated title replaces the old one")

    # The index is generated now, so its sections are derived (Pinned / Recent / Archive).
    # A caller-supplied non-default section expresses "this is durable reference", which
    # the generated model spells `pin: true` — the same mapping the migration used for the
    # old Conventions/Process entries.
    _seed_named(mem, "proc.md")
    run("add", "proc.md", "--title", "Proc", "--hook", "h", "--section", "Process")
    text = (mem / "MEMORY.md").read_text()
    check("## Pinned" in text, "the generated index has a Pinned section")
    pinned_block = text.split("## Pinned", 1)[1].split("## Recent", 1)[0]
    check("](proc.md)" in pinned_block, "a non-default section pins the entry")
    check("pin: true" in (mem / "proc.md").read_text(), "the pin is stored in the FILE")


def test_integrity(mem: Path) -> None:
    print("\n[5] index integrity")
    text = (mem / "MEMORY.md").read_text()
    check(text.startswith("#"), "index still begins with its heading")
    check(text.endswith("\n") and "\n\n\n" not in text, "trailing newline, no runaway blanks")
    check(not list(mem.glob("MEMORY.md.tmp-*")), "no temp files left behind")


def main() -> int:
    print("test_memory_index.py")
    with tempfile.TemporaryDirectory() as td:
        mem, chan = Path(td) / "memory", Path(td) / "agent-coordination"
        mem.mkdir(parents=True)
        test_concurrency(mem, chan)
        test_control_without_lock(mem)
        test_idempotence_and_sections(mem, chan)
        test_integrity(mem)
    print(f"\n{'FAILED: ' + str(len(FAILURES)) if FAILURES else 'ALL PASS'}")
    for f in FAILURES:
        print(f"  - {f}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
