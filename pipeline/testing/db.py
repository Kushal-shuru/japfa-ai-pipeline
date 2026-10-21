"""The dev database during live tests (P10): the shared dev DB from the repo's own `.env`.

Guards, all here so no caller can skip them:

- `connect` refuses any host that isn't in `acceptance.allowed_db_hosts` (and any database whose
  name says prod), before a connection is opened.
- Table and column names are checked identifiers (`psycopg.sql.Identifier`), values are always
  parameters.
- `scalar` (the cases' DB checks, written by an agent) runs in a READ ONLY transaction with a
  statement timeout: Postgres itself refuses any write.
- `delete_keys` deletes rows by primary key only, in one transaction; there is no other delete,
  no TRUNCATE and no DDL anywhere.
"""

from __future__ import annotations

import contextlib
import re
import time
from dataclasses import dataclass
from typing import Any, Protocol

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")
PROD_WORDS = re.compile(r"(^|[^a-z])(prod|prd|production)([^a-z]|$)", re.I)
STATEMENT_TIMEOUT_MS = 10_000
IDLE_CHECK_S = 20  # a connection idle longer than this is tested before it is used


class DbGuardError(RuntimeError):
    """A connection or statement the test run must never make."""


@dataclass(frozen=True)
class DbParams:
    host: str
    port: int
    name: str
    user: str
    password: str
    ssl: bool = False

    def where(self) -> str:
        return f"{self.name} on {self.host}"


def check_identifier(name: str) -> str:
    if not IDENT.match(name or ""):
        raise DbGuardError(f"`{name}` isn't a plain table or column name")
    return name


def check_host(params: DbParams, allowed: list[str]) -> None:
    """Only the allowlisted dev database (and never one named prod) may be used."""
    if params.host.lower() not in {h.lower() for h in allowed}:
        raise DbGuardError(
            f"the database host {params.host} isn't in acceptance.allowed_db_hosts, so the live "
            "tests won't connect to it"
        )
    if PROD_WORDS.search(params.name) or PROD_WORDS.search(params.host):
        raise DbGuardError(f"{params.where()} looks like production; the live tests refuse it")


class DbPort(Protocol):
    def primary_key(self, table: str) -> list[str]: ...
    def columns(self, table: str) -> set[str]: ...
    def table_exists(self, table: str) -> bool: ...
    def insert(self, table: str, values: dict) -> dict: ...
    def select_keys(self, table: str, where: dict) -> list[dict]: ...
    def children(self, table: str) -> list[tuple[str, list[str], list[str]]]: ...
    def select_child_keys(self, child: str, child_cols: list[str], parent_cols: list[str],
                          parents: list[dict]) -> list[dict]: ...  # fmt: skip
    def delete_keys(self, rows: list[tuple[str, dict]]) -> int: ...
    def scalar(self, sql: str, params: list[Any]) -> Any: ...
    def rows(self, sql: str, params: list[Any], limit: int = 50) -> list[tuple]: ...
    def applied_migrations(self, names: list[str]) -> set[str]: ...
    def all_tables(self) -> list[str]: ...
    def table_facts(self, table: str) -> str: ...
    def close(self) -> None: ...


def _split(table: str) -> tuple[str, str]:
    schema, _, name = table.rpartition(".")
    return schema or "public", name


