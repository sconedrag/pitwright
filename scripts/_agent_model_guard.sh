#!/usr/bin/env bash
# _agent_model_guard.sh — PreToolUse(Agent) hook: enforce the Model-Tier Policy.
#
# WHY: the per-agent `model:` frontmatter is a NO-OP in the current Claude Code version
# (a `model: haiku` agent still serves Opus — verified 2026-06-05). The ONLY effective
# lever is the spawn-time `model` opt on the Agent tool. This hook makes that lever
# enforceable: it blocks an Agent spawn that omits `model` or requests a model BELOW the
# agent's assigned tier floor. Agents with no declared floor pass through. Over-tiering
# passes too, but an Explore spawn above haiku gets an ADVISORY (Stage 1b) — escalation
# for auth/RLS/medical is legitimate for specialists, and never for read-only search.
#
# Source of truth = the agent's OWN frontmatter `model:` in .claude/agents/<type>.md
# (so retiering an agent there automatically updates enforcement — DRY). Built-in agents
# with no file use the defaults below (CLAUDE.md Model-Tier Policy).
#
# Wired in settings.json (user applies via /update-config) — add to hooks.PreToolUse:
#   { "matcher": "Agent", "hooks": [
#       { "type": "command", "command": "scripts/_agent_model_guard.sh" } ] }
#
# Escape hatch (intentional over/under-ride): AGENT_MODEL_GUARD_OK=1 in the environment.
set -uo pipefail

command -v python3 >/dev/null 2>&1 || exit 0   # fail-open if no python3
[ "${AGENT_MODEL_GUARD_OK:-0}" = "1" ] && exit 0

# Read the tool-call JSON from stdin into an env var so the python heredoc (which owns
# stdin) can read it without a stdin conflict, and so single quotes in python are safe.
# Captured ONCE into an exported var because two stages consume it: the blocking tier
# check below, then the advisory delegation-quality check. stdin can only be drained once.
AGENT_GUARD_JSON="$(cat)"
export AGENT_GUARD_JSON

python3 <<'PY'
import json, os, re, sys

raw = os.environ.get("AGENT_GUARD_JSON", "")
try:
    data = json.loads(raw)
except Exception:
    sys.exit(0)  # unparseable -> fail open

ti = (data.get("tool_input") or {})
atype = (ti.get("subagent_type") or "").strip()
requested = (ti.get("model") or "").strip().lower()
if not atype:
    sys.exit(0)

RANK = {"haiku": 0, "sonnet": 1, "opus": 2, "fable": 3}

# Built-in agents have no frontmatter file (CLAUDE.md Model-Tier Policy defaults).
BUILTIN_FLOOR = {
    "Explore": "haiku",
    "general-purpose": "haiku",
    "code-reviewer": "sonnet",
    "swift-debugger": "opus",
}

def floor_for(t):
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    path = os.path.join(root, ".claude", "agents", t + ".md")
    try:
        with open(path) as f:
            head = f.read(2000)
        m = re.search(r"(?m)^model:\s*([A-Za-z]+)\s*$", head)
        if m:
            return m.group(1).strip().lower()
    except OSError:
        pass
    return BUILTIN_FLOOR.get(t)

floor = floor_for(atype)
if floor not in RANK:
    sys.exit(0)  # no declared floor / "inherit" / unknown -> nothing to enforce

if not requested:
    sys.stderr.write(
        'BLOCKED: Agent "' + atype + '" has tier floor "' + floor + '" but you spawned it '
        'with NO `model` opt.\nThe per-agent frontmatter is a NO-OP in this Claude Code '
        'version, so an Agent spawn without `model` inherits the (Opus) main-session model '
        '— defeating the Model-Tier Policy and overspending.\nRe-spawn with the Agent tool '
        '`model` opt set to "' + floor + '" (or higher to escalate, e.g. opus for an '
        'auth/RLS/medical task). See CLAUDE.md "Model-Tier Policy".\n'
        'Override: AGENT_MODEL_GUARD_OK=1.\n'
    )
    sys.exit(2)

