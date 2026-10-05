#!/usr/bin/env python3
"""memory_index.py — concurrency-safe writes to the native memory index (MEMORY.md).

The bug this fixes
------------------
The native memory directory is a SINGLE physical location shared by the main checkout and
every worktree of this repo (verified: 10 worktree project slugs exist under
~/.claude/projects/, none has its own memory/ — they all resolve to the main slug, which
holds all 301 files). It sits outside the git tree, so no coordination mechanism sees it:
the edit-time lock guard (`lock_guard.py`) guards repo-relative paths only, and there is
no lock, no CAS, no hook gating writes to it.

With ~10 live sessions that means MEMORY.md is a shared mutable file with last-writer-wins
semantics. A full-file rewrite by two sessions at once silently loses one session's index
lines — no error, no detection. This module makes index updates atomic and merge-based
instead of rewrite-based.

Note the lock lives in the SHARED cross-worktree channel, not the per-worktree
.claude/coordination/locks — a per-worktree lock cannot protect a cross-worktree file.
Individual memory files are one-fact-per-file and do not collide; only the index does.

CLI
---
    python3 scripts/memory_index.py add <file.md> --title "..." --hook "..." [--section "..."]
    python3 scripts/memory_index.py check            # drift: unindexed files / dead links
    python3 scripts/memory_index.py path             # where the memory dir resolves to
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import _agent_channel  # noqa: E402
import _coord_lock  # noqa: E402
import coord_config  # noqa: E402

LOCK_NAME = "memory-index"
LOCK_TIMEOUT = 30.0
DEFAULT_SECTION = "Active Features"


def _git(*args: str) -> str:
    try:
        out = subprocess.run(["git", *args], capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def memory_dir() -> Path:
    """Resolve the native memory dir for THIS project.

    Derived from the MAIN checkout (the git common dir's parent), not the current worktree:
    the harness resolves memory by logical project identity, so every worktree shares the
    main checkout's slug. CLAUDE_MEMORY_DIR overrides (used by the tests) — routed through
    coord_config so it's one lookup, but the override behavior and the derivation below are
    unchanged.
    """
    override = coord_config.get("memory_dir")
    if override:
        d = Path(override)
        d.mkdir(parents=True, exist_ok=True)
        return d
    common = _git("rev-parse", "--path-format=absolute", "--git-common-dir")
    main_root = Path(common).parent if common else Path.cwd()
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(main_root))
    return Path.home() / ".claude" / "projects" / slug / "memory"


def index_path() -> Path:
    return memory_dir() / "MEMORY.md"


def role_index_path(role: str) -> Path:
    """Per-role index: `MEMORY.<role>.md`, beside the global one.

    Why the INDEX is partitioned and the memory FILES are not
    ---------------------------------------------------------
    The context cost is the index: `MEMORY.md` is auto-loaded into every session whole,
    already compressed into name-lists because one-line-per-memory stopped fitting. In
    practice most memory files belong to a handful of domains, so a session working in one
    domain pays for a large majority of the index it will never open.

    Moving the FILES into role directories would fix nothing that matters and break a great
    deal: every `[title](file.md)` pointer and every `[[wikilink]]` cross-reference resolves
    by bare filename. Partitioning the index gets the whole budget win with none of that
    risk, and it degrades safely — an untagged memory simply stays in the global index,
    which is where it is today.
    """
    return memory_dir() / f"MEMORY.{role}.md"


def _frontmatter_meta(path: Path, key: str) -> str:
    """One `key:` from a memory file's `metadata:` block, or "" if absent.

    Generalized from the role-only reader so `hook:` and `pin:` share one parser. A second
    hand-rolled front-matter reader is how two accessors end up disagreeing about the same
    file — the mistake `_role_header` already records for the index headers.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if not text.startswith("---"):
        return ""
    head = text.split("---", 2)[1] if text.count("---") >= 2 else ""
    m = re.search(rf"^\s+{re.escape(key)}:\s*(.+?)\s*$", head, re.M)
    return m.group(1) if m else ""


def _frontmatter_top(path: Path, key: str) -> str:
    """One TOP-LEVEL front-matter field (`description:`, `name:`) — not under `metadata:`."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    if not text.startswith("---"):
        return ""
    head = text.split("---", 2)[1] if text.count("---") >= 2 else ""
    m = re.search(rf'^{re.escape(key)}:\s*"?(.*?)"?\s*$', head, re.M)
    return m.group(1).strip() if m else ""


def _frontmatter_role(path: Path) -> str:
    """The `role:` recorded in a memory file's metadata block, or "" if untagged."""
    return _frontmatter_meta(path, "role")


def stamp_meta(filename: str, key: str, value: str) -> bool:
    """Record `key: value` in the file's metadata block. Idempotent; True if written.

    `re.sub` replacements go through a lambda so a value containing a backslash or a `\\g`
    sequence is inserted LITERALLY. Passing the value inside an f-string template makes it
    a replacement PATTERN, and a hook full of backslashes (regex snippets are common in
    these memories) would then corrupt the file it was meant to describe.
    """
    p = memory_dir() / Path(filename).name
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return False
    if _frontmatter_meta(p, key) == value:
        return False
    if re.search(rf"^\s+{re.escape(key)}:\s*.+$", text, re.M):
        text = re.sub(rf"^(\s+){re.escape(key)}:\s*.+$",
                      lambda m: f"{m.group(1)}{key}: {value}", text, count=1, flags=re.M)
    elif re.search(r"^metadata:\s*$", text, re.M):
        text = re.sub(r"^(metadata:\s*)$",
                      lambda m: f"{m.group(1)}\n  {key}: {value}", text, count=1, flags=re.M)
    else:
        return False
    p.write_text(text, encoding="utf-8")
    return True


def stamp_role(filename: str, role: str) -> bool:
    """Record `role:` in the file's metadata block. Idempotent; returns True if written."""
    return stamp_meta(filename, "role", role)


def _shared_locks_dir() -> Path:
    d = _agent_channel.channel_dir() / "locks"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _entry_line(filename: str, title: str, hook: str) -> str:
    hook = " ".join((hook or "").split())
    return f"- [{title}]({filename})" + (f" — {hook}" if hook else "")


def add_entry(filename: str, title: str, hook: str, section: str = DEFAULT_SECTION,
              role: str = "") -> str:
    """Insert or update one pointer line under `section`, atomically.

    Read-modify-write happens entirely inside the shared lock, and the replacement is
    written via a temp file + os.replace so a reader never observes a half-written index.

    With `role`, the pointer goes to that role's index instead of the global one and the
    role is stamped into the file's metadata, so `--regen-roles` can rebuild every role
    index from the files themselves. Migration is incremental on purpose: a memory is
    tagged when it is written or next touched, and everything untagged keeps working
    exactly as it does today (the memory contract is unchanged for callers who
    pass no role).
    """
    filename = Path(filename).name
    if role:
        stamp_role(filename, role)
    elif not (memory_dir() / filename).exists():
        # The global index is GENERATED from the files, so a pointer to a file that does not
        # exist is a dead link the next regen drops anyway. Refuse loudly instead.
        raise SystemExit(
            f"{filename} does not exist in {memory_dir()} — write the memory file "
            f"first, then index it. (A pointer to a missing file is a dead link.)"
        )
    idx = role_index_path(role) if role else index_path()
    idx.parent.mkdir(parents=True, exist_ok=True)

    if not _coord_lock.acquire(LOCK_NAME, timeout=LOCK_TIMEOUT,
                               locks_dir=_shared_locks_dir()):
        raise SystemExit(
            f"could not acquire the '{LOCK_NAME}' lock within {LOCK_TIMEOUT}s — another "
            f"session is writing the index. Retry; do not edit MEMORY.md directly."
        )
    try:
        if not role:
            # Global index: the durable home for a title/hook is the memory FILE, because
            # the index is regenerated from it. Appending a line here would be erased —
            # along with the hook — on the very next regen.
            stamp_meta(filename, "title", " ".join((title or "").split()))
            if hook:
                stamp_meta(filename, "hook", " ".join(hook.split()))
            # A non-default section expresses "durable reference", which the generated model
            # spells `pin: true` — the same mapping the migration used for the old
            # Conventions/Process entries.
            if section and section.strip() != DEFAULT_SECTION:
                stamp_meta(filename, "pin", "true")
            regen_global(_locked=True)
            return "indexed"

        default_header = ("\n".join(_role_header(role)) + "\n" if role
                          else "# Project Memory\n")
        text = idx.read_text() if idx.exists() else default_header
        lines = text.splitlines()
        new_line = _entry_line(filename, title, hook)

        # Replace an existing pointer to the same file wherever it already lives, so a
        # re-index updates in place instead of duplicating.
        link = f"]({filename})"
        for i, ln in enumerate(lines):
            if link in ln and ln.lstrip().startswith("-"):
                lines[i] = new_line
                _write(idx, lines)
                return "updated"

        # Otherwise append under the requested section, creating it if absent.
        hdr = None
        for i, ln in enumerate(lines):
            if ln.strip().lower().lstrip("# ").strip() == section.strip().lower():
                hdr = i
                break
        if hdr is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"## {section}")
            lines.append(new_line)
        else:
            end = hdr + 1
            while end < len(lines) and not lines[end].startswith("## "):
                end += 1
            while end > hdr + 1 and not lines[end - 1].strip():
                end -= 1
            lines.insert(end, new_line)
        _write(idx, lines)
        return "added"
    finally:
        _coord_lock.release(LOCK_NAME, locks_dir=_shared_locks_dir())


