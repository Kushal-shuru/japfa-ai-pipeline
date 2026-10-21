"""Runs the live acceptance cases and cleans up after them (P10).

Per case: seed its rows → its HTTP calls (with the identity's auth, against the ticket's local
apps) → its read-only DB checks. Each step's request, response and checks are kept as evidence
(credentials masked). A case is

- **passed**: every expectation held;
- **failed**: the code did something else (back to the coder);
- **blocked**: it can't be decided here: a table its ticket's migration adds isn't on the dev DB
  yet, or an external call had no fixture (never green on a made-up answer);
- **invalid**: the case itself is wrong (a seed for a missing column, a write to a row it didn't
  create …): back to the tester, not the coder.

**Cleanup: only what the run created.** The DB is the team's shared dev database, so nothing is
ever deleted by time window or by "everything new". The run's ledger (`ledger.json` in the
ticket's `.stack/`, written to disk after every insert, before the next step) holds:

1. every row a case seeded (its primary key, from `INSERT … RETURNING`);
2. every row a call created that the case tracks (`track`: looked up by the values the case
   sent or the ids the response returned);
3. rows under the run's **own test partner** (`{{run.partner_id}}`, a fresh UUID per run) in the
   tables the cases touch: everything the API creates for that partner;
4. rows that reference the above through a foreign key (children), found from the schema.

They're deleted by primary key, children first, in one transaction; then the run checks that
nothing is left. A failure keeps the ledger, so `pipeline acceptance <KEY> --cleanup` (or the next
run) finishes the job.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from pipeline.logging import mask_text
from pipeline.schemas import AcceptanceCase, AcceptanceStep, CaseResult
from pipeline.testing.cases import OWN_REF, WRITE_METHODS
from pipeline.testing.db import DbGuardError, DbPort
from pipeline.testing.envfile import render_value, sql_params

MAX_BODY_CHARS = 4000
SECRET_HEADERS = ("cookie", "authorization", "x-client-secret", "x-api-key")
CHILD_DEPTH = 3


def jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    return value


def get_path(data: Any, path: str) -> Any:
    """ "data.items[0].id" / "data.items.0.id" → the value, or KeyError."""
    value = data
    for part in re.findall(r"[^.\[\]]+", path or ""):
        if isinstance(value, list) and part.isdigit():
            value = value[int(part)]
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise KeyError(path)
    return value


def _same(actual: Any, expected: Any) -> bool:
    if actual == expected:
        return True
    try:
        return float(actual) == float(expected)
    except (TypeError, ValueError):
        return str(actual) == str(expected)


def json_problems(body: Any, checks: list[dict]) -> list[str]:
    out: list[str] = []
    for c in checks:
        path = str(c.get("path") or "")
        try:
            value = get_path(body, path)
        except (KeyError, IndexError, TypeError):
            if c.get("exists") is False:
                continue
            out.append(f"the response has no `{path}`")
            continue
        if "equals" in c and not _same(value, c["equals"]):
            out.append(f"`{path}` is {json.dumps(jsonable(value))[:200]}, expected "
                       f"{json.dumps(c['equals'])[:200]}")  # fmt: skip
        if "contains" in c and str(c["contains"]) not in json.dumps(jsonable(value)):
            out.append(f"`{path}` doesn't contain {json.dumps(c['contains'])}")
        if c.get("exists") is False:
            out.append(f"`{path}` should not be in the response")
        kinds = {"string": str, "number": (int, float), "integer": int, "boolean": bool,
                 "array": list, "object": dict, "null": type(None)}  # fmt: skip
        if "type" in c and not isinstance(value, kinds.get(str(c["type"]), object)):
            out.append(f"`{path}` is not a {c['type']}")
        if "length" in c and (not isinstance(value, list | str) or len(value) != c["length"]):
            out.append(f"`{path}` doesn't have {c['length']} items")
    return out


def masked_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: ("***" if k.lower() in SECRET_HEADERS else v) for k, v in headers.items()}


# ---------- the ledger ----------


@dataclass
class Ledger:
    """Rows a run created (or found under its partner), on disk after every change."""

    path: Path
    partner_id: str = ""
    # the partner is new to this run (random): everything under it is the run's. False = a real
    # partner (`acceptance.run_values`): never swept, only seeded / tracked rows are deleted
    partner_owned: bool = True
    # the run's own partner (`run.own_partner`, `<identity>!own`): always new to this run, so
    # everything under it is swept whether or not the main partner is a real one
    own_partner: str = ""
    entries: list[dict] = field(default_factory=list)  # {repo, table, key, depth, source}
    tables: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> Ledger:
        data = json.loads(path.read_text()) if path.is_file() else {}
        return cls(path=path, partner_id=data.get("partner_id", ""),
                   partner_owned=bool(data.get("partner_owned", True)),
                   own_partner=data.get("own_partner", ""),
                   entries=list(data.get("entries") or []),
                   tables=list(data.get("tables") or []))  # fmt: skip

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"partner_id": self.partner_id, "partner_owned": self.partner_owned,
                "own_partner": self.own_partner, "tables": self.tables,
                "entries": self.entries}
        self.path.write_text(json.dumps(data, indent=1, default=str))

    def add(self, repo: str, table: str, key: dict, source: str, depth: int = 0) -> bool:
        key = jsonable(key)
        if any(e["table"] == table and e["key"] == key and e["repo"] == repo
               for e in self.entries):  # fmt: skip
            return False
        self.entries.append({"repo": repo, "table": table, "key": key, "depth": depth,
                             "source": source})  # fmt: skip
        if table not in self.tables:
            self.tables.append(table)
        self.save()
        return True

    def swept_partners(self) -> list[str]:
        """The partners whose rows in the touched tables the cleanup deletes: the run's partner
        when it is new, and the run's own partner."""
        out = [self.partner_id] if self.partner_id and self.partner_owned else []
        return [*out, *([self.own_partner] if self.own_partner else [])]

    def note_tables(self, tables: list[str]) -> None:
        new = [t for t in tables if t not in self.tables]
        if new:
            self.tables += new
            self.save()