class PgDb:
    """psycopg 3. Every connection is named `application_name` (`aipl_test_<KEY>_<run>`), so the
    DB's own activity view and logs show what came from a test run."""

    def __init__(self, params: DbParams, allowed: list[str], app_name: str) -> None:
        check_host(params, allowed)
        self.params = params
        self.app_name = app_name
        self._conn: Any = self._connect()
        self._last_used = time.monotonic()
        self._pk: dict[str, list[str]] = {}
        self._cols: dict[str, set[str]] = {}

    def _connect(self) -> Any:
        import psycopg

        p = self.params
        return psycopg.connect(
            host=p.host, port=p.port, dbname=p.name, user=p.user, password=p.password,
            sslmode="require" if p.ssl else "prefer", application_name=self.app_name[:63],
            connect_timeout=15, autocommit=True,
            # TCP keepalives: a live-test run waits tens of minutes for the Tester agent between
            # opening the connection and using it, and a VPN / RDS drops idle connections
            keepalives=1, keepalives_idle=30, keepalives_interval=10, keepalives_count=3,
        )  # fmt: skip

    @property
    def conn(self) -> Any:
        """The connection, tested first when it has been idle (DEMO-10: opened before a 20-minute
        Tester run, "the connection is lost" when the cases finally ran and again at the
        cleanup). A dead one is replaced; nothing here relies on session state."""
        import psycopg

        if self._conn.closed or self._conn.broken:
            self._conn = self._connect()
        elif time.monotonic() - self._last_used > IDLE_CHECK_S:
            try:
                self._conn.execute("SELECT 1")
            except psycopg.Error:
                with contextlib.suppress(Exception):
                    self._conn.close()
                self._conn = self._connect()
        self._last_used = time.monotonic()
        return self._conn

    def close(self) -> None:
        self._conn.close()

    @staticmethod
    def _ident(table: str) -> Any:
        from psycopg import sql

        schema, name = _split(check_identifier(table))
        return sql.Identifier(schema, name)

    def table_exists(self, table: str) -> bool:
        schema, name = _split(check_identifier(table))
        row = self.conn.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
            (schema, name),
        ).fetchone()
        return row is not None

    def columns(self, table: str) -> set[str]:
        if table not in self._cols:
            schema, name = _split(check_identifier(table))
            rows = self.conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s", (schema, name),
            ).fetchall()  # fmt: skip
            self._cols[table] = {r[0] for r in rows}
        return self._cols[table]

    def primary_key(self, table: str) -> list[str]:
        if table not in self._pk:
            schema, name = _split(check_identifier(table))
            rows = self.conn.execute(
                "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid "
                "AND a.attnum = ANY(i.indkey) WHERE i.indrelid = %s::regclass AND i.indisprimary "
                "ORDER BY array_position(i.indkey, a.attnum)", (f'"{schema}"."{name}"',),
            ).fetchall()  # fmt: skip
            self._pk[table] = [r[0] for r in rows]
        return self._pk[table]

    def insert(self, table: str, values: dict) -> dict:
        from psycopg import sql
        from psycopg.rows import dict_row
        from psycopg.types.json import Jsonb

        cols = [check_identifier(c) for c in values]
        unknown = sorted(set(cols) - self.columns(table))
        if unknown:
            raise DbGuardError(f"{table} has no column {', '.join(unknown)}")
        q = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
            self._ident(table), sql.SQL(", ").join(map(sql.Identifier, cols)),
            sql.SQL(", ").join(sql.Placeholder() * len(cols)),
        )  # fmt: skip
        params = [Jsonb(v) if isinstance(v, dict | list) else v for v in values.values()]
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(q, params)
            row = cur.fetchone()
        return dict(row or {})

    def _keys_where(self, table: str, where: dict) -> list[dict]:
        from psycopg import sql
        from psycopg.rows import dict_row

        pk = self.primary_key(table)
        if not pk:
            raise DbGuardError(f"{table} has no primary key, so its rows can't be cleaned up")
        cols = [check_identifier(c) for c in where]
        cond = sql.SQL(" AND ").join(sql.SQL("{} = %s").format(sql.Identifier(c)) for c in cols)
        q = sql.SQL("SELECT {} FROM {} WHERE {}").format(
            sql.SQL(", ").join(map(sql.Identifier, pk)), self._ident(table), cond)
        with self.conn.cursor(row_factory=dict_row) as cur:
            cur.execute(q, list(where.values()))
            return [dict(r) for r in cur.fetchall()]

    def select_keys(self, table: str, where: dict) -> list[dict]:
        if not where:
            raise DbGuardError("a row lookup needs at least one condition")
        return self._keys_where(table, where)

    def children(self, table: str) -> list[tuple[str, list[str], list[str]]]:
        """Tables with a foreign key to `table`: (child table, its columns, the parent's)."""
        schema, name = _split(check_identifier(table))
        rows = self.conn.execute(
            "SELECT kcu.table_schema || '.' || kcu.table_name, kcu.column_name, ccu.column_name, "
            "tc.constraint_name FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu ON tc.constraint_name = "
            "kcu.constraint_name AND tc.table_schema = kcu.table_schema "
            "JOIN information_schema.constraint_column_usage ccu ON ccu.constraint_name = "
            "tc.constraint_name AND ccu.table_schema = tc.table_schema "
            "WHERE tc.constraint_type = 'FOREIGN KEY' AND ccu.table_schema = %s "
            "AND ccu.table_name = %s ORDER BY tc.constraint_name, kcu.ordinal_position",
            (schema, name),
        ).fetchall()  # fmt: skip
        grouped: dict[tuple[str, str], tuple[list[str], list[str]]] = {}
        for child, ccol, pcol, constraint in rows:
            cols = grouped.setdefault((child, constraint), ([], []))
            cols[0].append(ccol)
            cols[1].append(pcol)
        return [(child.removeprefix("public."), c, p) for (child, _), (c, p) in grouped.items()]

    def select_child_keys(self, child: str, child_cols: list[str], parent_cols: list[str],
                          parents: list[dict]) -> list[dict]:  # fmt: skip
        out: list[dict] = []
        for parent in parents:
            if not all(c in parent for c in parent_cols):
                continue
            where = {cc: parent[pc] for cc, pc in zip(child_cols, parent_cols, strict=True)}
            out += self._keys_where(child, where)
        return out

    def delete_keys(self, rows: list[tuple[str, dict]]) -> int:
        """Delete exactly these rows (by primary key), in this order, in one transaction."""
        from psycopg import sql

        deleted = 0
        with self.conn.transaction():
            for table, key in rows:
                pk = self.primary_key(table)
                if not pk or set(key) != set(pk):
                    raise DbGuardError(f"refusing to delete from {table} without its full "
                                       "primary key")  # fmt: skip
                cond = sql.SQL(" AND ").join(sql.SQL("{} = %s").format(sql.Identifier(c))
                                             for c in pk)  # fmt: skip
                q = sql.SQL("DELETE FROM {} WHERE {}").format(self._ident(table), cond)
                deleted += self.conn.execute(q, [key[c] for c in pk]).rowcount
        return deleted

    def scalar(self, sql_text: str, params: list[Any]) -> Any:
        """A check query, read-only: the first column of the first row."""
        if not re.match(r"^\s*(select|with)\b", sql_text, re.I) or ";" in sql_text.strip()[:-1]:
            raise DbGuardError("a DB check must be one SELECT query")
        with self.conn.transaction():
            self.conn.execute("SET TRANSACTION READ ONLY")
            self.conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
            row = self.conn.execute(sql_text.rstrip().rstrip(";"), params).fetchone()  # type: ignore[arg-type]
        return None if row is None else row[0]

    def rows(self, sql_text: str, params: list[Any], limit: int = 50) -> list[tuple]:
        """A read-only query's first `limit` rows (the data facts for the Tester)."""
        if not re.match(r"^\s*(select|with)\b", sql_text, re.I) or ";" in sql_text.strip()[:-1]:
            raise DbGuardError("a read query must be one SELECT query")
        with self.conn.transaction():
            self.conn.execute("SET TRANSACTION READ ONLY")
            self.conn.execute(f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
            cur = self.conn.execute(sql_text.rstrip().rstrip(";"), params)  # type: ignore[arg-type]
            return cur.fetchmany(limit)

    def all_tables(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
            "AND table_type = 'BASE TABLE' ORDER BY table_name").fetchall()  # fmt: skip
        return [r[0] for r in rows]

    def table_facts(self, table: str) -> str:
        """One markdown bullet: the columns (type, NOT NULL, default) and the constraints."""
        schema, name = _split(check_identifier(table))
        cols = self.conn.execute(
            "SELECT column_name, data_type, is_nullable, column_default FROM "
            "information_schema.columns WHERE table_schema = %s AND table_name = %s "
            "ORDER BY ordinal_position", (schema, name)).fetchall()  # fmt: skip
        parts = []
        for col, typ, nullable, default in cols[:40]:
            bits = [f"{col} {typ}"]
            if nullable == "NO":
                bits.append("NOT NULL")
            if default is not None:
                bits.append(f"default {str(default)[:40]}")
            parts.append(" ".join(bits))
        more = f" (+{len(cols) - 40} more)" if len(cols) > 40 else ""
        cons = self.conn.execute(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = %s::regclass ORDER BY contype, conname",
            (f'"{schema}"."{name}"',)).fetchall()  # fmt: skip
        idx = self.conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = %s "
            "AND indexdef LIKE 'CREATE UNIQUE%%'", (schema, name)).fetchall()  # fmt: skip
        known = {c[1] for c in cons}
        rules = [f"{c[0]}: {c[1]}" for c in cons]
        for iname, idef in idx:
            tail = idef.split(" USING ", 1)[-1]
            if tail not in known and not iname.endswith("_pkey"):
                rules.append(f"{iname}: {tail}")
        return (f"- `{table}`: " + "; ".join(parts) + more
                + (" — constraints: " + "; ".join(rules) if rules else ""))

    def applied_migrations(self, names: list[str]) -> set[str]:
        if not names:
            return set()
        if not self.table_exists("SequelizeMeta"):
            return set()
        rows = self.conn.execute('SELECT name FROM "SequelizeMeta" WHERE name = ANY(%s)',
                                 (names,)).fetchall()  # fmt: skip
        return {r[0] for r in rows}


def params_from_env(env: dict[str, str], names: Any) -> DbParams:
    """`stack.db` (the variable names) + the env_from values → DbParams."""
    missing = [n for n in (names.host, names.port, names.name, names.user, names.password)
               if not env.get(n)]  # fmt: skip
    if missing:
        raise KeyError(", ".join(missing))
    return DbParams(
        host=env[names.host].strip('"'), port=int(env[names.port]), name=env[names.name],
        user=env[names.user], password=env[names.password],
        ssl=str(env.get(names.ssl or "", "")).lower() == "true",
    )  # fmt: skip