def _role_header(role: str) -> list[str]:
    """The role index header — ONE definition, used by both writers.

    `add_entry` and `regen_role_indexes` each built their own, and they disagreed: `add`'s
    carried no do-not-hand-edit warning, so an index it created invited exactly the editing
    that the next `regen-roles` silently overwrites. It also let a test assert against one
    producer and pass while the other was wrong.

    The command named here must be the real subcommand (`regen-roles`, not `--regen-roles`) —
    this line is what a reader follows to recover the file, so a citation that does not resolve
    fails them at the worst moment.
    """
    return [f"# {role} memory", "",
            "Derived from `role:` metadata — regenerate with "
            "`python3 scripts/memory_index.py regen-roles`; do not hand-edit.", ""]


def regen_role_indexes() -> dict:
    """Rebuild every `MEMORY.<role>.md` from the files' own `role:` metadata.

    Derived, not accumulated: the role indexes are a VIEW of what the files say, so a
    hand-edit or a half-finished tagging run self-heals on the next regen instead of
    leaving a pointer to a memory that no longer claims that role. Same contract as any
    other generated-index file elsewhere in a project (a debt register's README, a
    domain map) — derive it, never hand-edit it.

    The global `MEMORY.md` is deliberately left alone — untagged memories keep working
    exactly as they do now, so this is safe to run at any point during the migration.
    """
    by_role: dict[str, list[tuple[str, str, str]]] = {}
    for p in sorted(memory_dir().glob("*.md")):
        if p.name.startswith("MEMORY"):
            continue
        role = _frontmatter_role(p)
        if not role:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        m = re.search(r'^description:\s*"?(.*?)"?\s*$', text, re.M)
        hook = (m.group(1) if m else "")[:180]
        by_role.setdefault(role, []).append((p.name, p.stem, hook))

    written = {}
    for role, rows in sorted(by_role.items()):
        lines = _role_header(role) + [f"## {DEFAULT_SECTION}"]
        lines += [_entry_line(fn, stem, hook) for fn, stem, hook in sorted(rows)]
        idx = role_index_path(role)
        _write(idx, lines)
        written[role] = len(rows)
    return written


