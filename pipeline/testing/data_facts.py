"""What the dev database already holds for the run's partner (P10), for the Tester.

The Tester used to find out by failing: a real partner has configs for the current month, so
seeding another active one hit the database's overlap constraint, and a channel name copied from
the ticket wasn't a valid one on dev. A few read-only queries say it up front: which periods are
taken, and which values the status-like columns really hold. Structure only: dates, counts and
the values of low-cardinality code columns; no names, e-mails or free text. Never fatal: with no
facts the Tester works as before.
"""

from __future__ import annotations

import re
from typing import Any

MAX_TABLES = 8
MAX_ENUM_COLUMNS = 6
MAX_VALUES = 12
PERIODS = (("valid_from", "valid_to"), ("start_date", "end_date"), ("from_date", "to_date"),
           ("effective_from", "effective_to"), ("start_at", "end_at"))  # fmt: skip
_ENUM_NAME = re.compile(r"(status|type|code|channel|level|category|kind|mode|state|role|class|"
                        r"group)$", re.I)
_PRIVATE = re.compile(r"(name|mail|phone|address|token|secret|password|note|comment|desc)", re.I)
_TEXT = {"text", "character varying", "character", "USER-DEFINED"}


def _q(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError(name)
    return f'"{name}"'


def _month(value: Any) -> str:
    return str(value)[:7]


def table_data_md(db: Any, table: str, partner: str) -> str:
    cols = dict(db.rows("SELECT column_name, data_type FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = %s", [table], 400))
    if not cols:
        return ""
    where, params = ("WHERE partner_id = %s", [partner]) if "partner_id" in cols and partner \
        else ("", [])  # fmt: skip
    t = _q(table)
    bits: list[str] = []
    if where:
        (count,) = db.rows(f"SELECT count(*) FROM {t} {where}", params, 1)[0]
        bits.append(f"{count} row(s) for this partner")
    for a, b in PERIODS:
        if a in cols and b in cols and where:
            extra = [c for c in cols if re.search(r"(^|_)status$", c)][:1]
            sel = ", ".join([_q(a), _q(b), *map(_q, extra)])
            rows = db.rows(f"SELECT {sel} FROM {t} {where} ORDER BY {_q(a)} LIMIT 30", params, 30)
            if rows:
                spans = "; ".join(
                    f"{_month(r[0])}..{_month(r[1])}" + (f" ({r[2]})" if extra else "")
                    for r in rows)  # fmt: skip
                bits.append(f"periods taken (month level): {spans}")
            break
    enum = [c for c, typ in cols.items() if typ in _TEXT and _ENUM_NAME.search(c)
            and not _PRIVATE.search(c)][:MAX_ENUM_COLUMNS]  # fmt: skip
    for col in enum:
        vals = [r[0] for r in db.rows(f"SELECT DISTINCT {_q(col)} FROM {t} {where} "
                                      f"LIMIT {MAX_VALUES + 1}", params, MAX_VALUES + 1)
                if r[0] is not None]  # fmt: skip
        if vals and len(vals) <= MAX_VALUES:
            bits.append(f"`{col}` values: " + ", ".join(f"'{str(v)[:40]}'" for v in sorted(vals)))
    return f"- `{table}`: " + " — ".join(bits) if bits else ""


def data_md(dbs: dict[str, Any], tables: list[str], partner: str) -> str:
    """A markdown section for the Tester, or "" (nothing to say, the DB can't be read)."""
    db = next(iter(dbs.values()), None)
    if db is None or not hasattr(db, "rows") or not tables:
        return ""
    lines: list[str] = []
    for table in tables[:MAX_TABLES]:
        try:
            line = table_data_md(db, table, partner)
        except Exception:  # noqa: BLE001 (never stops a run)
            line = ""
        if line:
            lines.append(line)
    if not lines:
        return ""
    return ("### What the dev database already holds (read from the database)\n\n"
            "The run's partner may have real rows. Pick periods and values that don't collide with "
            "them, and take reference values (a channel, a type) from what is listed here or "
            "with a `lookup`, not from the ticket's examples.\n\n" + "\n".join(lines))
