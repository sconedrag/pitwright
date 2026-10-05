#!/usr/bin/env python3
"""Shared helpers for the tests in this directory.

Not named test_*.py on purpose: `run_all.sh` discovers tests by that glob, so a helper
module named otherwise is imported but never executed as a test in its own right.
"""
from __future__ import annotations

import os
import subprocess
import sys


def dead_pid() -> int:
    """A pid guaranteed not to be running, on any platform.

    A hardcoded constant does not work, and the way it fails is worth stating because
    it cost a green local suite and a red CI one. `999_999` sits above macOS's default
    pid ceiling (~99998), so on a developer's Mac it is reliably dead — but Linux's
    `pid_max` defaults to 4194304, so on the `ubuntu-latest` CI runner that same
    constant can name a LIVE process. Every "dead owner" case then silently became a
    live one, and the tests that assert a session is inheritable failed only in CI,
    where the diagnosis is slowest.

    The fix is to stop asserting that a pid is dead and start ensuring it: spawn a
    trivial child, reap it, and use its pid. Dead by construction. The result is still
    verified before returning, because a reaped pid can in principle be recycled in the
    window between `wait()` and the check — rare, but this helper underpins every
    liveness test, so a flake here would look like a liveness bug.
    """
    for _ in range(20):
        proc = subprocess.Popen(
            [sys.executable, "-c", ""],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        proc.wait()
        pid = proc.pid
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return pid          # confirmed gone
        except OSError:
            continue            # recycled or not probeable — try again
    raise RuntimeError("could not obtain a confirmed-dead pid after 20 attempts")
