"""Pausing the whole pipeline (§9.11, Phase 8 1.1): the `$PIPELINE_HOME/PAUSE` file.

`pipeline pause` / an admin's "pause the pipeline" in chat writes it (who, when, why);
`unpause` removes it. While it exists no step and no agent starts: a ticket about to run its
next step says so once in its thread and waits, then carries on by itself after `unpause`
(nothing goes to Needs Human). A `stop` still ends the wait. A hand-made empty PAUSE file
works the same.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

PAUSE_FILE = "PAUSE"
POLL_S = 5.0  # how often a waiting ticket looks again (tests lower it)


def pause_info(home: Path) -> dict | None:
    """{"by", "at", "reason"} while paused (empty values for a hand-made file); None if not."""
    path = home / PAUSE_FILE
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text() or "{}")
    except (OSError, ValueError):
        data = {}
    return {k: str(data.get(k) or "") for k in ("by", "at", "reason")}


def set_pause(home: Path, by: str, at: str, reason: str = "") -> bool:
    """Pause; False if it already was."""
    if (home / PAUSE_FILE).exists():
        return False
    home.mkdir(parents=True, exist_ok=True)
    (home / PAUSE_FILE).write_text(json.dumps({"by": by, "at": at, "reason": reason}))
    return True


def clear_pause(home: Path) -> dict | None:
    """Unpause; returns what the pause was (None: it wasn't paused)."""
    info = pause_info(home)
    (home / PAUSE_FILE).unlink(missing_ok=True)
    return info


def paused_words(info: dict) -> str:
    """ "paused by Kushal Soni (demo break)" / "paused"."""
    text = f"paused by {info['by']}" if info.get("by") else "paused"
    return f"{text} ({info['reason']})" if info.get("reason") else text


def wait_while_paused(home: Path, stop: Callable[[], bool] = lambda: False,
                      poll_s: float | None = None) -> bool:  # fmt: skip
    """Block while paused. True = it waited (was paused); ends early when `stop()` is true."""
    waited = False
    while (home / PAUSE_FILE).exists() and not stop():
        waited = True
        time.sleep(POLL_S if poll_s is None else poll_s)
    return waited
