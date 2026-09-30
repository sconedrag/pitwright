#!/usr/bin/env bash
# prompt-context-guard.sh — PreToolUse(Agent) hook: flag delegations that are not self-contained.
#
# WHY: a (non-fork) subagent receives its own definition, the Agent tool's `prompt`, and the
# project's CLAUDE.md — NOT the parent's conversation, tool results, or system prompt. A
# delegation that says "the file we discussed" or "fix the bug from earlier" points at context
# that does not exist on the other side. The subagent does not error; it infers something and
# returns confident work about the wrong thing. This guard catches the phrasing that causes it.
#
# Forks are exempt: `subagent_type: "fork"` inherits the parent's full context by design, so
# references to earlier conversation are legitimate there.
#
# Modes:
#   default                       advisory — warn on stderr, exit 0
#   PROMPT_CONTEXT_GUARD_STRICT=1 blocking — exit 2 on a finding
#   SPAWN_GUARDS_OK=1             off (shared with model-guard.sh)
set -uo pipefail

command -v python3 >/dev/null 2>&1 || exit 0   # fail open without python3
[ "${SPAWN_GUARDS_OK:-0}" = "1" ] && exit 0

PROMPT_GUARD_JSON="$(cat)" python3 <<'PY'
import json, os, re, sys

try:
    data = json.loads(os.environ.get("PROMPT_GUARD_JSON", ""))
except ValueError:
    sys.exit(0)  # unparseable payload -> fail open

ti = data.get("tool_input") or {}
prompt = (ti.get("prompt") or "").strip()
atype = (ti.get("subagent_type") or "").strip()
if not prompt or atype == "fork":
    sys.exit(0)

findings = []

# 1. Phrases that only resolve against a conversation the subagent cannot see.
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

# 2. Some statement of what to hand back. Deliberately generous: any of these counts.
DELIVERABLE = re.compile(
    r"\breturn\b|\breport\b|\boutput\b|\bwrite\s+(?:to|a|the)\b|\bproduce\b|\bemit\b"
    r"|\bgive\s+me\b|\bprovide\b|\bsummar(?:y|ise|ize)\b|\brespond\s+with\b"
    r"|\bfinal\s+message\b|\bschema\b|\blist\b|\bfor\s+each\b",
    re.I,
)
if not DELIVERABLE.search(prompt):
    findings.append("no-deliverable: nothing states what to hand back. A subagent's final text IS its return value — say what shape it should take.")

# 3. A scope fence — only on longer delegations; a two-line lookup does not need one.
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

strict = os.environ.get("PROMPT_CONTEXT_GUARD_STRICT") == "1"
sys.stderr.write(
    f'{"BLOCKED" if strict else "ADVISORY"}: delegation to "{atype or "subagent"}" may not be self-contained.\n'
    + "".join(f"  - {f}\n" for f in findings)
    + "A subagent receives only its own definition, this prompt, and CLAUDE.md — no parent\n"
      "conversation, tool results, or system prompt. Inline every path, decision, and constraint.\n"
    + ("Override: SPAWN_GUARDS_OK=1.\n" if strict else ""))
sys.exit(2 if strict else 0)
PY