if requested not in RANK:
    sys.exit(0)  # unrecognized model string -> do not block

if RANK[requested] < RANK[floor]:
    sys.stderr.write(
        'BLOCKED: Agent "' + atype + '" requested model "' + requested + '", which is BELOW '
        'its tier floor "' + floor + '".\nThis risks quality on work that needs the stronger '
        'model (Rule 9 / Model-Tier Policy).\nRe-spawn with model "' + floor + '" or higher. '
        'Override: AGENT_MODEL_GUARD_OK=1.\n'
    )
    sys.exit(2)

sys.exit(0)
PY

# A non-zero exit above is a real BLOCK (tier violation) — propagate it and stop. The
# advisory stage must never turn a block into a pass, nor run after one.
TIER_RC=$?
[ "$TIER_RC" -ne 0 ] && exit "$TIER_RC"

# ---------------------------------------------------------------------------------------
# Stage 1b — tier CEILING (advisory, never blocks).
#
# Stage 1 only sees UNDER-tiering. Over-tiering was invisible: measured 2026-09-27, three
# Explore spawns in one session requested sonnet where policy says haiku, and the guard
# passed all three silently. The floor protects quality; the ceiling protects spend.
#
# ADVISORY, not blocking, because escalation is sometimes legitimate (the policy names
# escalation overrides for specialists), and because a blocking ceiling that fires on a
# deliberate choice trains AGENT_MODEL_GUARD_OK=1 — which would also disable the floor
# above (Rule 42). Scoped to the ONE agent whose ceiling the policy states outright:
# Explore is read-only search, with no escalation clause anywhere in CLAUDE.md.
python3 <<'PY'
import json, os, sys

try:
    data = json.loads(os.environ.get("AGENT_GUARD_JSON", ""))
except Exception:
    sys.exit(0)

ti = data.get("tool_input") or {}
atype = (ti.get("subagent_type") or "").strip()
requested = (ti.get("model") or "").strip().lower()

RANK = {"haiku": 0, "sonnet": 1, "opus": 2, "fable": 3}
# Only agents with NO sanctioned escalation belong here — see the note above.
CEILING = {"Explore": "haiku"}

ceiling = CEILING.get(atype)
if ceiling is None or requested not in RANK or RANK[requested] <= RANK[ceiling]:
    sys.exit(0)

sys.stderr.write(
    "\n[model-tier] ADVISORY (not blocking) — \"%s\" requested %s; its policy ceiling is %s.\n"
    % (atype, requested, ceiling)
    + "  Read-only search does not need a stronger model; the difference is spend, not quality.\n"
      "  Re-spawn with model \"%s\" unless this search genuinely needs judgment, in which case\n"
      "  a general-purpose or specialist agent is the better fit. See CLAUDE.md Model-Tier Policy.\n\n"
    % ceiling
)
sys.exit(0)
PY

# ---------------------------------------------------------------------------------------
# Stage 2 — delegation QUALITY (advisory, never blocks).
#
# The Model-Tier Policy above governs WHICH model runs the work. This governs whether the
# delegate can actually do it. CLAUDE.md names the failure directly: "No telephone game —
# if the lossy summary would drop signal the orchestrator needs, either keep it inline or
# require structured output." A one-line delegation is how that signal gets dropped, and
# the cost lands on the parent, who then re-does the work (paying twice) or ships the
# delegate's guess.
#
# ADVISORY on purpose. "Is this prompt well-formed?" is a judgment call, and a blocking
# gate that is wrong even occasionally trains people to set the override permanently
# (Rule 42) — at which point the tier enforcement above, which shares the override, dies
# with it. A false positive here would cost more than the miss it prevents.
python3 <<'PY'
import hashlib, json, os, re, sys

try:
    data = json.loads(os.environ.get("AGENT_GUARD_JSON", ""))
