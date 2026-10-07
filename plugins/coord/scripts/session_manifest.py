#!/usr/bin/env python3
"""session_manifest.py — the session manifest that `lock_guard.py` and the board read.

Why this exists
---------------
`lock_guard.py` (the PreToolUse edit hook) keys locking on
`.claude/coordination/sessions/<session id>.json` and, finding none, allows the edit
with no lock taken: an unregistered session has opted itself out of the coordination layer,
for itself AND for every peer trying to see it. Relying on every session to run
`/coord:start-session` before its first edit rewards omission — a session that skips it
pays no visible cost, so skipping wins. `render_board.py` reads the same manifests to show
who is active.

Two identity systems had drifted apart: `session_registry_hook.py` writes
`current-session.json` keyed by the native session UUID, while the guard reads a manifest
keyed by `TERM_SESSION_ID`. Auto-registration therefore did not satisfy the guard. This
module bridges them (and since v0.2 both resolve through `_identity`, normally to the same
native id), so locking is on by DEFAULT and `/coord:start-session` becomes an upgrade
(declaring a domain) rather than the thing that turns coordination on at all.

Claiming nothing, on purpose
----------------------------
An auto-registered manifest sets `claimedPaths: []`. Auto-claiming a domain's globs would
let a session that merely started block peers over paths it never touched — trading a
silent failure for a noisy one. Identity and lockability come first; path ownership is
claimed explicitly, by `/coord:claim-files` or by declaring a domain.

Invoked by: scripts/session_registry_hook.py (auto-registers at session start),
scripts/lock_guard.py (touches the manifest on every locked edit),
scripts/coord_locks.py (publishes a claimed domain to the shared registry), and the CLI
`/coord:start-session` runs — `session_manifest.py declare <domain>` — to turn the
auto-registration into a declared one.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _identity  # noqa: E402

# The domain recorded before anything is known about the session's work. It is a real,
# reserved value rather than a missing key, so "nobody has said yet" is distinguishable
# from "the field was dropped" — the latter reads as corruption and gets overwritten.
UNSCOPED = "unscoped"

# Must match the documented sanitisation formula EXACTLY wherever it is re-derived — e.g. a
# shell implementation (a bash hook) doing `tr -cd 'A-Za-z0-9_-'`, i.e. DELETE the disallowed
# characters rather than substitute them. Substituting instead would derive a different
# filename from the same TERM_SESSION_ID, and such a hook would look for a manifest this
# module never wrote — the two-identity-systems bug again, one level down.
_ID_ALLOWED = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_session_id(raw: str) -> str:
    return _ID_ALLOWED.sub("", raw or "")


def session_id_from_env(pid: int | None = None) -> str:
    """This session's coordination id — see `_identity` for the resolution order."""
    return _identity.session_id(pid)


def _validated_pid(pid: int | None) -> int:
    """Record a pid only if it is alive RIGHT NOW; otherwise record none.

    A wrong pid is strictly worse than no pid, which is not obvious and is the whole
    reason this exists. Compare what `coord_locks.liveness()` returns once the
    heartbeat has aged past the staleness threshold:

        absent pid          -> unknown  -> never inheritable          (safe)
        correct, alive pid  -> alive    -> never inheritable          (protected)
        wrong, dead pid     -> DEAD     -> INHERITABLE                (a live
                                                                       session's work
                                                                       becomes adoptable)

    So a caller that passes the pid of a throwaway shell — `os.getppid()` from a script
    invoked through a shell, rather than the long-lived claude process — does not merely
    record a useless number. It converts "idle but alive, protected" into "abandoned,
    take it", which is precisely the outcome the three-state liveness verdict exists to
    prevent. Observed in practice: a registration helper recorded its subshell's pid,
    already dead by the time the file was written.

    A pid that is dead at write time will never become alive, so there is nothing to
    lose by dropping it and everything to lose by keeping it.
    """
    if pid is None:
        return 0
    try:
        pid = int(pid)
    except (TypeError, ValueError, OverflowError):
        # OverflowError is not hypothetical padding: int(float("inf")) raises it, and
        # this runs during registration. An exception here means NO manifest, which
        # means the guard treats the session as unregistered and disables locking
        # entirely — a far larger failure than the bad pid that caused it. Every
        # unusable input degrades to "no pid recorded".
        return 0
    if pid <= 0:
        return 0
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return 0          # dead on arrival — caller passed the wrong process
    except PermissionError:
        return pid        # exists, owned by another user
    except OSError:
        return 0
    return pid


def _machine_id() -> str:
    """Stable machine identity (see coord_locks.machine_id — hostname is not stable)."""
    try:
        import coord_locks
        return coord_locks.machine_id()
    except Exception:
        return socket.gethostname()