def _write(idx: Path, lines: list[str]) -> None:
    tmp = idx.with_suffix(f".tmp-{os.getpid()}")
    tmp.write_text("\n".join(lines).rstrip("\n") + "\n")
    os.replace(tmp, idx)


GENERATED_MARK = (
    "<!-- GENERATED by scripts/memory_index.py regen — do not hand-edit. "
    "Edit the memory file's `hook:`/`pin:` metadata, then re-run. -->"
)

# The always-on budget. MEMORY.md is loaded into EVERY session whole, so this number is a
# per-session context tax paid on every turn, not a disk figure.
#
# Why a budget at all, rather than a bigger ceiling: measured growth is a few KB a day
# against an index already tens of KB in size. Any fixed raise is consumed in days, so an
# unbounded index has no stable size — only a bounded one does. Raising the ceiling and
# raising this constant are complementary; only this makes the size hold.
# 20,000 is chosen, not guessed: a sweep over a real memory directory showed a much smaller
# budget keeps only a few days of history (archiving durable safety lessons on age alone),
# while 20K keeps roughly as many entries as the hand-maintained index it replaced used to
# carry, and still sits comfortably under what demonstrably loaded before. Tune THIS ONE
# NUMBER; everything else is derived.
#
# Anything that must never rotate out on age takes `pin: true`, which is deliberately a
# HUMAN judgment. Auto-pinning by keyword (rls/consent/security/…) was considered and
# rejected: that matches vocabulary rather than the property, which is the exact defect
# fixed in audit_debt_registry's TRIGGER_RE the same day.
HOT_BUDGET_BYTES = coord_config.get("memory_hot_budget_bytes")

