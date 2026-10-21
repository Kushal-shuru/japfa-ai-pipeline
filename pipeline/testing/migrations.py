"""A ticket's migrations and the shared dev DB (P10).

The live tests use the team's dev database, so the pipeline never applies a ticket's migration
(D123): it would change the schema for everyone. Instead:

- `ticket_migrations`: the migration files the ticket's branch adds or changes (vs the default
  branch), and the tables they create or alter (`createTable('x'`, `addColumn('x'` …).
- `pending`: those not in the dev DB's `SequelizeMeta` yet (sequelize-cli's record of applied
  migrations, one row per file name; read-only).
- Cases touching those tables wait (*blocked*) until a person applies the migration on dev
  (`pipeline migrate <KEY>` in a terminal, or by hand); the poller then resumes the ticket.
- A migration that only creates tables is satisfied when those tables already exist on dev
  (`satisfied_by_tables`): the name check alone would wait for a migration that has nothing to do,
  and `pipeline migrate` refuses to run it.
- A migration file that changed **after** it was found applied (a review fix edited it) can't be
  trusted: the dev DB has the old version. `changed_after_applied` finds those, from the content
  hashes recorded when they were first seen applied.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from pipeline.analysis.devops_facts import is_seeder

_TABLE_CALL = re.compile(
    r"\b(?:createTable|dropTable|renameTable|addColumn|removeColumn|changeColumn|renameColumn"
    r"|addIndex|removeIndex|addConstraint|removeConstraint)\s*\(\s*"
    r"(?:\{\s*tableName\s*:\s*)?['\"`]([A-Za-z_][\w.]*)['\"`]"
)
_RAW_TABLE = re.compile(
    r"\b(?:CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?|ALTER\s+TABLE|DROP\s+TABLE(?:\s+IF\s+EXISTS)?)"
    r"\s+[\"`]?(?:public\.)?[\"`]?([A-Za-z_]\w*)", re.I,
)  # fmt: skip
_CREATE_CALL = re.compile(
    r"\bcreateTable\s*\(\s*(?:\{\s*tableName\s*:\s*)?['\"`]([A-Za-z_][\w.]*)['\"`]"
)
NOTE = "migrations"  # ticket_notes: {file name: content hash} for files first seen applied


@dataclass
class Migration:
    repo: str
    path: str  # relative to the worktree
    tables: list[str] = field(default_factory=list)
    digest: str = ""
    creates: list[str] = field(default_factory=list)  # set when it ONLY creates these tables

    @property
    def name(self) -> str:
        """What `SequelizeMeta.name` holds: the file name."""
        return PurePosixPath(self.path).name


def migration_tables(text: str) -> list[str]:
    """The tables a migration creates, changes or drops, in order of first mention."""
    found = [m.group(1).split(".")[-1] for m in _TABLE_CALL.finditer(text)]
    found += [m.group(1) for m in _RAW_TABLE.finditer(text)]
    return list(dict.fromkeys(found))


def created_tables_only(text: str) -> list[str]:
    """The tables a migration creates when creating tables is all it does (indexes and constraints
    on those same tables are fine); empty when it alters or drops anything else. Sequelize's
    `createTable` is `CREATE TABLE IF NOT EXISTS`, so such a migration changes nothing when the
    tables are already there, e.g. the team applied them under other file names (DEMO-10)."""
    created = [m.group(1).split(".")[-1] for m in _CREATE_CALL.finditer(text)]
    tables = migration_tables(text)
    return list(dict.fromkeys(created)) if created and set(tables) <= set(created) else []


def is_migration(path: str, migrations_dir: str | None) -> bool:
    p = PurePosixPath(path)
    if p.suffix not in (".js", ".cjs", ".mjs", ".ts", ".sql"):
        return False
    if migrations_dir:
        return path.startswith(migrations_dir.rstrip("/") + "/") and not is_seeder(path)
    return "migrations" in p.parts and not is_seeder(path)


def ticket_migrations(repo: str, worktree: Path, changed: list[str],
                      migrations_dir: str | None) -> list[Migration]:  # fmt: skip
    """The migrations among `changed` (paths vs the default branch) that still exist."""
    out: list[Migration] = []
    for rel in sorted(changed):
        if not is_migration(rel, migrations_dir):
            continue
        file = worktree / rel
        if not file.is_file():
            continue
        text = file.read_text(errors="replace")
        out.append(Migration(repo=repo, path=rel, tables=migration_tables(text),
                             creates=created_tables_only(text),
                             digest=hashlib.sha256(text.encode()).hexdigest()[:16]))  # fmt: skip
    return out


def changed_after_applied(migrations: list[Migration], applied: set[str],
                          seen: dict[str, str]) -> list[Migration]:  # fmt: skip
    """Applied migrations whose file differs from the content seen when they were applied."""
    return [m for m in migrations
            if m.name in applied and m.name in seen and seen[m.name] != m.digest]  # fmt: skip


def remember_applied(migrations: list[Migration], applied: set[str],
                     seen: dict[str, str]) -> dict[str, str]:  # fmt: skip
    """`seen` + the content of migrations applied for the first time."""
    out = dict(seen)
    for m in migrations:
        if m.name in applied and m.name not in out:
            out[m.name] = m.digest
    return out


def satisfied_by_tables(migrations: list[Migration], existing: set[str]) -> set[str]:
    """Names of create-table-only migrations whose tables all exist already: nothing to apply."""
    return {m.name for m in migrations if m.creates and set(m.creates) <= existing}


def blocked_tables(pending: list[Migration]) -> set[str]:
    return {t for m in pending for t in m.tables}