def git_toplevel(cwd: str) -> Path | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             cwd=cwd, capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def current_branch(cwd: str) -> str:
    """Feeds pre-commit check 6b (branch-ownership). That gate fails OPEN on a missing
    `branch`, so recording it is what arms the protection — and a STALE value aims it at
    the wrong branch, which is why `touch()` refreshes it rather than writing it once."""
    try:
        out = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                             cwd=cwd, capture_output=True, text=True, timeout=5)
        branch = out.stdout.strip()
        return "" if branch in ("", "HEAD") else branch
    except (OSError, subprocess.SubprocessError):
        return ""


def sessions_dir(cwd: str) -> Path | None:
    top = git_toplevel(cwd)
    return None if top is None else top / ".claude" / "coordination" / "sessions"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read(path: Path) -> dict | None:
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_atomic(path: Path, payload: dict) -> bool:
    """Write via a temp file + rename. The guard reads this path on every Edit/Write, so a
    torn half-written manifest would read as unparseable and silently disable locking —
    the failure this module exists to remove. rename(2) makes the swap atomic."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except (OSError, NameError, UnboundLocalError):
            pass
        return False


def native_id(cwd: str, manifest: dict | None = None) -> str:
    """This session's NATIVE uuid — the key the shared registry uses.

    Registry records are keyed by the native uuid. Since v0.2 manifests normally are too,
    but a manifest keyed by a fallback id (TERM_SESSION_ID) still needs the join. Without
    this join a domain declared on the manifest can never reach the registry, which is why
    `representation()` saw 0 domains across 28 records while three sessions had one.

    Two sources, in order of trust:
      1. the manifest's own `nativeSessionId`, stamped by the SessionStart hook;
      2. `current-session.json` — but ONLY when its pid agrees with the manifest's. That
         marker is written once at session start and never refreshed, so it can name a
         SUPERSEDED session (observed in practice, still holding a pre-restart id and pid).
         Trusting it blindly would publish this session's domain onto a stranger's record.
    """
    if manifest is None:
        # Load it rather than skipping the check. Accepting `None` and then only guarding
        # "if manifest" would leave an unverified path that returns the marker's id with no
        # corroboration — precisely the stale-marker hazard this function exists to close,
        # reachable by any caller that simply omitted an argument.
        directory = sessions_dir(cwd)
        sid = session_id_from_env()
        manifest = read(directory / f"{sid}.json") if (directory and sid) else None
    if manifest and manifest.get("nativeSessionId"):
        return str(manifest["nativeSessionId"])
    # Since v0.2 the manifest is normally keyed BY the native id, so the join is the key
    # itself — no marker needed, and no stale-marker hazard.
    native_env = _identity.sanitize(os.environ.get(_identity.ENV_NATIVE))
    if manifest and native_env and manifest.get("sessionId") == native_env:
        return native_env
    top = git_toplevel(cwd)
    if top is None or manifest is None:
        return ""
    try:
        marker = json.loads(
            (top / ".claude" / "coordination" / "current-session.json").read_text("utf-8"))
    except (OSError, ValueError):
        return ""
    if int(marker.get("pid", 0) or 0) != int(manifest.get("pid", 0) or 0):
        return ""
    return str(marker.get("sessionId", "") or "")


def stamp_native_id(cwd: str, native: str) -> bool:
    """Repair the manifest→registry join from a caller that KNOWS its native id.

    `ensure()` stamps `nativeSessionId`, but it only runs at SessionStart — and SessionStart
    does not re-fire when a session is resumed. Measured in practice: every manifest on one
    machine carried `nativeSessionId: None` while the sessions were running fine, because each
    had been resumed at least once. So the one hook that DOES run on every prompt has to be
    able to repair the join, or a resumed session stays unjoinable for its whole life.

    Idempotent, and it never overwrites a different id: two ids for one manifest means
    something upstream is confused, and silently picking one would bury that.
    """
    directory = sessions_dir(cwd)
    sid = session_id_from_env()
    if not native or directory is None or not sid:
        return False
    path = directory / f"{sid}.json"
    manifest = read(path)
    if manifest is None or manifest.get("nativeSessionId"):
        return False
    manifest["nativeSessionId"] = native
    return _write_atomic(path, manifest)


def _publish_domain(cwd: str, manifest: dict, domain: str) -> None:
    """Mirror a manifest domain into the shared cross-worktree registry.

    The manifest is per-worktree and gitignored, so a domain declared there is invisible to
    every peer — and the registry is the only view `representation()` can read. Publishing
    here rather than at each call site means every writer (SessionStart, /coord:start-session, the
    inference hook, a lock claim) syncs by construction instead of by remembering to.

    Best-effort: a registry hiccup must never fail the manifest write that already succeeded.
    """
    if not domain or domain == UNSCOPED:
        return
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).resolve().parent))
        import session_registry as _sr

        # Publish to the channel belonging to the repo we were HANDED, not to whatever the
        # process's cwd happens to resolve to. `_agent_channel.channel_dir()` runs `git
        # rev-parse` against the process cwd, so a caller operating on one repo while running
        # from another publishes into the WRONG shared registry. That is not hypothetical:
        # adding this publish to `set_domain` made an existing manifest test — which builds a
        # temp repo but runs from the real one — write live-looking records into the real
        # cross-worktree registry, including one that then read ALIVE and claimed a domain.
        # Deriving the channel from `cwd` makes any caller hermetic by construction instead of
        # relying on every present and future test to remember an env override.
        # Only derive when the caller has named NO channel. Either override is an explicit
        # choice and must win — deriving over `AGENT_CHANNEL_DIR` would silently redirect
        # every hermetic test that had correctly isolated itself, which is how this fix first
        # broke three passing tests.
        _restore = os.environ.get("SESSION_REGISTRY_CHANNEL")
        if not _restore and not os.environ.get("AGENT_CHANNEL_DIR"):
            _common = subprocess.run(
                ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                cwd=cwd, capture_output=True, text=True, timeout=5).stdout.strip()
            if _common:
                os.environ["SESSION_REGISTRY_CHANNEL"] = str(Path(_common) / "agent-coordination")
        try:
            _publish_domain_inner(_sr, cwd, manifest, domain)
        finally:
            if _restore is None:
                os.environ.pop("SESSION_REGISTRY_CHANNEL", None)
            else:
                os.environ["SESSION_REGISTRY_CHANNEL"] = _restore
    except Exception:
        pass


def _publish_domain_inner(_sr, cwd: str, manifest: dict, domain: str) -> None:
    try:

        key = native_id(cwd, manifest)
        if not key:
            # No usable native uuid. Observed on a RESUMED session: the SessionStart hook
            # had not re-fired, so no manifest carried `nativeSessionId` and the marker
            # still named the pre-restart session. Depending on that key would mean the sync
            # silently does nothing for exactly the sessions most likely to need it.
            #
            # The registry key only has to be stable and unique, not a particular flavour of
            # id, and `role_spawn` never parses it — so fall back to this session's
            # own coordination id (`_identity`) and self-register under it. A session present under a second key
            # is a duplicate row at worst; a session absent entirely is invisible to every
            # peer, which is strictly worse.
            key = sanitize_session_id(manifest.get("sessionId", "")) or session_id_from_env()
            if not key:
                return
            _sr.cmd_register(key, cwd, int(manifest.get("pid", 0) or 0) or None)
        _sr.cmd_domain(key, domain)
    except Exception:
        pass


def ensure(cwd: str, pid: int, session_id: str = "", *,
           native_session_id: str = "", domain: str = UNSCOPED,
           human_name: str = "") -> Path | None:
    """Create the manifest if absent; refresh liveness fields if present.

    Idempotent and deliberately non-destructive: an existing manifest written by
    `/coord:start-session` carries a declared domain, a human name, and real `claimedPaths`, and
    this must never flatten them back to defaults on the next session start.
    """
    sid = sanitize_session_id(session_id) or session_id_from_env(pid)
    if not sid:
        return None
    directory = sessions_dir(cwd)
    if directory is None:
        return None
    path = directory / f"{sid}.json"

    existing = read(path)
    if existing is not None:
        existing["pid"] = _validated_pid(pid)
        existing["lastHeartbeat"] = _now()
        branch = current_branch(cwd)
        if branch:
            existing["branch"] = branch
        if native_session_id:
            existing["nativeSessionId"] = native_session_id
        if not _write_atomic(path, existing):
            return None
        # Re-publish on every session start. A manifest that declared a domain BEFORE this
        # sync existed would otherwise stay invisible to peers forever — the backlog fixes
        # itself on the next start instead of needing a migration nobody would run.
        _publish_domain(cwd, existing, existing.get("domain", UNSCOPED))
        return path

    manifest = {
        "sessionId": sid,
        "nativeSessionId": native_session_id,
        "pid": _validated_pid(pid),
        # Host makes the pid PROBEABLE-OR-NOT checkable instead of conventional. A pid is
        # only meaningful on the machine that recorded it — os.kill(pid, 0) elsewhere
        # probes an unrelated local process and answers confidently wrong. The older
        # convention (a cross-machine manifest simply omits its pid) got the same result
        # by agreement; stamping the host means a reader can verify rather than trust it,
        # which is what liveness must have before it can authorise inheriting work.
        "host": _machine_id(),
        "branch": current_branch(cwd),
        "domain": domain,
        "description": "auto-registered at session start",
        "humanName": human_name or "unnamed session",
        "planFile": "",
        "taskIds": [],
        "activity": "session started (auto-registered)",
        "startedAt": _now(),
        "lastHeartbeat": _now(),
        "status": "active",
        # Empty ON PURPOSE — see the module docstring. Identity and lockability without
        # claiming territory the session has not touched.
        "claimedPaths": [],
        "lockedFiles": [],
        "lastEventOffset": 0,
        "autoRegistered": True,
    }
    return path if _write_atomic(path, manifest) else None


def set_domain(cwd: str, domain: str, *, session_id: str = "", pid: int | None = None,
               claimed_paths: list[str] | None = None, inferred: bool = False,
               rationale: str = "") -> bool:
    """Record a domain on an existing manifest.

    Refuses to overwrite a domain a human declared. An inference is a guess, and a guess
    that silently replaces a declaration is worse than no inference at all — it would move
    a session's claimed territory without anyone asking for it.
    """
    sid = sanitize_session_id(session_id) or session_id_from_env(pid)
    directory = sessions_dir(cwd)
    if not sid or directory is None:
        return False
    path = directory / f"{sid}.json"
    manifest = read(path)
    if manifest is None:
        return False
    if inferred and manifest.get("domain", UNSCOPED) != UNSCOPED:
        return False
    manifest["domain"] = domain
    manifest["lastHeartbeat"] = _now()
    if inferred:
        manifest["domainInferred"] = True
        manifest["domainRationale"] = rationale
        # Left unconfirmed until a human says yes; `/coord:start-session` clears it by declaring.
        manifest["domainConfirmed"] = False
    if claimed_paths is not None:
        manifest["claimedPaths"] = claimed_paths
    if not _write_atomic(path, manifest):
        return False
    _publish_domain(cwd, manifest, domain)
    return True


def touch(cwd: str, *, session_id: str = "", pid: int | None = None,
          activity: str = "") -> bool:
    """Refresh heartbeat/branch. Liveness is PID-based, so this is not what keeps a session
    alive; it keeps `branch` (check 6b) and the board's activity line honest."""
    sid = sanitize_session_id(session_id) or session_id_from_env(pid)
    directory = sessions_dir(cwd)
    if not sid or directory is None:
        return False
    path = directory / f"{sid}.json"
    manifest = read(path)
    if manifest is None:
        return False
    manifest["lastHeartbeat"] = _now()
    branch = current_branch(cwd)
    if branch:
        manifest["branch"] = branch
    if activity:
        manifest["activity"] = activity
    return _write_atomic(path, manifest)


