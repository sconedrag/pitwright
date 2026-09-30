#!/usr/bin/env bash
# model-guard.sh — PreToolUse(Agent) hook: enforce a per-agent model-tier FLOOR at spawn time.
#
# WHY: what model a subagent runs on is decided by the `model` option passed when it is spawned.
# Relying on an agent file's `model:` frontmatter alone has been observed not to govern the served
# model in some Claude Code versions — an agent declared `model: haiku` still ran on the session's
# (larger) model. Either way, a spawn with no `model` option inherits the main session's model,
# which silently overspends on work a cheaper tier handles, and a spawn BELOW an agent's intended
# tier silently risks quality. This hook makes the intended tier enforceable where it is decided:
#
#   Stage 1   BLOCK a spawn that omits `model`, or requests a model below the agent's floor.
#   Stage 1b  ADVISE (never block) when a read-only search agent is spawned above its ceiling.
#   Stage 2   ADVISE (never block) when the delegation prompt looks under-specified.
#
# Floors come from, in order: the agent's own frontmatter `model:` (project `.claude/agents/`,
# then `~/.claude/agents/`), then `.claude/spawn-guards.json` in the project, then built-in
# defaults. So re-tiering an agent in its own file automatically updates enforcement.
#
# Config (optional) — <project>/.claude/spawn-guards.json:
#   { "floors":   { "Explore": "haiku", "my-reviewer": "sonnet" },
#     "ceilings": { "Explore": "haiku" },
#     "search_agents": ["Explore"] }
#
# Override for one session: SPAWN_GUARDS_OK=1 in the environment disables all stages.
set -uo pipefail

command -v python3 >/dev/null 2>&1 || exit 0   # fail open without python3
[ "${SPAWN_GUARDS_OK:-0}" = "1" ] && exit 0

# The hook payload arrives on stdin once; three stages read it, so capture it into the
# environment (the python heredocs own stdin).
SPAWN_GUARD_JSON="$(cat)"
export SPAWN_GUARD_JSON

# Shared config loader, emitted into each stage so the stages stay independent processes.
read -r -d '' SPAWN_GUARD_CONFIG_PY <<'PY'
import json, os
RANK = {"haiku": 0, "sonnet": 1, "opus": 2, "fable": 3}
DEFAULTS = {
    "floors": {"Explore": "haiku", "general-purpose": "haiku"},
    "ceilings": {"Explore": "haiku"},
    "search_agents": ["Explore"],
}
def load_config():
    cfg = {k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in DEFAULTS.items()}
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    try:
        with open(os.path.join(root, ".claude", "spawn-guards.json")) as fh:
            user = json.load(fh)
    except (OSError, ValueError):
        return cfg
    for key in ("floors", "ceilings"):
        if isinstance(user.get(key), dict):
            cfg[key].update({k: str(v).lower() for k, v in user[key].items()})
    if isinstance(user.get("search_agents"), list):
        cfg["search_agents"] = [str(a) for a in user["search_agents"]]
    return cfg
def payload():
    try:
        data = json.loads(os.environ.get("SPAWN_GUARD_JSON", ""))
    except ValueError:
        return None
    return data.get("tool_input") or {}
PY
export SPAWN_GUARD_CONFIG_PY

# ── Stage 1 — tier FLOOR (blocking) ──────────────────────────────────────────────────
python3 <<'PY'
import os, re, sys
exec(os.environ["SPAWN_GUARD_CONFIG_PY"])

ti = payload()
if ti is None:
    sys.exit(0)  # unparseable payload -> fail open
atype = (ti.get("subagent_type") or "").strip()
requested = (ti.get("model") or "").strip().lower()
# A fork inherits the parent's model and context by design; there is no tier to enforce.
if not atype or atype == "fork":
    sys.exit(0)

def frontmatter_floor(agent):
    roots = [os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd(), os.path.expanduser("~")]
    for root in roots:
        try:
            with open(os.path.join(root, ".claude", "agents", agent + ".md")) as fh:
                head = fh.read(4000)
        except OSError:
            continue
        m = re.search(r"(?m)^model:\s*([A-Za-z]+)\s*$", head)
        if m:
            return m.group(1).strip().lower()
    return None

cfg = load_config()
floor = frontmatter_floor(atype) or cfg["floors"].get(atype)
if floor not in RANK:
    sys.exit(0)  # no floor, "inherit", or unknown -> nothing to enforce

if not requested:
    sys.stderr.write(
        f'BLOCKED: agent "{atype}" has model-tier floor "{floor}", but it was spawned with no '
        f'`model` option, so it would inherit the main session\'s model.\n'
        f'Re-spawn with model "{floor}" (or higher, if this task genuinely needs it).\n'
        f'Override for this session: SPAWN_GUARDS_OK=1.\n')
    sys.exit(2)

