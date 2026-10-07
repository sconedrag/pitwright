#!/usr/bin/env python3
"""_identity.py — the one place coord decides who "this session" is.

Every lock, manifest, mailbox and marker is keyed by a session id, and every module that
needs one calls `session_id()` here. Before v0.2 the resolution order was copied into six
places; copies drift, and two identity namespaces drifting apart is the root cause of the
dead-letter and unreadable-manifest bugs recorded in `agent_message.my_keys()`.

Resolution — exactly ONE id, never a set
  1. `CLAUDE_CODE_SESSION_ID` — set by Claude Code itself for hook processes AND for the
     Bash tool, so skill scripts see it too. It equals the hook payload's `session_id`,
     survives `/compact` and `--resume`, and a subagent sees its parent's value (so a
     subagent shares its parent's locks, as it did under v0.1).
  2. otherwise `TERM_SESSION_ID` — the v0.1 key. Only macOS Terminal and iTerm2 set it.
  3. otherwise `/tmp/.claude-session-<ppid>.id` — only when nothing else identifies us.

Why one id and not "every id we answer to": `TERM_SESSION_ID` is NOT unique to a session.
Every tmux pane inherits the value from the tmux server (the tab that started it), so treating it as an alias of
the current session made a live peer's lock read as our own — editable and releasable over
the peer (found in adversarial review, reproduced). Nor is v0.1 state migrated: under v0.1
every pane of a tmux server wrote ONE shared manifest recording one pid, so no check can tell
which pane a v0.1 lock belongs to (a second review reproduced a "proven" migration taking a
live pane's locks). v0.1 state is left under its key and ages out like any other.
Ownership checks are plain equality.

`CLAUDE_CODE_SESSION_ID` is observed behaviour, not a documented contract. If it disappears
the order falls through to the v0.1 behaviour.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

ENV_NATIVE = "CLAUDE_CODE_SESSION_ID"
ENV_LEGACY = "TERM_SESSION_ID"

# DELETE disallowed characters rather than substitute them. The shell hook re-derives ids
# with `tr -cd 'A-Za-z0-9_-'`; substituting here would produce a different filename for
# the same raw id, and the two would look for manifests the other never wrote.
_ID_DISALLOWED = re.compile(r"[^A-Za-z0-9_-]")


def sanitize(raw: str | None) -> str:
    return _ID_DISALLOWED.sub("", raw or "")


def native_id() -> str:
    """Claude Code's own id for this session, or "" when not running under it."""
    return sanitize(os.environ.get(ENV_NATIVE))


def legacy_id() -> str:
    """The v0.1 key. Shared by every tmux pane in a tab — never proof of identity alone."""
    return sanitize(os.environ.get(ENV_LEGACY))


def _ppid_cache(ppid: int | None = None) -> str:
    pid = ppid if ppid is not None else os.getppid()
    try:
        return sanitize(Path(f"/tmp/.claude-session-{pid}.id").read_text().strip())
    except OSError:
        return ""


def session_id(ppid: int | None = None) -> str:
    """This session's coordination id, or "" when nothing identifies it."""
    return native_id() or legacy_id() or _ppid_cache(ppid)


def is_self(other: str | None, sid: str | None = None) -> bool:
    """Does `other` name this session (or the explicitly named `sid`)? Plain equality."""
    me = sid or session_id()
    return bool(other) and bool(me) and other == me


if __name__ == "__main__":
    print(session_id())