# A hook longer than this is truncated in the index. The full text is one file-open away,
# and the index exists to help you decide WHICH file to open.
HOOK_MAX = 200


def archive_index_path() -> Path:
    """Where entries that do not fit the hot budget are still listed, in full."""
    return memory_dir() / "MEMORY.archive.md"


def _memory_files() -> list[Path]:
    return sorted(p for p in memory_dir().glob("*.md") if not p.name.startswith("MEMORY"))


def _hook_for(path: Path) -> str:
    """The index hook for one memory: `hook:` metadata, else the `description:` field.

    ORDERED fallback — `hook:` always wins when present. The two fields are different
    registers, measured in practice across the indexed files: curated hooks are
    operationally denser ("what will bite you"), descriptions are fuller summaries, and a
    meaningful fraction of hooks carried materially more than their description. Preferring
    description would silently discard that, which is why the migration writes each
    curated hook into its own file first.
    """
    hook = _frontmatter_meta(path, "hook") or _frontmatter_top(path, "description")
    hook = " ".join(hook.split())
    return hook[:HOOK_MAX - 1] + "…" if len(hook) > HOOK_MAX else hook


def _is_pinned(path: Path) -> bool:
    return _frontmatter_meta(path, "pin").strip().lower() in ("true", "yes", "1")


def _recency_key(path: Path) -> str:
    """Sort key for "most recent first", as an ORDERED cascade.

    `modified:` (33% coverage) → the date in the filename (91%) → mtime (100%). Ordered,
    never overriding: a later source fills a gap the earlier one left, and can't contradict
    it. Widening a deriver by pattern-matching instead of ordered fallback is how a marker
    gets flipped — recorded already for the registry derivers.

    mtime is last precisely because it is the least trustworthy: a bulk edit or a git
    operation restamps it without the memory having changed.
    """
    mod = _frontmatter_meta(path, "modified")
    if re.match(r"\d{4}-\d{2}-\d{2}", mod):
        return mod[:10]
    m = re.search(r"_(\d{4})_(\d{2})_(\d{2})\.md$", path.name)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return datetime.date.fromtimestamp(path.stat().st_mtime).isoformat()


def _title_for(path: Path) -> str:
    """Link text for one memory: curated `title:` → `name:` → the filename stem.

    ORDERED, and `title:` leads for the same reason `hook:` does. The curated index titles
    carried a human summary plus the ⭐ importance marks ("⭐⭐Beta delivery — VERIFY
    archive+upload before 'shipped'; build# BURN trap"); `name:` is usually just the slug.
    Falling back to the slug first would have quietly downgraded every title in the index.
    """
    return (_frontmatter_meta(path, "title")
            or _frontmatter_top(path, "name")
            or path.stem)