if requested not in RANK:
    sys.exit(0)  # a model name this hook does not know -> never block on a guess

if RANK[requested] < RANK[floor]:
    sys.stderr.write(
        f'BLOCKED: agent "{atype}" requested model "{requested}", below its floor "{floor}".\n'
        f'Re-spawn with model "{floor}" or higher. Override: SPAWN_GUARDS_OK=1.\n')
    sys.exit(2)
sys.exit(0)
PY
TIER_RC=$?
# A block is final: the advisory stages must never turn it into a pass or run after it.
[ "$TIER_RC" -ne 0 ] && exit "$TIER_RC"

# ── Stage 1b — tier CEILING (advisory) ───────────────────────────────────────────────
# The floor protects quality; the ceiling protects spend. Advisory because escalation can be
# deliberate, and a blocking ceiling would train people to set the override — which would
# switch off the floor too.
python3 <<'PY'
import os, sys
exec(os.environ["SPAWN_GUARD_CONFIG_PY"])
ti = payload() or {}
atype = (ti.get("subagent_type") or "").strip()
requested = (ti.get("model") or "").strip().lower()
ceiling = load_config()["ceilings"].get(atype)
if ceiling in RANK and requested in RANK and RANK[requested] > RANK[ceiling]:
    sys.stderr.write(
        f'\n[spawn-guards] ADVISORY — "{atype}" requested {requested}; its ceiling is {ceiling}.\n'
        f'  Read-only search rarely needs a stronger model; the difference is spend, not quality.\n'
        f'  If this search needs judgment, a general-purpose or specialist agent fits better.\n\n')
sys.exit(0)
PY

# ── Stage 2 — delegation quality (advisory) ──────────────────────────────────────────
# A delegate cannot ask a follow-up question: whatever the prompt leaves out, it invents.
# Two or more weak signals are required before speaking, because any single one is too
# often a fine prompt phrased differently.
python3 <<'PY'
import hashlib, os, re, sys
exec(os.environ["SPAWN_GUARD_CONFIG_PY"])
ti = payload() or {}
atype = (ti.get("subagent_type") or "").strip()
prompt = ti.get("prompt") or ""
if not atype or atype == "fork" or not prompt.strip():
    sys.exit(0)

search_agents = set(load_config()["search_agents"])
text = prompt.lower()
DELIVERABLE = ("return", "report", "list", "output", "provide", "summar", "answer", "identify",
               "produce", "write", "give me", "respond with", "deliver", "find", "locate",
               "search", "enumerate", "trace", "map ", "audit", "review")
ACCEPTANCE = ("verify", "test", "must ", "should ", "criteria", "done when", "acceptance",
              "ensure", "confirm", "check that", "passes", "expect")
has_anchor = bool(re.search(r"[\w/]+\.[A-Za-z]{1,5}\b", prompt)
                  or re.search(r"\b[A-Z][a-z0-9]+[A-Z][a-zA-Z0-9]*\b", prompt)
                  or "/" in prompt)

problems = []
if len(prompt.strip()) < 120 and atype not in search_agents:
    problems.append("it is very short for a non-search agent (<120 chars)")
if not any(w in text for w in DELIVERABLE):
    problems.append("it never says what to RETURN")
if not any(w in text for w in ACCEPTANCE) and atype not in search_agents:
    problems.append("it never says what DONE looks like")
if not has_anchor:
    problems.append("it names no file, path, or symbol to anchor the scope")
if len(problems) < 2:
    sys.exit(0)

# Warn once per distinct prompt (a re-spawn after a floor block should not warn twice).
state = os.environ.get("CLAUDE_PLUGIN_DATA") or os.environ.get("TMPDIR") or "/tmp"
sentinel = os.path.join(state, "spawn-guards-warned-"
                        + hashlib.sha256((atype + "|" + prompt).encode()).hexdigest()[:16])
if os.path.exists(sentinel):
    sys.exit(0)
try:
    os.makedirs(state, exist_ok=True)
    open(sentinel, "w").close()
except OSError:
    pass

sys.stderr.write(
    f'\n[spawn-guards] ADVISORY — this spawn of "{atype}" may be under-specified:\n'
    + "".join(f"  - {p}\n" for p in problems)
    + "  One more sentence naming the deliverable and how to check it is the cheapest fix.\n"
      "  For results you must not lose, ask for structured output rather than prose.\n\n")
sys.exit(0)
PY
exit 0