def set_identity(cwd: str, *, human_name: str = "", description: str = "",
                 session_id: str = "", pid: int | None = None) -> bool:
    """Set the board-visible name and/or description on an existing manifest.

    Empty arguments leave the corresponding field unchanged, so a caller can update one
    without restating the other.
    """
    sid = sanitize_session_id(session_id) or session_id_from_env(pid)
    directory = sessions_dir(cwd)
    if not sid or directory is None:
        return False
    path = directory / f"{sid}.json"
    manifest = read(path)
    if manifest is None:
        return False
    if human_name:
        manifest["humanName"] = human_name.strip()
    if description:
        manifest["description"] = description.strip()
    manifest["lastHeartbeat"] = _now()
    return _write_atomic(path, manifest)


def main(argv: list[str] | None = None) -> int:
    """CLI for the declaration a session makes about itself.

        session_manifest.py declare <domain> [--name N] [--activity TEXT]
        session_manifest.py show

    Never creates a manifest: that is the SessionStart hook's job, because only it knows the
    long-lived Claude process id. A process spawned from a tool call would record its own
    short-lived parent, and the session would read as dead the moment that shell exited.
    """
    import argparse
    ap = argparse.ArgumentParser(prog="session_manifest.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("declare", help="declare domain, and optionally name and activity")
    d.add_argument("domain")
    d.add_argument("--name", default="")
    d.add_argument("--activity", default="")
    sub.add_parser("show", help="print this session's manifest")
    args = ap.parse_args(argv)

    cwd = os.getcwd()
    sid = session_id_from_env()
    directory = sessions_dir(cwd)
    path = directory / f"{sid}.json" if sid and directory is not None else None
    if path is None or read(path) is None:
        print("no manifest for this session — it is created by the plugin's SessionStart "
              "hook. Restart the session, or check that the coord plugin's hooks are enabled.",
              file=sys.stderr)
        return 1

    if args.cmd == "show":
        print(json.dumps(read(path), indent=2))
        return 0

    ok = set_domain(cwd, args.domain)
    if ok and args.name:
        ok = set_identity(cwd, human_name=args.name, description=args.activity)
    if ok and args.activity:
        ok = touch(cwd, activity=args.activity)
    if not ok:
        print("failed to update the manifest", file=sys.stderr)
        return 1
    m = read(path) or {}
    print(f"declared: domain={m.get('domain')} name={m.get('humanName', '')!r} "
          f"activity={m.get('activity', '')!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
