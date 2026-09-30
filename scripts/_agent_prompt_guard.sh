#!/usr/bin/env bash
# _agent_prompt_guard.sh — PreToolUse(Agent) hook: enforce delegation self-containedness.
#
# WHY: a subagent receives its own definition, the Agent tool's `prompt`, and the project
# CLAUDE.md — and NOTHING ELSE. Not the parent's conversation history, not the parent's
# tool results, not the parent's system prompt. A delegation that says "the file we
# discussed" or "fix the bug from earlier" therefore points at context that does not exist
# on the other side. The subagent does not error; it infers something and returns
# confident work about the wrong thing. That is the silent-failure class this guard closes.
#
# Sibling of `_agent_model_guard.sh` (same stdin-JSON-via-env pattern, same fail-open
# posture, same exit-2-to-block contract, same single-env-var override).
#
# WIRING (user applies via /update-config) — add to hooks.PreToolUse alongside the model guard:
#   { "matcher": "Agent", "hooks": [
#       { "type": "command", "command": "scripts/_agent_model_guard.sh" },
#       { "type": "command", "command": "scripts/_agent_prompt_guard.sh" } ] }
#
# Modes:
#   default                        advisory — warn on stderr, exit 0
#   AGENT_PROMPT_GUARD_STRICT=1    blocking — exit 2 on a finding
#   AGENT_PROMPT_GUARD_OK=1        off
#
# Guide: Documentation/Dev Guides/Core/PROMPT_AND_DELEGATION_FORMAT.md
set -uo pipefail

command -v python3 >/dev/null 2>&1 || exit 0   # fail-open if no python3
[ "${AGENT_PROMPT_GUARD_OK:-0}" = "1" ] && exit 0

AGENT_PROMPT_JSON="$(cat)" python3 <<'PY'
import json, os, re, sys

try:
    data = json.loads(os.environ.get("AGENT_PROMPT_JSON", ""))
except Exception:
    sys.exit(0)  # unparseable -> fail open

ti = data.get("tool_input") or {}
prompt = (ti.get("prompt") or "").strip()
atype = (ti.get("subagent_type") or "").strip()
if not prompt:
    sys.exit(0)

findings: list[str] = []

# --- 1. Parent-context references -------------------------------------------------
# Phrases that only resolve against a conversation the subagent cannot see.
DANGLING = re.compile(
    r"\b(?:as|like)\s+(?:discussed|mentioned|described|noted|above|before|earlier)\b"
    r"|\bthe\s+(?:file|bug|issue|error|test|change|plan|approach|one)\s+"
    r"(?:we|you|i)\s+(?:discussed|mentioned|found|saw|were|just)\b"
    r"|\b(?:from|in)\s+(?:earlier|before|the\s+previous\s+(?:step|message|turn))\b"
    r"|\bthat\s+(?:same|other)\s+(?:file|error|approach|one)\b"
    r"|\bcontinue\s+(?:where|from)\s+(?:we|you)\b"
    r"|\bthe\s+above\b",
    re.I,
)
for m in DANGLING.finditer(prompt):
    findings.append(f'parent-context: "{m.group(0)}" — the subagent has no parent history to resolve this against.')

# --- 2. Missing deliverable ---------------------------------------------------------
# Some statement of what to hand back. Deliberately generous: any of these counts.
DELIVERABLE = re.compile(
    r"\breturn\b|\breport\b|\boutput\b|\bwrite\s+(?:to|a|the)\b|\bproduce\b|\bemit\b"
    r"|\bgive\s+me\b|\bprovide\b|\bsummar(?:y|ise|ize)\b|\brespond\s+with\b"
    r"|\bfinal\s+message\b|\bschema\b|\blist\b|\bfor\s+each\b",
    re.I,
)
if not DELIVERABLE.search(prompt):
    findings.append("no-deliverable: nothing states what to hand back. A subagent's final text IS its return value — say what shape it should take.")

# --- 3. Missing boundary ------------------------------------------------------------
# Only checked on longer delegations; a 2-line lookup does not need a scope fence.
BOUNDARY = re.compile(
    r"\bonly\b|\bdo not\b|\bdon'?t\b|\bavoid\b|\bscope\b|\blimit(?:ed)?\s+to\b"
    r"|\bwithout\b|\bexclude\b|\brather\s+than\b|\bnot\s+(?:the|any)\b|\bstop\b"
    r"|\bread-only\b|\bno\s+\w+",
    re.I,
)
if len(prompt) > 600 and not BOUNDARY.search(prompt):
    findings.append("no-boundary: a delegation this long states no limit on scope. Say where the agent's responsibility ends and what it must not touch.")

if not findings:
    sys.exit(0)

label = atype or "subagent"
strict = os.environ.get("AGENT_PROMPT_GUARD_STRICT") == "1"
head = "BLOCKED" if strict else "ADVISORY"

sys.stderr.write(
    f"{head}: delegation to \"{label}\" may not be self-contained.\n"
    + "".join(f"  - {f}\n" for f in findings)
    + "A subagent receives ONLY its own definition, this prompt, and CLAUDE.md — no parent\n"
      "conversation, tool results, or system prompt. Inline every path, decision, and constraint.\n"
      "See Documentation/Dev Guides/Core/PROMPT_AND_DELEGATION_FORMAT.md.\n"
    + ("Override: AGENT_PROMPT_GUARD_OK=1.\n" if strict else "")
)
sys.exit(2 if strict else 0)
PY