except Exception:
    sys.exit(0)

ti = data.get("tool_input") or {}
atype = (ti.get("subagent_type") or "").strip()
prompt = (ti.get("prompt") or "")
if not atype or not prompt.strip():
    sys.exit(0)

# Read-only search agents are legitimately terse ("find every call site of X"), so the
# brevity heuristic would be mostly false positives there.
SEARCH_AGENTS = {"Explore", "research-coordinator"}

text = prompt.lower()

# Does the prompt say what to COME BACK WITH? Without it the delegate picks its own
# format and the parent gets prose where it needed a list, or a summary where it needed
# file:line.
DELIVERABLE = ("return", "report", "list", "output", "provide", "summar", "answer",
               "identify", "produce", "write", "give me", "respond with", "deliver",
               # Search/locate verbs are deliverable statements too — "find every call
               # site" says exactly what comes back. Omitting these flagged a perfectly
               # good Explore delegation on the first run of the tests.
               "find", "locate", "search", "enumerate", "trace", "map ", "audit", "review")
has_deliverable = any(w in text for w in DELIVERABLE)

# Does it say what DONE looks like? For implementation work this is what stops a delegate
# declaring victory on a partial change.
ACCEPTANCE = ("verify", "test", "must ", "should ", "criteria", "done when", "acceptance",
              "ensure", "confirm", "check that", "passes", "expect")
has_acceptance = any(w in text for w in ACCEPTANCE)

# Concrete anchors — a path, a symbol, a file. A delegation with none is asking the
# delegate to guess the scope the parent already knows.
# Any multi-hump CamelCase identifier counts, not a fixed suffix list: the first version
# enumerated View/Service/Manager/Tool/Tests and so missed "PlannerFrontierSelector",
# flagging a good prompt. A closed vocabulary of suffixes drifts from the codebase exactly
# the way a hand-kept keyword table does (Rule 104).
has_anchor = bool(re.search(r"[\w/]+\.(swift|py|sh|md|json|yml|yaml)\b", prompt)
                  or re.search(r"\b[A-Z][a-z0-9]+[A-Z][a-zA-Z0-9]*\b", prompt)
                  or "/" in prompt)

problems = []
if len(prompt.strip()) < 120 and atype not in SEARCH_AGENTS:
    problems.append("it is very short for a non-search agent (<120 chars)")
if not has_deliverable:
    problems.append("it never says what to RETURN (no deliverable named)")
if not has_acceptance and atype not in SEARCH_AGENTS:
    problems.append("it never says what DONE looks like (no acceptance criteria)")
if not has_anchor:
    problems.append("it names no file, path, or symbol to anchor the scope")

# Two or more signals before speaking. Any single one alone is too often a fine prompt
# that simply phrases things differently.
if len(problems) < 2:
    sys.exit(0)

# Dedupe on the PROMPT, not the session: re-spawning the same prompt after a tier block
# should not warn twice, but a different weak prompt genuinely deserves its own warning.
fp = hashlib.sha256((atype + "|" + prompt).encode()).hexdigest()[:16]
sentinel = "/tmp/.claude-delegation-warn-%s" % fp
if os.path.exists(sentinel):
    sys.exit(0)
try:
    open(sentinel, "w").close()
except OSError:
    pass

sys.stderr.write(
    "\n[delegation] ADVISORY (not blocking) — this spawn of \"%s\" may under-specify:\n" % atype
    + "".join("  - %s\n" % p for p in problems)
    + "  A delegate cannot ask a follow-up question; whatever is missing here, it will\n"
      "  invent. Cheapest fix is one more sentence naming the deliverable and the check.\n"
      "  See /brief and Documentation/Dev Guides/Core/PROMPT_AND_DELEGATION_FORMAT.md.\n"
      "  For results the orchestrator must not lose, pass a schema rather than prose.\n\n"
)
sys.exit(0)
PY
exit 0