# ---------- running ----------


@dataclass
class Target:
    """Where the calls go: the stack's local apps (`url`, `prefix`), the DB per repo, the
    identities and the fixture server's request log."""

    url: Callable[[str], str]
    prefix: Callable[[str], str]
    db_for: Callable[[str], tuple[str, DbPort] | None]  # case repo → (db repo, db)
    headers: Callable[[str, str], dict[str, str]]
    misses: Callable[[int], list[dict]] = lambda _since: []
    request_count: Callable[[], int] = lambda: 0
    alive: Callable[[str], bool] = lambda _repo: True


class Executor:
    def __init__(self, target: Target, variables: dict[str, Any], ledger: Ledger,
                 blocked_tables: dict[str, str] | None = None,
                 blocked_externals: dict[str, str] | None = None,
                 http: httpx.Client | None = None, timeout_s: float = 30,
                 dbs: dict[str, DbPort] | None = None) -> None:  # fmt: skip
        self.target = target
        self.dbs = dbs  # given = every case's rows are deleted right after it (cases are isolated)
        self.cleaned: dict[str, Any] = {"inserted": 0, "deleted": 0, "left": 0, "tables": {},
                                        "error": None}  # fmt: skip
        self.variables = variables
        self.ledger = ledger
        self.blocked_tables = blocked_tables or {}  # table → why (the pending migration)
        self.blocked_externals = blocked_externals or {}
        self.http = http or httpx.Client(timeout=timeout_s)
        self.timeout_s = timeout_s

    def run(self, cases: list[AcceptanceCase]) -> list[CaseResult]:
        out = []
        for case in cases:
            start = len(self.ledger.entries)
            out.append(self.run_case(case))
            self._clean_case(start)
        return out

    def _clean_case(self, start: int) -> None:
        """Delete the rows this case created (ledger entries from `start`), so the next case starts
        from the dev DB as it was: one case's data can't make another fail (DEMO-10: 409s).
        Same deletes as the final cleanup (primary key, children first); a failure keeps the
        rows in the ledger for the final cleanup to retry."""
        if not self.dbs or len(self.ledger.entries) <= start:
            return
        sub = Ledger(path=self.ledger.path.with_name("case-ledger.json"),
                     partner_id=self.ledger.partner_id, partner_owned=self.ledger.partner_owned,
                     own_partner=self.ledger.own_partner,
                     entries=list(self.ledger.entries[start:]),
                     tables=list(self.ledger.tables))  # fmt: skip
        try:
            report = cleanup(sub, self.dbs)
        finally:
            sub.path.unlink(missing_ok=True)
        if report.get("error") or report.get("left"):
            self.cleaned["error"] = report.get("error") or self.cleaned["error"]
            return
        self.cleaned["inserted"] += len(self.ledger.entries) - start
        self.cleaned["deleted"] += report["deleted"]
        for table, n in report["tables"].items():
            self.cleaned["tables"][table] = self.cleaned["tables"].get(table, 0) + n
        del self.ledger.entries[start:]
        self.ledger.save()

    def _blocked(self, case: AcceptanceCase) -> str | None:
        tables = set(case.tables) | {s.table for s in case.seed}
        tables |= {str(t.get("table")) for st in case.steps for t in st.track}
        text = " ".join(c.sql for c in case.db).lower()
        for table, why in self.blocked_tables.items():
            if table in tables or re.search(rf"\b{re.escape(table.lower())}\b", text):
                return why
        for name in case.externals:
            if name in self.blocked_externals:
                return self.blocked_externals[name]
        return None

    def run_case(self, case: AcceptanceCase) -> CaseResult:
        base = {"case_id": case.id, "criterion": case.criterion, "repo": case.repo,
                "title": case.title}  # fmt: skip
        why = self._blocked(case)
        if why:
            return CaseResult(**base, status="blocked", reason=why)
        variables = {**self.variables, "seed": {}, "saved": {}, "lookup": {}}
        evidence: list[dict] = []
        db_pair = self.target.db_for(case.repo)
        self.ledger.note_tables(sorted(set(case.tables) | {s.table for s in case.seed}))
        # 0. lookups: data the dev DB must already have (a valid channel, a master row). Missing
        # data is the environment's, never the code's: the case is blocked, with what's missing
        for look in case.lookups:
            if db_pair is None:
                return CaseResult(**base, status="invalid", reason="a lookup, but no database "
                                  "is configured")  # fmt: skip
            try:
                sql, params = sql_params(look.sql, variables)
                value = db_pair[1].scalar(sql, params)
            except KeyError as err:
                return CaseResult(**base, status="invalid",
                                  reason=f"lookup {look.name}: unknown {{{{{err.args[0]}}}}}")
            except Exception as err:  # a wrong query: the case is wrong
                return CaseResult(**base, status="invalid", reason=f"lookup {look.name}: "
                                  f"{str(err).splitlines()[0][:300]}")  # fmt: skip
            evidence.append({"lookup": look.name, "value": jsonable(value)})
            if value is None:
                missing = look.why or f"`{look.sql[:120]}` found nothing"
                return CaseResult(**base, status="blocked", evidence=evidence,
                                  reason=f"the dev database has no data for this case: {missing}")
            variables["lookup"][look.name] = jsonable(value)
        # 1. seed
        for row in case.seed:
            if db_pair is None:
                return CaseResult(**base, status="invalid", reason="this case seeds rows but no "
                                  "repo under test has a database configured")  # fmt: skip
            db_repo, db = db_pair
            try:
                values = render_value(row.values, variables)
                if "partner_id" in db.columns(row.table) and "partner_id" not in values:
                    values["partner_id"] = self.variables["run"]["partner_id"]
                inserted = db.insert(row.table, values)
            except (KeyError, DbGuardError) as err:
                return CaseResult(**base, status="invalid", evidence=evidence,
                                  reason=f"seeding {row.table}: {err}")  # fmt: skip
            except Exception as err:  # psycopg errors: a wrong seed, not the code under test
                return CaseResult(**base, status="invalid", evidence=evidence,
                                  reason=f"seeding {row.table}: {str(err).splitlines()[0][:300]}")
            pk = db.primary_key(row.table)
            self.ledger.add(db_repo, row.table, {c: inserted.get(c) for c in pk}, "seed")
            if row.alias:
                variables["seed"][row.alias] = jsonable(inserted)
            evidence.append({"seed": row.table, "alias": row.alias,
                             "key": {c: jsonable(inserted.get(c)) for c in pk}})  # fmt: skip
        # 2. calls
        for i, step in enumerate(case.steps, 1):
            outcome = self._step(case, i, step, variables, evidence, db_pair)
            if outcome is not None:
                status, reason = outcome
                return CaseResult(**base, status=status, reason=reason, evidence=evidence)
        # 3. DB checks
        for check in case.db:
            if db_pair is None:
                return CaseResult(**base, status="invalid", evidence=evidence,
                                  reason="a DB check, but no database is configured")  # fmt: skip
            problem = self._db_check(db_pair[1], check, variables, evidence)
            if problem:
                status = "invalid" if problem.startswith("invalid:") else "failed"
                return CaseResult(**base, status=status, evidence=evidence,
                                  reason=problem.removeprefix("invalid: "))  # fmt: skip
        return CaseResult(**base, status="passed", evidence=evidence)

    def _step(self, case: AcceptanceCase, n: int, step: AcceptanceStep, variables: dict,
              evidence: list[dict], db_pair: Any) -> tuple[str, str] | None:  # fmt: skip
        """None = fine; else (status, reason)."""
        at = f"step {n} ({step.method} {step.path})"
        if step.method in WRITE_METHODS and not OWN_REF.search(step.path):
            return "invalid", f"{at}: a {step.method} may only target a row this case created"
        try:
            path = render_value(step.path, variables)
            query = render_value(step.query, variables)
            body = render_value(step.body, variables)
            extra = render_value(step.headers, variables)
        except KeyError as err:
            return "invalid", f"{at}: unknown template {{{{{err.args[0]}}}}}"
        try:
            headers = {**self.target.headers(step.repo, step.identity), **extra}
        except (KeyError, RuntimeError, OSError) as err:
            return "invalid", f"{at}: identity `{step.identity}`: {err}"
        url = self.target.url(step.repo) + self.target.prefix(step.repo) + str(path)
        before = self.target.request_count()
        record: dict[str, Any] = {"step": n, "request": {
            "method": step.method, "repo": step.repo, "path": path, "query": query,
            "headers": masked_headers(headers), "body": body, "as": step.identity}}  # fmt: skip
        started = time.monotonic()
        try:
            resp = self.http.request(step.method, url, params=query or None,
                                     json=body if body is not None else None,
                                     headers=headers)  # fmt: skip
        except httpx.HTTPError as err:
            record["error"] = str(err)[:300]
            evidence.append(mask_text(record))
            alive = self.target.alive(step.repo)
            return "failed", (f"{at}: no answer from {step.repo} ({err})"
                              + ("" if alive else f"; {step.repo} has crashed"))  # fmt: skip
        try:
            data: Any = resp.json()
        except ValueError:
            data = resp.text
        record["response"] = {"status": resp.status_code, "ms": int((time.monotonic() - started)
                                                                    * 1000),
                              "body": _short(data)}  # fmt: skip
        problems = []
        allowed = step.expect.status
        if allowed is not None:
            allowed_list = allowed if isinstance(allowed, list) else [allowed]
            if resp.status_code not in allowed_list:
                problems.append(f"status {resp.status_code}, expected "
                                f"{' or '.join(map(str, allowed_list))}")  # fmt: skip
        try:  # expected values use the same templates ({{seed.c1.id}}, {{run.user_uuid}})
            checks = render_value(step.expect.json_checks, variables)
        except KeyError as err:
            return "invalid", f"{at}: expect uses unknown template {{{{{err.args[0]}}}}}"
        problems += json_problems(data, checks)
        for name, where in step.save.items():
            try:
                variables["saved"][name] = jsonable(get_path(data, where))
            except (KeyError, IndexError, TypeError):
                problems.append(f"nothing at `{where}` to save as `{name}`")
        tracked = self._track(step, variables, db_pair)
        if tracked:
            record["tracked"] = tracked
        misses = self.target.misses(before)
        if misses:
            record["externals_without_answer"] = [
                f"{m['external']}: {m['method']} {m['path']}" for m in misses]
        record["problems"] = problems
        evidence.append(mask_text(record))
        if problems and misses:
            first = misses[0]
            return "blocked", (f"{at}: needs an answer from {first['external']} for "
                               f"{first['method']} {first['path']} (no fixture); "
                               + "; ".join(problems))  # fmt: skip
        if problems and step.setup:  # it only prepares data: a failure is the case's
            return "invalid", f"{at} (setup, proves nothing): " + "; ".join(problems)
        if problems:
            return "failed", f"{at}: " + "; ".join(problems)
        return None

    def _track(self, step: AcceptanceStep, variables: dict, db_pair: Any) -> list[str]:
        if db_pair is None:
            return []
        db_repo, db = db_pair
        out = []
        for t in step.track:
            table = str(t.get("table") or "")
            try:
                where = render_value(dict(t.get("where") or {}), variables)
                rows = db.select_keys(table, where)
            except (KeyError, DbGuardError, Exception):  # a bad track entry: cleanup sweeps
                continue
            for key in rows:
                self.ledger.add(db_repo, table, key, "track")
            out.append(f"{table}: {len(rows)}")
        return out

    def _db_check(self, db: DbPort, check: Any, variables: dict,
                  evidence: list[dict]) -> str | None:  # fmt: skip
        try:
            sql, params = sql_params(check.sql, variables)
        except KeyError as err:
            return f"invalid: DB check uses unknown {{{{{err.args[0]}}}}}"
        deadline = time.monotonic() + max(0.0, check.wait_s)
        while True:
            try:
                value = db.scalar(sql, params)
            except DbGuardError as err:
                return f"invalid: {err}"
            except Exception as err:
                return f"invalid: DB check failed to run: {str(err).splitlines()[0][:300]}"
            ok = True
            try:
                expected = render_value(check.equals, variables)
            except KeyError as err:
                return f"invalid: DB check expects unknown {{{{{err.args[0]}}}}}"
            if check.equals is not None and not _same(value, expected):
                ok = False
            if check.at_least is not None and (value is None or float(value) < check.at_least):
                ok = False
            if ok or time.monotonic() >= deadline:
                break
            time.sleep(min(2.0, max(0.2, check.wait_s / 10)))
        evidence.append({"db_check": check.sql[:500], "value": jsonable(value), "ok": ok})
        if ok:
            return None
        want = (f"= {render_value(check.equals, variables)!r}" if check.equals is not None
                else f">= {check.at_least}")  # fmt: skip
        waited = f" after {check.wait_s:.0f}s" if check.wait_s else ""
        return f"DB check `{check.sql[:160]}` gave {jsonable(value)!r}, expected {want}{waited}"

    # ---------- cleanup ----------

    def cleanup(self, dbs: dict[str, DbPort]) -> dict:
        """Delete exactly what the run created (see the module docstring). Never raises."""
        return cleanup(self.ledger, dbs)


