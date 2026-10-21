"""SQLite checkpointer for ticket threads (same `pipeline.db` as the tracking tables, §8)."""

from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel

import pipeline.schemas as schemas


def _state_models() -> list[tuple[str, str]]:
    """Every pydantic model in `pipeline.schemas`: allowed to be restored from checkpoints."""
    return [
        (schemas.__name__, name)
        for name, obj in vars(schemas).items()
        if inspect.isclass(obj)
        and issubclass(obj, BaseModel)
        and obj.__module__ == schemas.__name__
    ]


def make_checkpointer(db_path: Path | str | None) -> SqliteSaver:
    """`db_path=None` → in-memory (tests, dry-run)."""
    target = ":memory:" if db_path is None else str(db_path)
    conn = sqlite3.connect(target, check_same_thread=False)
    if db_path is not None:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
    saver = SqliteSaver(conn, serde=JsonPlusSerializer(allowed_msgpack_modules=_state_models()))
    saver.setup()
    return saver