def regen_global(budget: int = HOT_BUDGET_BYTES, _locked: bool = False,
                 lock_timeout: float = LOCK_TIMEOUT,
                 best_effort: bool = False) -> dict | None:
    """Rebuild MEMORY.md from the memory files themselves, bounded by `budget`.

    Generated, not accumulated. `add_entry` inserts a line and never rebuilds from disk, so
    the index only ever grew — the same shape as the hand-maintained debt index that drifted
    to covering 45% of its own register before it was made generated. A derived index cannot
    drift, and that is the point.

    Selection: every `pin: true` memory, then most-recent-first until the budget is spent.
    Everything else goes to MEMORY.archive.md — ARCHIVED, never dropped. Nothing is deleted
    and no file leaves disk; the archive simply is not loaded into every session.

    Concurrency falls out of this for free: two sessions each write their own memory file
    and then regenerate deterministically from disk, so their edits commute. Today they
    contend on one shared mutable file and the loser is silently overwritten.
    """
    # Every writer of MEMORY.md takes the SHARED cross-worktree lock. Regen rewrites the
    # whole file, so an unlocked regen is precisely the last-writer-wins hazard this module
    # exists to remove — worse than the append it replaced, because it clobbers everything
    # rather than one line. `_locked` is for callers already inside the lock (add_entry);
    # re-acquiring there would deadlock against ourselves.
    if not _locked:
        if not _coord_lock.acquire(LOCK_NAME, timeout=lock_timeout,
                                   locks_dir=_shared_locks_dir()):
            if best_effort:
                # Whoever holds the lock regenerates from the same files on the same disk,
                # so their write IS ours. Skipping is correct here, not a degraded fallback
                # — and it keeps a session-start hook from ever stalling on a peer.
                return None
            raise SystemExit(
                f"could not acquire the '{LOCK_NAME}' lock within {lock_timeout}s — "
                f"another session is writing the index. Retry."
            )
        try:
            return regen_global(budget, _locked=True)
        finally:
            _coord_lock.release(LOCK_NAME, locks_dir=_shared_locks_dir())

    files = _memory_files()
    pinned = [p for p in files if _is_pinned(p)]
    rest = sorted((p for p in files if not _is_pinned(p)),
                  key=lambda p: (_recency_key(p), p.name), reverse=True)

    header = ["# Project Memory", "", GENERATED_MARK, ""]
    pin_lines = [_entry_line(p.name, _title_for(p), _hook_for(p))
                 for p in sorted(pinned, key=lambda p: p.name)]

    used = len("\n".join(header).encode()) + sum(len(l.encode()) + 1 for l in pin_lines)
    used += 200  # section headings + the archive pointer, budgeted before selection

    hot, overflow = [], []
    for p in rest:
        line = _entry_line(p.name, _title_for(p), _hook_for(p))
        cost = len(line.encode()) + 1
        if used + cost <= budget:
            hot.append((p, line))
            used += cost
        else:
            overflow.append(p)

    def _assemble(hot_lines: list[str], n_over: int) -> list[str]:
        out = list(header)
        if pin_lines:
            out += ["## Pinned", ""] + pin_lines + [""]
        out += ["## Recent", ""] + hot_lines + [""]
        out += ["## Archive", "",
                f"- {n_over} older memories are indexed in "
                f"[MEMORY.archive.md](MEMORY.archive.md) — same one-line format, not "
                f"auto-loaded. Every memory file is still on disk; grep the directory by "
                f"keyword to recall one by name."]
        return out

    # Trim against the ASSEMBLED size, not an estimate. The scaffold cost cannot be known
    # up front -- the archive pointer embeds the overflow count, which is only settled once
    # selection ends, so the two depend on each other. A fixed reserve is a guess, and it
    # was wrong by 43 bytes on the real corpus: the function reported a budget it had just
    # exceeded. Measure the real thing and give entries back until it fits.
    lines = _assemble([l for _, l in hot], len(overflow))
    while hot and len(("\n".join(lines) + "\n").encode()) > budget:
        p_over, _ = hot.pop()
        overflow.insert(0, p_over)
        lines = _assemble([l for _, l in hot], len(overflow))

    arch = ["# Project Memory — archive", "", GENERATED_MARK, "",
            "Memories outside the always-on budget. Nothing here is deleted or less true — "
            "it is simply not loaded into every session.", "", "## Archived", ""]
    arch += [_entry_line(p.name, _title_for(p), _hook_for(p)) for p in overflow]

    # Subset invariant: vocabulary (files on disk) MUST be a subset of what the
    # indexes reach. A generated index that silently drops a file is strictly worse than the
    # accumulating one it replaces, because the loss is invisible. Assert, never assume.
    reached = {p.name for p in pinned} | {p.name for p, _ in hot} | {p.name for p in overflow}
    # Re-OBSERVE the directory here. Comparing `reached` against the same `files` list the
    # partition was built from is vacuous -- the two can never disagree, so the guard would
    # have exited 0 forever while looking rigorous. Caught by its own mutation test, which
    # is the only reason this is a real check and not decoration.
    missing = {p.name for p in _memory_files()} - reached
    if missing:
        raise SystemExit(
            f"refusing to write: {len(missing)} memory file(s) would vanish from both "
            f"indexes — {sorted(missing)[:5]}"
        )

    _write(index_path(), lines)
    _write(archive_index_path(), arch)
    return {"pinned": len(pinned), "hot": len(hot), "archived": len(overflow),
            "bytes": len(index_path().read_bytes()), "budget": budget,
            "files": len(files)}