def _short(data: Any) -> Any:
    text = data if isinstance(data, str) else json.dumps(jsonable(data), default=str)
    if len(text) <= MAX_BODY_CHARS:
        return data
    return text[:MAX_BODY_CHARS] + f"… ({len(text)} characters)"


def cleanup(ledger: Ledger, dbs: dict[str, DbPort]) -> dict:
    """Delete the ledger's rows + the run partner's rows in its tables + their children, by
    primary key, children first; then check nothing is left. Returns the report."""
    report: dict[str, Any] = {"inserted": len(ledger.entries), "deleted": 0, "left": 0,
                              "tables": {}, "error": None}  # fmt: skip
    try:
        for repo, db in dbs.items():
            for partner in ledger.swept_partners():
                for table in ledger.tables:
                    if db.table_exists(table) and "partner_id" in db.columns(table):
                        for key in db.select_keys(table, {"partner_id": partner}):
                            ledger.add(repo, table, key, "partner")
            depth = 0
            frontier = [e for e in ledger.entries if e["repo"] == repo]
            while frontier and depth < CHILD_DEPTH:
                depth += 1
                found = []
                by_table: dict[str, list[dict]] = {}
                for e in frontier:
                    by_table.setdefault(e["table"], []).append(e)
                for table, entries in by_table.items():
                    pk = db.primary_key(table)
                    for child, ccols, pcols in db.children(table):
                        if not set(pcols) <= set(pk):  # an FK to a non-key column: fetch rows
                            continue
                        keys = db.select_child_keys(child, ccols, pcols,
                                                    [e["key"] for e in entries])  # fmt: skip
                        for key in keys:
                            if ledger.add(repo, child, key, "child", depth):
                                found.append(ledger.entries[-1])
                frontier = found
            mine = [e for e in ledger.entries if e["repo"] == repo]
            order = sorted(enumerate(mine), key=lambda ie: (-ie[1]["depth"], -ie[0]))
            rows = [(e["table"], e["key"]) for _, e in order]
            report["deleted"] += db.delete_keys(rows)
            for table, _key in rows:
                report["tables"][table] = report["tables"].get(table, 0) + 1
            left = sum(1 for table, key in rows if db.select_keys(table, key))
            for partner in ledger.swept_partners():
                for table in ledger.tables:
                    if db.table_exists(table) and "partner_id" in db.columns(table):
                        left += len(db.select_keys(table, {"partner_id": partner}))
            report["left"] += left
    except Exception as err:  # keep the ledger: the next run or `--cleanup` tries again
        report["error"] = str(err).splitlines()[0][:400] if str(err) else type(err).__name__
        return report
    report["found"] = len(ledger.entries)
    if report["left"] == 0:
        ledger.path.unlink(missing_ok=True)
    report["at"] = datetime.now(UTC).isoformat(timespec="seconds")
    return report
