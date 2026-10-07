#!/usr/bin/env python3
"""Tests for the generated, budgeted MEMORY.md (`memory_index.py regen`).

Run as a PLAIN SCRIPT — the CI "Tooling tests" job has no pytest, and a file that only
defines `test_*` functions exits 0 having asserted nothing. Everything runs from main().
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
os.environ.pop("CLAUDE_CODE_SESSION_ID", None)  # hermetic: tests pin identity themselves

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FAILS: list[str] = []


def ok(label: str, cond: bool, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}"
          + (f": {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(label)


def _mem(d: Path, name: str, desc: str, hook: str = "", pin: bool = False,
         modified: str = "") -> None:
    meta = ["metadata:", "  node_type: memory", "  type: feedback"]
    if hook:
        meta.append(f"  hook: {hook}")
    if pin:
        meta.append("  pin: true")
    if modified:
        meta.append(f"  modified: {modified}")
    (d / name).write_text(
        "---\n" + f"name: {name[:-3]}\n" + f"description: {desc}\n"
        + "\n".join(meta) + "\n---\n\nbody\n", encoding="utf-8")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        os.environ["CLAUDE_MEMORY_DIR"] = str(d)
        for mod in list(sys.modules):
            if mod == "memory_index":
                del sys.modules[mod]
        import memory_index as mi

        # --- fixtures: 3 pinned, 40 unpinned across distinct dates
        _mem(d, "pinned_a_2026_01_01.md", "desc A", hook="PIN A", pin=True)
        _mem(d, "pinned_b_2026_01_02.md", "desc B", hook="PIN B", pin=True)
        _mem(d, "pinned_c_2026_01_03.md", "desc C", hook="PIN C", pin=True)
        for i in range(40):
            _mem(d, f"topic_{i:02d}_2026_08_{(i % 28) + 1:02d}.md",
                 f"description number {i} " + "x" * 120)

        print("test_budget_is_enforced_and_nothing_is_lost")
        r = mi.regen_global(budget=4000)
        size = len(mi.index_path().read_bytes())
        ok("MEMORY.md respects the budget", size <= 4000)
        ok("some entries were archived", r["archived"] > 0)
        ok("all 43 files are accounted for",
           r["pinned"] + r["hot"] + r["archived"] == 43)

        hot_text = mi.index_path().read_text(encoding="utf-8")
        arch_text = mi.archive_index_path().read_text(encoding="utf-8")
        every = all((f.name in hot_text) or (f.name in arch_text)
                    for f in d.glob("*.md") if not f.name.startswith("MEMORY"))
        ok("every memory file appears in exactly one index", every)

        # The budget must hold at EVERY size, not just one. The first implementation
        # reserved a fixed byte budget for the scaffold, which happened to fit at one
        # tested size and overshot the budget on a larger, more realistic corpus -- the
        # function reported a budget it had just exceeded. A single-budget assertion
        # passed straight through that.
        print("test_budget_holds_at_every_size")
        for b in (1500, 2500, 4000, 6000, 9000, 12000):
            mi.regen_global(budget=b)
            got = len(mi.index_path().read_bytes())
            ok(f"budget {b}: wrote {got}B", got <= b)

        print("test_pinned_never_rotate_out")
        r = mi.regen_global(budget=4000)
        ok("all 3 pinned survive a tiny budget", r["pinned"] == 3)
        ok("pinned hooks are present", "PIN A" in hot_text and "PIN C" in hot_text)
        ok("pinned are NOT in the archive", "pinned_a_2026_01_01.md" not in arch_text)

        print("test_recency_wins_for_the_unpinned")
        # 2026-08-28 is the newest fixture date; 2026-08-01 the oldest.
        ok("newest unpinned is hot", "topic_27_2026_08_28.md" in hot_text)
        ok("oldest unpinned is archived", "topic_00_2026_08_01.md" in arch_text)

        print("test_hook_beats_description")
        _mem(d, "both_2026_09_01.md", "THE-DESCRIPTION", hook="THE-HOOK")
        mi.regen_global(budget=99000)
        t = mi.index_path().read_text(encoding="utf-8")
        ok("curated hook is used", "THE-HOOK" in t)
        ok("description is not used when a hook exists", "THE-DESCRIPTION" not in t)
        _mem(d, "desconly_2026_09_02.md", "ONLY-DESCRIPTION")
        mi.regen_global(budget=99000)
        t = mi.index_path().read_text(encoding="utf-8")
        ok("description is the fallback", "ONLY-DESCRIPTION" in t)

        print("test_regen_is_deterministic_and_idempotent")
        a = mi.index_path().read_bytes()
        mi.regen_global(budget=99000)
        b = mi.index_path().read_bytes()
        ok("two regens produce identical bytes", a == b)

        print("test_the_subset_invariant_can_actually_fail")
        # Mutation: make _hook_for blow up the reachability set by dropping a file from
        # selection. If the guard is real, regen must REFUSE rather than write.
        real = mi._memory_files
        victim = sorted(p.name for p in d.glob("*.md")
                        if not p.name.startswith("MEMORY"))[0]

        class _Dropper:
            def __init__(self, fn):
                self.fn = fn
                self.n = 0

            def __call__(self):
                files = self.fn()
                self.n += 1
                # Second call (inside the invariant check) sees the full set; the first
                # (selection) sees one fewer -- exactly the silent-drop shape.
                return files if self.n > 1 else [p for p in files if p.name != victim]

        mi._memory_files = _Dropper(real)
        refused = False
        try:
            mi.regen_global(budget=99000)
        except SystemExit:
            refused = True
        finally:
            mi._memory_files = real
        ok("a dropped file makes regen REFUSE to write", refused)

        print("test_migrate_is_hook_preserving_and_idempotent")
        mi.index_path().write_text(
            "# Project Memory\n\n## Active Features\n"
            "- [t](topic_00_2026_08_01.md) — CURATED HOOK TEXT\n"
            "\n## Conventions\n"
            "- [c](topic_01_2026_08_02.md) — a durable convention\n", encoding="utf-8")
        m1 = mi.migrate_hooks_from_index()
        ok("curated hooks written into files", m1["hooks"] == 2)
        ok("non-default section became a pin", m1["pins"] == 1)
        ok("the hook landed in the file",
           "CURATED HOOK TEXT" in (d / "topic_00_2026_08_01.md").read_text())
        ok("the Conventions entry is pinned",
           mi._is_pinned(d / "topic_01_2026_08_02.md"))
        ok("the Active-Features entry is NOT pinned",
           not mi._is_pinned(d / "topic_00_2026_08_01.md"))
        m2 = mi.migrate_hooks_from_index()
        ok("re-running writes nothing (idempotent)",
           m2["hooks"] == 0 and m2["pins"] == 0)

        print("test_a_hook_containing_backslashes_survives")
        tricky = r"regex \b(alt1|alt2)\b and a \g<1> group"
        mi.stamp_meta("topic_02_2026_08_03.md", "hook", tricky)
        got = mi._frontmatter_meta(d / "topic_02_2026_08_03.md", "hook")
        ok("backslashes are stored literally, not as a regex template", got == tricky)

        print("test_best_effort_skips_instead_of_stalling_on_a_peers_lock")
        import subprocess, time
        import _agent_channel, _coord_lock
        chan = Path(td) / "chan"
        os.environ["AGENT_CHANNEL_DIR"] = str(chan)
        locks = _agent_channel.channel_dir() / "locks"
        locks.mkdir(parents=True, exist_ok=True)
        held = _coord_lock.acquire("memory-index", timeout=5, locks_dir=locks)
        ok("the contention scenario is actually set up", held)
        try:
            # A DIFFERENT session id. The coord lock is keyed by session, so a subprocess
            # inheriting ours would see the lock as its OWN and acquire it — the contention
            # would never occur and this test would pass having proved nothing.
            env = dict(os.environ, CLAUDE_MEMORY_DIR=str(d),
                       AGENT_CHANNEL_DIR=str(chan),
                       TERM_SESSION_ID="PEER-SESSION-CONTENTION")
            script = str(Path(__file__).resolve().parents[1] / "memory_index.py")
            t0 = time.time()
            r = subprocess.run([sys.executable, script, "regen", "--best-effort"],
                               env=env, capture_output=True, text=True, timeout=60)
            dt = time.time() - t0
            ok("best-effort exits 0 under contention", r.returncode == 0, r.stderr[-200:])
            ok(f"best-effort does not stall a session start ({dt:.1f}s)", dt < 8)
            ok("it reports the skip rather than claiming success",
               "skipped" in r.stdout, r.stdout[:120])
        finally:
            _coord_lock.release("memory-index", locks_dir=locks)

    print("test_sessionstart_does_not_regen_memory_unasked")
    # The plugin's SessionStart hook must NOT regenerate MEMORY.md. In the repo this was
    # extracted from, every session wrote memories through memory_index.py, so a regen at
    # start healed drift. A plugin user may keep a hand-written MEMORY.md; regenerating it
    # unasked would replace their index with one built from file metadata they never wrote.
    # Regen stays available on demand (`memory_index.py regen`).
    hook = (Path(__file__).resolve().parents[1] / "session_start.sh")
    text = hook.read_text(encoding="utf-8")
    calls = [l for l in text.splitlines()
             if "memory_index" in l and not l.lstrip().startswith("#")]
    ok("session_start.sh does not invoke memory_index", not calls, "; ".join(calls))

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("all memory-regen tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
