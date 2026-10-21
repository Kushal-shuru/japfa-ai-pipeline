"""Agent child processes that outlive the server (K18, Phase 8 1.4).

`codex exec` runs in its own session (so a timeout can kill its whole tree): Ctrl-C on
`pipeline serve` doesn't reach it, and it would keep editing a worktree nobody watches while
the next start re-runs the same step. Each such process group gets a pidfile in
`$PIPELINE_HOME/children/`; `kill_leftovers()` (server shutdown and startup) ends them.
Claude runs are ordinary children of the server and end with it.
"""

from __future__ import annotations

import contextlib
import os
import signal
from pathlib import Path

CHILDREN_DIR = "children"


def register(directory: Path | None, pgid: int) -> None:
    if directory is None:
        return
    directory.mkdir(parents=True, exist_ok=True)
    (directory / str(pgid)).write_text(str(os.getpid()))


def unregister(directory: Path | None, pgid: int) -> None:
    if directory is not None:
        (directory / str(pgid)).unlink(missing_ok=True)


def kill_leftovers(directory: Path) -> list[int]:
    """SIGKILL every registered process group that is still alive; remove all pidfiles.
    Returns the groups that were killed."""
    killed: list[int] = []
    if not directory.is_dir():
        return killed
    for f in directory.iterdir():
        if not f.name.isdigit():
            continue
        pgid = int(f.name)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
            killed.append(pgid)
        f.unlink(missing_ok=True)
    return killed