def migrate_hooks_from_index() -> dict:
    """One-time: move each curated hook OUT of MEMORY.md and INTO its own memory file.

    The index's hooks were hand-written and are not the same text as the files'
    `description:` — measured across 92 indexed files, 18 hooks carried materially more
    than their description ("⭐⭐two-store rule: HK←session-owner(watch)…" vs a neutral
    one-line summary). Regenerating from `description:` alone would have silently thrown
    that away, which is the kind of loss nobody notices until they need the memory.

    So the migration is hook-preserving: the curated text becomes the file's own `hook:`,
    and the generator then has ONE canonical source per memory. Idempotent — a file that
    already carries a `hook:` is left alone.

    The pinned set is seeded from the sections a human already curated: everything outside
    "Active Features" (Conventions, Process, Web & Design Assets) is durable reference that
    should never rotate out on recency.
    """
    idx = index_path()
    if not idx.exists():
        return {"hooks": 0, "pins": 0, "skipped": 0}

    section, hooks, pins = None, 0, 0
    seen: set[str] = set()
    for ln in idx.read_text(encoding="utf-8").split("\n"):
        if ln.startswith("## "):
            section = ln[3:].strip()
            continue
        m = re.match(r"- \[.*?\]\(([^)]+\.md)\)\s*—?\s*(.*)$", ln.strip())
        if not m:
            continue
        fn, hook = m.group(1), " ".join(m.group(2).split())
        if not (memory_dir() / fn).exists():
            continue
        seen.add(fn)
        title = " ".join((re.match(r"- \[(.*?)\]\(", ln.strip()).group(1) or "").split())
        if title:
            stamp_meta(fn, "title", title)
        if hook and stamp_meta(fn, "hook", hook):
            hooks += 1
        if section and section != DEFAULT_SECTION and stamp_meta(fn, "pin", "true"):
            pins += 1
    return {"hooks": hooks, "pins": pins, "indexed": len(seen)}


