#!/usr/bin/env python3
"""Guard: the memory drift checker reports real drift and invents none.

`memory_index.check()` answers two questions — which memory files nothing points at, and which
index links point at nothing. It had no test, and it was quietly wrong in a way that would have
compounded: it globbed `*.md` and excluded only `MEMORY.md`, so every `MEMORY.<role>.md` — a
GENERATED view over role-tagged memories, not a memory — was reported as needing an index
entry. One false finding per role partition adopted, from a checker generating work for its own
sibling.

The precision requirement is not fussiness. The function's own docstring records that counting
markdown links alone once produced mostly-false findings across the real memory directory, and
that "a checker that is mostly wrong is a checker everyone learns to skip". A checker only
slightly wrong in a compounding way ends up in the same place, more slowly.

So both directions are asserted here. A checker that reports nothing is as useless as one that
reports everything, and only testing the quiet direction is how the first version passed
review.

Run: python3 scripts/tests/test_memory_drift_check.py
"""

from __future__ import annotations
import os
import sys
import tempfile
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))

import memory_index as mi  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}{'' if cond or not detail else f' — {detail}'}")
    if not cond:
        FAILURES.append(label)


class Mem:
    """A throwaway memory dir, via the documented CLAUDE_MEMORY_DIR override."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.prev = os.environ.get("CLAUDE_MEMORY_DIR")
        os.environ["CLAUDE_MEMORY_DIR"] = self.tmp.name
        return Path(self.tmp.name)

    def __exit__(self, *exc):
        if self.prev is None:
            os.environ.pop("CLAUDE_MEMORY_DIR", None)
        else:
            os.environ["CLAUDE_MEMORY_DIR"] = self.prev
        self.tmp.cleanup()
        return False


def test_a_generated_role_index_is_not_mistaken_for_a_memory():
    """The regression. `MEMORY.<role>.md` is a generated index; reporting it as an unindexed
    memory asks a human to index an artifact that is rewritten from scratch on every run."""
    print("test_a_generated_role_index_is_not_mistaken_for_a_memory")
    with Mem() as d:
        (d / "MEMORY.md").write_text("# Index\n- [A](alpha.md) — hook\n")
        (d / "alpha.md").write_text("fact\n")
        (d / "MEMORY.ui-ux.md").write_text("# ui-ux memory\n- [A](alpha.md) — hook\n")
        (d / "MEMORY.system-infra.md").write_text("# system-infra memory\n")
        r = mi.check()
    check("no generated index is reported as unindexed",
          not [f for f in r["unindexed"] if f.startswith("MEMORY.")], str(r["unindexed"]))
    check("and the real, indexed memory is not reported either",
          "alpha.md" not in r["unindexed"], str(r["unindexed"]))


def test_it_still_reports_real_drift():
    """The counterweight. A checker that reports nothing is as useless as one that reports
    everything — and silencing is the easier mistake to make while feeling like a fix."""
    print("test_it_still_reports_real_drift")
    with Mem() as d:
        (d / "MEMORY.md").write_text("# Index\n- [A](alpha.md) — hook\n- [Gone](vanished.md) — x\n")
        (d / "alpha.md").write_text("fact\n")
        (d / "orphan.md").write_text("nobody points at me\n")
        (d / "MEMORY.ui-ux.md").write_text("# ui-ux memory\n")
        r = mi.check()
    check("a memory nothing references IS reported", "orphan.md" in r["unindexed"],
          str(r["unindexed"]))
    check("a link to a missing file IS reported", "vanished.md" in r["deadLinks"],
          str(r["deadLinks"]))
    check("...and the generated index did not become a dead link either",
          not [f for f in r["deadLinks"] if f.startswith("MEMORY.")], str(r["deadLinks"]))


def test_a_bare_slug_counts_as_a_reference():
    """Deliberate convention, not an omission: the index compresses resolved topics into
    'Recall by name' lines that list slugs without link syntax. Treating those as unreferenced
    is what produced mostly-false findings, and is recorded in `check()`'s own docstring."""
    print("test_a_bare_slug_counts_as_a_reference")
    with Mem() as d:
        (d / "MEMORY.md").write_text("# Index\nRecall by name: beta_topic_2026_01_01\n")
        (d / "beta_topic_2026_01_01.md").write_text("fact\n")
        r = mi.check()
    check("a slug mentioned without link syntax counts as referenced",
          "beta_topic_2026_01_01.md" not in r["unindexed"], str(r["unindexed"]))


def main() -> int:
    for _, fn in sorted((k, v) for k, v in globals().items()
                        if k.startswith("test_") and callable(v)):
        fn()
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("\nall memory-drift-check tests passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
