"""What the dev database says about the tables a ticket touches (P10), for the Tester.

Seed rows and DB checks go wrong on facts the code only hints at: a NOT NULL column with no
default, a unique constraint, a check constraint, a foreign key. Reading them from the real
dev schema (read-only, `information_schema` / `pg_constraint`) is faster for the Tester than
digging through models and migrations, and exact. Failures are never fatal: the Tester then
reads the code as before.
"""

from __future__ import annotations

import re
from typing import Any

MAX_TABLES = 14
MAX_COLUMNS = 40


def tables_in(text: str, existing: list[str]) -> list[str]:
    """Existing tables named in `text` (the diffs: models, migrations, queries)."""
    wanted: list[str] = []
    for table in existing:
        if re.search(rf"(?<![\w]){re.escape(table)}(?![\w])", text):
            wanted.append(table)
    return wanted[:MAX_TABLES]


def pick_tables(existing: list[str], texts: list[str]) -> list[str]:
    """The tables to describe: those the ticket names first, then the diffs', then the spec's,
    each source in turn (an update of an existing endpoint changes no model's table name, so
    the diffs alone name no table: DEMO-12 got no facts at all)."""
    out: list[str] = []
    for text in texts:
        for table in tables_in(text, existing):
            if table not in out:
                out.append(table)
    return out[:MAX_TABLES]


def schema_md(dbs: dict[str, Any], diff_text: str, more_texts: list[str] | None = None) -> str:
    """A short markdown section, or "" when there is nothing to say or the DB can't be read.
    `more_texts`: other texts that name tables (the ticket, the spec excerpt), most important
    first."""
    db = next(iter(dbs.values()), None)
    if db is None or not hasattr(db, "all_tables"):
        return ""
    try:
        wanted = pick_tables(db.all_tables(), [*(more_texts or [])[:1], diff_text,
                                               *(more_texts or [])[1:]])  # fmt: skip
        lines = [db.table_facts(t) for t in wanted]
    except Exception:  # noqa: BLE001 (never stops a run: the Tester reads the code instead)
        return ""
    if not lines:
        return ""
    return ("### The dev database's tables the ticket touches (read from the database)\n\n"
            "NOT NULL columns without a default must be in every seed row; respect the unique, "
            "check and foreign-key constraints.\n\n" + "\n".join(lines))