def check() -> dict:
    """Drift between the index and the files on disk.

    A memory counts as referenced if MEMORY.md either links it `[Title](file.md)` OR
    mentions its bare slug. The bare-slug form is deliberate convention, not an omission —
    the index compresses resolved topics into "Recall by name" lines listing slugs only.
    Counting links alone reported 250 false problems against 303 files, and a checker that
    is mostly wrong is a checker everyone learns to skip.
    """
    d = memory_dir()
    idx = index_path()
    text = idx.read_text() if idx.exists() else ""
    # The archive counts as indexed. Reading only MEMORY.md reported 294 of 366 memories as
    # unindexed the moment the index became budgeted — every one of them correctly listed in
    # MEMORY.archive.md. This function's own docstring is the reason that matters: a checker
    # that is mostly wrong is one everybody learns to skip.
    arch = archive_index_path()
    if arch.exists():
        text += "\n" + arch.read_text()
    linked = set(re.findall(r"\]\(([^)]+\.md)\)", text))
    # Exclude every INDEX, not just the global one. `MEMORY.<role>.md` files are generated
    # views over role-tagged memories, so counting them as memories reported each new role
    # partition as an unindexed file that needs indexing — a checker generating false work
    # for its own sibling. The docstring above already records why a mostly-wrong checker is
    # worse than none: people learn to skip it, and then it protects nothing.
    on_disk = {f.name for f in d.glob("*.md") if not f.name.startswith("MEMORY.")}

    unreferenced = sorted(
        f for f in on_disk
        if f not in linked and Path(f).stem not in text
    )
    return {
        "memoryDir": str(d),
        "linked": len(linked),
        "referenced": len(on_disk) - len(unreferenced),
        "onDisk": len(on_disk),
        "unindexed": unreferenced,
        # An index linking a sibling INDEX (MEMORY.md → MEMORY.archive.md) is navigation,
        # not a memory pointer. `on_disk` deliberately excludes MEMORY.*, so without this
        # the archive pointer reports as the one dead link in an otherwise clean tree.
        "deadLinks": sorted(f for f in (linked - on_disk)
                            if not Path(f).name.startswith("MEMORY.")),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Concurrency-safe MEMORY.md index writes.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add")
    a.add_argument("file")
    a.add_argument("--title", required=True)
    a.add_argument("--hook", default="")
    a.add_argument("--section", default=DEFAULT_SECTION)
    a.add_argument("--role", default="",
                   help="write the pointer to MEMORY.<role>.md and stamp the file")
    sub.add_parser("check")
    sub.add_parser("regen-roles")
    rg = sub.add_parser("regen", help="rebuild MEMORY.md from the memory files, budgeted")
    rg.add_argument("--budget", type=int, default=HOT_BUDGET_BYTES)
    rg.add_argument("--dry-run", action="store_true")
    rg.add_argument("--best-effort", action="store_true",
                    help="skip silently if another session holds the lock "
                         "(session-start use)")
    sub.add_parser("migrate-hooks",
                   help="one-time: move curated index hooks into their memory files")
    rl = sub.add_parser("role-index")
    rl.add_argument("role")

    sub.add_parser("path")
    args = ap.parse_args()

    if args.cmd == "migrate-hooks":
        r = migrate_hooks_from_index()
        print(f"hooks written: {r['hooks']}   pins set: {r['pins']}   "
              f"indexed lines read: {r.get('indexed', 0)}")
        return 0

    if args.cmd == "regen":
        if args.dry_run:
            before = index_path().read_bytes() if index_path().exists() else b""
            r = regen_global(args.budget)
            index_path().write_bytes(before)
            print("(dry run — MEMORY.md restored; MEMORY.archive.md was written)")
        else:
            r = regen_global(
                args.budget,
                lock_timeout=3.0 if args.best_effort else LOCK_TIMEOUT,
                best_effort=args.best_effort,
            )
            if r is None:
                print("another session is regenerating the index — skipped")
                return 0
        print(f"MEMORY.md  {r['bytes']:,}B / {r['budget']:,}B budget")
        print(f"  pinned   {r['pinned']}")
        print(f"  recent   {r['hot']}")
        print(f"  archived {r['archived']}  → MEMORY.archive.md")
        print(f"  files    {r['files']} (every one reachable — invariant asserted)")
        return 0

    if args.cmd == "regen-roles":
        w = regen_role_indexes()
        print(f"regenerated {len(w)} role index(es)" if w else
              "no role-tagged memories yet — nothing to regenerate")
        for r, n in sorted(w.items()):
            print(f"  MEMORY.{r}.md  {n} entr(ies)")
        return 0

    if args.cmd == "role-index":
        idx = role_index_path(args.role)
        print(idx.read_text() if idx.exists() else
              f"(no memories tagged role: {args.role} yet)")
        return 0

    if args.cmd == "path":
        print(memory_dir())
        return 0
    if args.cmd == "check":
        r = check()
        print(f"memory dir : {r['memoryDir']}")
        print(f"referenced : {r['referenced']}/{r['onDisk']}  "
              f"({r['linked']} as markdown links, the rest by bare slug)")
        if r["unindexed"]:
            print(f"unindexed  : {len(r['unindexed'])}")
            for f in r["unindexed"][:15]:
                print(f"   {f}")
            if len(r["unindexed"]) > 15:
                print(f"   … and {len(r['unindexed']) - 15} more")
        if r["deadLinks"]:
            print(f"dead links : {len(r['deadLinks'])}")
            for f in r["deadLinks"][:15]:
                print(f"   {f}")
        if not r["unindexed"] and not r["deadLinks"]:
            print("index and directory agree")
        return 0

    # `args.role` was parsed, documented in --help, and then NOT PASSED — so `--role` stamped
    # nothing and quietly wrote the pointer to the global index instead of the role's. It
    # printed success either way, which is why it survived: the flag looked wired because it
    # was accepted. Found by using it for real, not by reading it.
    result = add_entry(args.file, args.title, args.hook, args.section, role=args.role)
    print(f"{result}: {args.file} under '{args.section}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
