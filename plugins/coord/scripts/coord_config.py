#!/usr/bin/env python3
"""coord_config.py — one place to resolve the project root and read coordination settings.

Why this exists
----------------
These scripts were written as `<repo>/scripts/*.py`, sitting directly inside the project
they coordinate. As a Claude Code plugin they instead live in the plugin cache — nowhere
near the user's repo — so every `Path(__file__).resolve().parent.parent`-style "repo root"
derivation is wrong: it names a directory inside the PLUGIN, not the user's project.

`project_root()` is the replacement: it trusts the environment the harness hands a plugin
script (`$CLAUDE_PROJECT_DIR`) first, then falls back to the process's current working
directory, and resolves that to the enclosing git repo's toplevel when there is one.

This module also centralises the small set of tunables that used to be scattered as
`int(os.environ.get(...))` one-liners across half a dozen scripts, plus a handful of
settings whose CURRENT values are specific to the app this toolkit was extracted from
(a literal `supabase/migrations/` path, an Xcode `project.pbxproj` reference, …). Those
default to empty/generic here — a project adopting this plugin supplies its own via
`.claude/coord.json`.

Precedence (low to high): built-in default < `<project_root>/.claude/coord.json` < env var.

CLI:
    python3 scripts/coord_config.py root            # resolved project root
    python3 scripts/coord_config.py get <key>        # resolved value, JSON-encoded
    python3 scripts/coord_config.py dump             # the full merged config, JSON
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

CONFIG_FILENAME = ".claude/coord.json"

# key -> (default, env var name or None). List/dict-valued keys have no env override —
# there is no sane scalar encoding for "a list of globs" that is worth the ambiguity, so
# those are file-config-only.
_SCHEMA: dict[str, tuple[object, str | None]] = {
    "stale_seconds": (86400, "COORD_STALE_SECONDS"),
    "idle_badge_seconds": (3600, "COORD_IDLE_BADGE_SECONDS"),
    "representation_stale_seconds": (21600, "COORD_REPRESENTATION_STALE_SECONDS"),
    "registry_cap": (200, "SESSION_REGISTRY_CAP"),
    "closeout_stale_branch_days": (7, "CLOSEOUT_STALE_BRANCH_DAYS"),
    "memory_hot_budget_bytes": (20000, "COORD_MEMORY_HOT_BUDGET_BYTES"),
    # lock_guard.py blocks an edit to a file another live session holds; true downgrades
    # that to a warning (the lock is still reported, the edit proceeds).
    "locks_advisory": (False, "COORD_LOCKS_ADVISORY"),
    # None means "no override — the caller falls back to its own derivation".
    "memory_dir": (None, "CLAUDE_MEMORY_DIR"),
    # App-specific in the repo this was extracted from (supabase/migrations/,
    # Documentation/Reports/, dev-graph-out/, a PromptSnapshots dir, baseline-file
    # globs). Default empty: a project supplies the patterns that apply to it.
    "churn_globs": ([], None),
    # Filename suffixes treated as "additive, merge-friendly, don't flag as a real
    # collision" (the app this shipped from used this for an Xcode project.pbxproj).
    "additive_files": ([], None),
    # Bucket definitions for closeout_ledger's uncommitted-file classification:
    # [{"name": "migration", "prefix": "supabase/migrations/", "suffix": ".sql"}, ...].
    # Empty by default — everything lands in the "other" catch-all until configured.
    "closeout_buckets": ([], None),
}

DEFAULTS: dict[str, object] = {k: v[0] for k, v in _SCHEMA.items()}

_CONFIG_CACHE: dict[str, dict] = {}  # keyed by str(resolved project root)


def _git_toplevel(cwd: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(cwd), capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def project_root() -> Path:
    """The user's project root — NEVER cached, since callers change cwd/env between calls
    (tests chdir into scratch repos; a long-lived process may be reused across projects).

    Resolution order:
      1. `$CLAUDE_PROJECT_DIR`, if set and an existing directory — the harness's own
         statement of which project this invocation is for.
      2. Otherwise the current working directory.
      3. Either base is then resolved to its enclosing git repo's toplevel (`git
         rev-parse --show-toplevel` run WITH that base as cwd); if git fails (no repo,
         no git binary), the base itself is returned unchanged.
    """
    base_str = os.environ.get("CLAUDE_PROJECT_DIR", "")
    base = Path(base_str) if base_str and Path(base_str).is_dir() else Path.cwd()
    top = _git_toplevel(base)
    return Path(top) if top else base


def _read_file_config(root: Path) -> dict:
    path = root / CONFIG_FILENAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"coord_config: malformed {path} ({exc}); using defaults", file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        print(f"coord_config: {path} is not a JSON object; using defaults", file=sys.stderr)
        return {}
    return data


def _load(root: Path) -> dict:
    """Defaults merged with `<root>/.claude/coord.json`. Cached per resolved root — the
    FILE read is what's cached, not env vars (those are re-read on every `get()` call, see
    below), so a test that flips an env var mid-run still sees the new value immediately."""
    key = str(root)
    cached = _CONFIG_CACHE.get(key)
    if cached is not None:
        return cached
    cfg = dict(DEFAULTS)
    cfg.update(_read_file_config(root))
    _CONFIG_CACHE[key] = cfg
    return cfg


def _coerce(raw: str, default: object):
    if isinstance(default, bool):
        return raw.strip().lower() not in ("", "0", "false", "no")
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            return default
    return raw


def get(key: str, *, root: Path | None = None):
    """Resolve one setting: built-in default < coord.json (for the given/current project
    root) < env var. `root` lets a caller that already knows its project root (e.g. a
    function that takes one as a parameter) avoid re-deriving it; omitted, it's computed
    fresh via `project_root()`.

    Checks the env var FIRST and returns immediately when it's set. This is an efficiency
    concern, not just a precedence one: `project_root()` shells out to git, and a caller
    like `memory_dir()` (overridden via `CLAUDE_MEMORY_DIR` on nearly every real and test
    invocation) would otherwise pay that subprocess cost on every call only to throw the
    result away in favor of the env var. Measured: 16 concurrent memory_index.py writers,
    each calling `memory_dir()` several times per run, turned into enough concurrent `git`
    forks to push lock acquisition past its 30s timeout — a flake with no git-call-order
    change otherwise visible in the diff.
    """
    if key not in _SCHEMA:
        raise KeyError(f"coord_config: unknown setting {key!r}")
    _, env_name = _SCHEMA[key]
    if env_name:
        raw = os.environ.get(env_name)
        if raw:
            return _coerce(raw, DEFAULTS[key])
    effective_root = root if root is not None else project_root()
    cfg = _load(effective_root)
    return cfg.get(key, DEFAULTS[key])


def parse_utc(value) -> "datetime.datetime | None":
    """Parse a coordination timestamp into a NAIVE UTC datetime, or None if unparseable.

    Two formats are written: naive UTC with a `Z` suffix (registry, locks, reaper) and
    timezone-aware ISO (`session_manifest`). Every age computation subtracts from a naive
    UTC "now", so an aware value must be converted first — subtracting it directly raises
    TypeError, which callers catching only ValueError do not survive.
    """
    import datetime
    try:
        ts = datetime.datetime.fromisoformat(str(value or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is not None:
        ts = ts.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return ts


def utcnow_naive() -> "datetime.datetime":
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def dump(root: Path | None = None) -> dict:
    effective_root = root if root is not None else project_root()
    return {k: get(k, root=effective_root) for k in _SCHEMA}


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] == "root":
        print(project_root())
        return 0
    if args[0] == "get" and len(args) > 1:
        print(json.dumps(get(args[1])))
        return 0
    if args[0] == "dump":
        print(json.dumps(dump(), indent=2))
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    sys.exit(main())
