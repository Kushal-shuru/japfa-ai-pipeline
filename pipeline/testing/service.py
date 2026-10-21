"""`AcceptanceService`: one live acceptance run for a ticket (P10), for the graph and the CLI.

`run(state)`:

1. **One ticket at a time** (`lock`: a file lock in `$PIPELINE_HOME`; a second ticket waits).
2. The databases: the dev DB per repo with `stack.db` (allowlisted host only). A ledger left
   by a run that crashed is cleaned up first.
3. **Migrations**: the ticket's migration files vs the dev DB's `SequelizeMeta`. One that
   changed after it was applied stops the run (a person undoes and re-applies it); pending ones
   don't stop it: the cases touching their tables come back *blocked* and the graph waits.
4. **Cases**: the contract's (no AI) + the tester's, cached in the ticket's
   `.agent/acceptance_plan.json` with the inputs they were written for (criteria + contract):
   a fix round re-runs the same cases without another agent run.
5. The **stack** on the ticket's code; the executor runs every case; *invalid* tester cases go
   back to the tester once and run again.
6. **Cleanup** (always, also on errors): exactly the rows the run created; then the stack stops.
7. Results per repo (`RepoWork.acceptance_result`) and for the ticket, written as
   `.agent/acceptance.json` in each worktree (the reviewer's input) and the ticket dir.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from pipeline.environment import machine_problem
from pipeline.logging import get_logger
from pipeline.schemas import (
    AcceptanceCase,
    AcceptancePlan,
    AcceptanceResult,
    AgentRun,
    CaseResult,
)
from pipeline.testing.cases import contract_cases, expand_fixtures
from pipeline.testing.db import DbPort, PgDb, params_from_env
from pipeline.testing.executor import Executor, Ledger, Target, cleanup
from pipeline.testing.identities import Identities
from pipeline.testing.migrations import (
    NOTE,
    Migration,
    changed_after_applied,
    remember_applied,
    satisfied_by_tables,
    ticket_migrations,
)
from pipeline.testing.preflight import preflight, repos_under_test
from pipeline.testing.stack import Stack, StackError
from pipeline.workspace import AGENT_DIR

LOCK_FILE = "acceptance.lock"
PLAN_FILE = "acceptance_plan.json"
PLAN_KEY_FILE = "acceptance_plan.key"
# bump when the Tester's rules change (prompts/tester.md, plan_problems): cached cases written
# under older rules are written again instead of reused (2: the per-endpoint budget is gone, D183)
PLAN_RULES_VERSION = 3  # 3: date templates, lookups, own partner, fixtures, per-criterion cache
FAILED_NOTE = "acceptance_failures"  # ticket note: case id → reason of the cases sent to a coder
RESULT_FILE = "acceptance.json"


@dataclass
class Outcome:
    """What a run found. Exactly one of the problem fields is set when it couldn't finish."""

    result: AcceptanceResult
    per_repo: dict[str, AcceptanceResult] = field(default_factory=dict)
    pending: list[Migration] = field(default_factory=list)
    changed: list[Migration] = field(default_factory=list)  # changed after applied on dev
    machine: str | None = None  # the machine's problem (DB unreachable, Redis didn't start)
    boot_failure: tuple[str, str] | None = None  # (repo, log): the app crashed on the new code
    tester_error: str | None = None
    slot: Any = None
    runs: list[AgentRun] = field(default_factory=list)
    skipped: bool = False
    repeated: list[CaseResult] = field(default_factory=list)  # failed the same way after a fix

    @property
    def failing(self) -> list[CaseResult]:
        return [r for r in self.result.results if r.status == "failed"]

    @property
    def invalid(self) -> list[CaseResult]:
        return [r for r in self.result.results if r.status == "invalid"]

    @property
    def blocked(self) -> list[CaseResult]:
        return [r for r in self.result.results if r.status == "blocked"]


def reason_key(reason: str) -> str:
    """A failure reason without ids and numbers, to tell "the same failure again" from a new one."""
    text = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<id>", reason)
    return re.sub(r"\d{5,}", "<n>", text.lower()).strip()[:300]  # keep status codes (409 ≠ 500)


def _hash(*parts: Any) -> str:
    text = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()[:20]


def required_criteria(state: Any, repos: list[str]) -> dict[str, list[tuple[str, str]]]:
    """Per repo, its task's criteria (id, text in the ticket's own words when it has them)."""
    from pipeline.agents.planner import task_criteria, ticket_ac_split

    plan = state.get("plan")
    ctx = state.get("context")
    texts = ticket_ac_split(ctx.description_md)[0] if ctx is not None else {}
    out: dict[str, list[tuple[str, str]]] = {}
    for repo in repos:
        task = next((t for t in plan.tasks if t.repo == repo), None) if plan else None
        if task is not None:
            items = task_criteria(task, plan, texts)
            if texts:  # the ticket numbers its criteria: one it dropped since the plan is gone
                items = [(cid, text) for cid, text in items
                         if not (cid.startswith("AC") and cid not in texts)]  # fmt: skip
            out[repo] = items
    return out


def _merge_cleanup(per_case: dict, final: dict) -> dict:
    """The case-by-case deletes + the final cleanup, as one report."""
    tables = dict(per_case["tables"])
    for table, n in (final.get("tables") or {}).items():
        tables[table] = tables.get(table, 0) + n
    return {**final, "inserted": per_case["inserted"] + final.get("inserted", 0),
            "deleted": per_case["deleted"] + final.get("deleted", 0), "tables": tables,
            "left": final.get("left", 0), "error": final.get("error") or per_case["error"]}


def _blocked_repo(entry: dict, required: dict, repos: list[str]) -> str:
    """The repo a blocked entry is about: its `repo`, a "<repo>: …" reason, else the first repo
    whose task has the criterion (DEMO-8: ws1 entries landed on ws2)."""
    named = str(entry.get("repo") or "")
    if named in repos:
        return named
    reason = str(entry.get("reason") or "")
    for repo in repos:
        if reason.startswith(f"{repo}:"):
            return repo
    return next((r for r, items in required.items()
                 if any(i == entry.get("criterion") for i, _ in items)), repos[0])


class AcceptanceService:
    def __init__(self, deps: Any) -> None:
        self.deps = deps

    @property
    def mode(self) -> str:
        return self.deps.config.acceptance.mode

    # ---------- one ticket at a time ----------

    @contextlib.contextmanager
    def lock(self, key: str, on_wait: Any = None) -> Iterator[None]:
        path = self.deps.home / LOCK_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                f.seek(0)
                holder = f.read().strip() or "another ticket"
                if on_wait is not None:
                    on_wait(holder)
                fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0)
            f.truncate()
            f.write(key)
            f.flush()
            try:
                yield
            finally:
                f.seek(0)
                f.truncate()
                fcntl.flock(f, fcntl.LOCK_UN)

    # ---------- the parts ----------

    def preflight(self, state: Any) -> Any:
        return preflight(self.deps, state["key"], list(state.get("repos") or {}),
                         state.get("plan"))  # fmt: skip

    def repos(self, state: Any) -> list[str]:
        return repos_under_test(self.deps, list(state.get("repos") or {}))

    def open_dbs(self, key: str, repos: list[str], run_id: str = "check") -> dict[str, DbPort]:
        """The dev DB per repo with `stack.db` (raises on a guard or connection problem)."""
        from pipeline.testing.envfile import read_env_file

        dbs: dict[str, DbPort] = {}
        try:
            for repo in repos:
                cfg = self.deps.config.repo(repo).stack
                if cfg is None or cfg.db is None:
                    continue
                env = read_env_file(cfg.env_from, self.deps.home)
                dbs[repo] = PgDb(params_from_env(env, cfg.db),
                                 self.deps.config.acceptance.allowed_db_hosts,
                                 f"aipl_test_{key}_{run_id}")  # fmt: skip
        except BaseException:
            for db in dbs.values():
                db.close()
            raise
        return dbs

    def migrations(self, key: str, repos: list[str],
                   dbs: dict[str, DbPort]) -> tuple[list[Migration], list[Migration]]:  # fmt: skip
        """(pending, changed after applied) over the repos with a database."""
        ws = self.deps.workspaces
        found: list[Migration] = []
        for repo in repos:
            if repo not in dbs:
                continue
            cfg = self.deps.config.repo(repo)
            worktree = ws.worktree_path(key, repo)
            changed = ws.changed_paths(worktree, cfg.default_branch)["vs_default"]
            found += ticket_migrations(repo, worktree, changed, (cfg.paths or {}).get("migrations"))
        applied: set[str] = set()
        for repo, db in dbs.items():
            applied |= db.applied_migrations([m.name for m in found if m.repo == repo])
        seen = self.deps.store.get_note(key, NOTE) or {}
        changed_after = changed_after_applied(found, applied, seen)
        self.deps.store.set_note(key, NOTE, remember_applied(found, applied, seen))
        satisfied = self.satisfied(found, dbs)
        return [m for m in found if m.name not in applied | satisfied], changed_after

    @staticmethod
    def satisfied(found: list[Migration], dbs: dict[str, DbPort]) -> set[str]:
        """Create-table-only migrations whose tables are on dev already (under other names)."""
        out: set[str] = set()
        for m in found:
            db = dbs.get(m.repo)
            if db is not None:
                out |= satisfied_by_tables([m], {t for t in m.creates if db.table_exists(t)})
        return out

    def redundant_migrations(self, key: str, repo: str, names: list[str]) -> list[str]:
        """Of `names` (what `db:migrate` would run), those whose tables are on dev already."""
        dbs = self.open_dbs(key, [repo])
        try:
            worktree = self.deps.workspaces.worktree_path(key, repo)
            cfg = self.deps.config.repo(repo)
            changed = self.deps.workspaces.changed_paths(worktree, cfg.default_branch)["vs_default"]
            found = ticket_migrations(repo, worktree, changed, (cfg.paths or {}).get("migrations"))
            done = self.satisfied(found, dbs)
            return [n for n in names if n in done]
        finally:
            for db in dbs.values():
                db.close()

    def pending_applied(self, key: str, pending: list[dict]) -> bool:
        """Are these migrations ({repo, name}) on the dev DB now? (the poller; read-only)"""
        repos = sorted({str(p["repo"]) for p in pending})
        dbs = self.open_dbs(key, repos)
        try:
            for repo, db in dbs.items():
                names = [str(p["name"]) for p in pending if p["repo"] == repo]
                if not set(names) <= db.applied_migrations(names):
                    return False
            return True
        finally:
            for db in dbs.values():
                db.close()

    def cleanup_leftovers(self, key: str) -> dict | None:
        """A ledger left by a crashed run: delete its rows now (None = nothing left over)."""
        path = self.ledger_path(key)
        if not path.is_file():
            return None
        ledger = Ledger.load(path)
        repos = sorted({e["repo"] for e in ledger.entries}) or self.deps.config.repo_names
        dbs = self.open_dbs(key, [r for r in repos if r in self.deps.config.repo_names])
        try:
            return cleanup(ledger, dbs)
        finally:
            for db in dbs.values():
                db.close()

    def ledger_path(self, key: str) -> Any:
        return self.deps.workspaces.ticket_dir(key) / ".stack" / "ledger.json"

    # ---------- cases ----------

    def _plan_key(self, state: Any) -> str:
        """What the whole cached plan depends on: the Tester's rules and the contract. A
        criterion's own text is checked per criterion (`_criteria_hashes`)."""
        plan = state.get("plan")
        return _hash(PLAN_RULES_VERSION, plan.contract.model_dump(mode="json") if plan else None)

    @staticmethod
    def _criteria_hashes(required: dict) -> dict[str, str]:
        """criterion id → a hash of its text in every repo that has it."""
        ids = sorted({cid for items in required.values() for cid, _ in items})
        return {cid: _hash(sorted((repo, text) for repo, items in required.items()
                                  for c, text in items if c == cid)) for cid in ids}

    def _read_cache(self, agent: Any) -> tuple[Any, dict]:
        """(the cached plan, its key meta); (None, {}) when there is none or it can't be read."""
        try:
            meta = json.loads((agent / PLAN_KEY_FILE).read_text())
            plan = AcceptancePlan.model_validate_json((agent / PLAN_FILE).read_text())
        except (OSError, ValueError):
            return None, {}
        return (plan, meta) if isinstance(meta, dict) else (None, {})

    def cases(self, state: Any, repos: list[str], required: dict,
              blocked_tables: dict[str, str], outcome: Outcome,
              dbs: dict[str, DbPort] | None = None) -> list[AcceptanceCase]:  # fmt: skip
        """Contract cases + the tester's. The tester's cases are cached per criterion: when the
        ticket edits one criterion, only that one is written again (the rest is kept); a fix
        round re-runs the same cases without an agent run."""
        from pipeline.agents.tester import write_cases
        from pipeline.graph.nodes.common import agent_slot

        key = state["key"]
        plan = state.get("plan")
        cases = contract_cases(plan, repos, self.deps.config) if plan is not None else []
        if not any(required.values()):
            return cases
        agent = self.deps.workspaces.ticket_dir(key) / AGENT_DIR
        agent.mkdir(parents=True, exist_ok=True)
        want, hashes = self._plan_key(state), self._criteria_hashes(required)
        cached, meta = self._read_cache(agent)
        same_rules = cached is not None and meta.get("rules") == want
        old = dict(meta.get("criteria") or {}) if same_rules else {}
        stale = sorted(c for c in hashes if old.get(c) != hashes[c])
        if not same_rules or stale:
            todo = {r: [(cid, text) for cid, text in items if cid in stale or not same_rules]
                    for r, items in required.items()}
            todo = {r: items for r, items in todo.items() if items}
            keep = cached if same_rules else AcceptancePlan()
            live = {c for c in hashes if c not in stale}
            kept_cases = [c for c in keep.cases if c.criterion in live]
            kept_blocked = [b for b in keep.blocked if str(b.get("criterion")) in live]
            schema = self._facts(key, repos, dbs)
            first = 1 + max([int(re.sub(r"\D", "", c.id) or 0) for c in kept_cases], default=0)
            with agent_slot(state, self.deps, "tester") as slot:
                result, run = write_cases(
                    self.deps, key, repos, todo, blocked_tables, slot=slot.name, schema=schema,
                    provider_override=state.get("provider_override"), first_id=first,
                )  # fmt: skip
            outcome.slot, outcome.runs = slot, [*outcome.runs, run]
            if result is None:
                from pipeline.chat.summaries import error_words

                outcome.tester_error = error_words(run.error)
                return cases
            fresh, _ = expand_fixtures(result)  # stored expanded: kept and new cases can't clash
            taken = {c.id for c in kept_cases}
            new_cases = []
            for c in fresh.cases:
                while c.id in taken:
                    first += 1
                    c = c.model_copy(update={"id": f"A{first}"})
                taken.add(c.id)
                new_cases.append(c)
            cached = AcceptancePlan(
                cases=[*kept_cases, *new_cases], blocked=[*kept_blocked, *fresh.blocked],
                notes=list(dict.fromkeys([*(keep.notes if same_rules else []), *fresh.notes])))
            (agent / PLAN_FILE).write_text(cached.model_dump_json(indent=1))
            (agent / PLAN_KEY_FILE).write_text(json.dumps({"rules": want, "criteria": hashes}))
        assert cached is not None
        cached, _ = expand_fixtures(cached)
        if any(c.criterion and c.criterion not in hashes for c in cached.cases) or any(
                str(b.get("criterion")) not in hashes for b in cached.blocked):  # criteria dropped
            cached = cached.model_copy(update={
                "cases": [c for c in cached.cases if not c.criterion or c.criterion in hashes],
                "blocked": [b for b in cached.blocked if str(b.get("criterion")) in hashes]})
            (agent / PLAN_FILE).write_text(cached.model_dump_json(indent=1))
            (agent / PLAN_KEY_FILE).write_text(json.dumps({"rules": want, "criteria": hashes}))
        tester = [c.model_copy(update={"source": "tester"}) for c in cached.cases]
        blocked = [AcceptanceCase(id=f"B{n}", criterion=str(b.get("criterion") or ""),
                                  repo=_blocked_repo(b, required, repos),
                                  source="tester", title=str(b.get("reason") or "")[:200],
                                  steps=[]) for n, b in enumerate(cached.blocked, 1)]  # fmt: skip
        return cases + tester + blocked

    def _facts(self, key: str, repos: list[str], dbs: dict[str, DbPort] | None) -> str:
        """What the dev DB says about the tables the diffs name: their structure and (unless
        `acceptance.data_probe` is off) what the run's partner already holds."""
        from pipeline.testing.data_facts import data_md
        from pipeline.testing.schema_facts import pick_tables, schema_md

        ws = self.deps.workspaces  # the tables the diffs name (models, migrations, queries)
        diffs = "\n".join(ws.diff_vs_default(ws.worktree_path(key, r),
                                             self.deps.config.repo(r).default_branch)
                          for r in repos)  # fmt: skip
        agent = ws.ticket_dir(key) / AGENT_DIR  # the ticket and the spec name tables too
        more = [p.read_text(errors="replace") for p in (agent / "ticket.md",
                                                       agent / "spec_excerpt.md") if p.is_file()]
        text = schema_md(dbs or {}, diffs, more)
        acc = self.deps.config.acceptance
        db = next(iter((dbs or {}).values()), None)
        if getattr(acc, "data_probe", True) and db is not None and hasattr(db, "all_tables"):
            try:
                tables = pick_tables(db.all_tables(), [*more[:1], diffs, *more[1:]])
                probe = data_md(dbs or {}, tables, acc.run_values.get("partner_id", ""))
            except Exception:  # noqa: BLE001 (never stops a run)
                probe = ""
            text = "\n\n".join(x for x in (text, probe) if x)
        return text

    # ---------- the run ----------

    def run(self, state: Any, on_wait: Any = None) -> Outcome:
        key = state["key"]
        mode = "advisory" if self.mode == "advisory" else "required"
        repos = self.repos(state)
        if self.mode == "off" or not repos:
            return Outcome(result=AcceptanceResult(ok=True, mode=mode), skipped=True)
        with self.lock(key, on_wait):
            return self._run(self.fresh_state(state), repos, mode)

    def fresh_state(self, state: Any) -> Any:
        """The state with the ticket text as Jira has it now: people edit the acceptance criteria
        while a ticket waits or is stopped, and the tests must prove what the ticket says today
        (the Tester's cases are cached by criteria, so they follow the edit). Falls back to the
        text the ticket started with when Jira can't be read."""
        from pipeline.context import with_description
        from pipeline.criteria import extractor

        ctx = state.get("context")
        if ctx is None:
            return state
        try:
            issue = self.deps.jira.get_issue(state["key"])
        except Exception:  # noqa: BLE001 (a Jira outage never stops a test run)
            return state
        text = issue.get("description_md") or issue.get("description") or ""
        if not text or text == ctx.description_md:
            return state
        fresh = with_description(ctx, text, extractor(self.deps, state["key"]))
        self.deps.workspaces.write_ticket_files(fresh)
        get_logger(ticket=state["key"], node="acceptance").info("ticket_text_refreshed")
        return {**state, "context": fresh}

    def _run(self, state: Any, repos: list[str], mode: str) -> Outcome:
        key = state["key"]
        log = get_logger(ticket=key, node="acceptance")
        started = time.monotonic()
        empty = AcceptanceResult(ok=False, mode=mode)  # type: ignore[arg-type]
        try:
            leftover = self.cleanup_leftovers(key)
            if leftover and leftover.get("error"):
                return Outcome(result=empty, machine="an earlier test run's rows couldn't be "
                               f"deleted from the dev database: {leftover['error']}")  # fmt: skip
            dbs = self.open_dbs(key, repos)
        except Exception as err:
            return Outcome(result=empty, machine=f"the dev database: {err}")
        stack = Stack(self.deps, key, repos)
        outcome = Outcome(result=empty)
        heads = {r: self.deps.workspaces.head(self.deps.workspaces.worktree_path(key, r))
                 for r in repos}  # fmt: skip
        cleanup_report: dict = {}
        per_case: dict = {}  # the rows already deleted case by case (executor.cleaned)
        results: list[CaseResult] = []
        try:
            pending, changed = self.migrations(key, repos, dbs)
            outcome.pending, outcome.changed = pending, changed
            if changed:
                return outcome
            required = required_criteria(state, repos)
            blocked_tables = {t: f"waiting for the migration {m.name} on the dev database"
                              for m in pending for t in m.tables}  # fmt: skip
            cases = self.cases(state, repos, required, blocked_tables, outcome, dbs)
            if outcome.tester_error:
                return outcome
            try:
                stack.start()
            except StackError as err:
                cause = machine_problem(err.log) or (None if err.repo else str(err))
                if cause:
                    outcome.machine = f"{err} — {cause}" if err.repo else cause
                else:
                    outcome.boot_failure = (err.repo or repos[0], f"{err}\n\n{err.log}".strip())
                return outcome
            ledger = Ledger(path=self.ledger_path(key), partner_id=stack.run["partner_id"],
                            partner_owned=bool(stack.run.get("partner_owned", True)),
                            own_partner=str(stack.run.get("own_partner", "")))
            ledger.save()
            variables = stack.variables()
            identities = Identities(self.deps.config.acceptance, stack.redis.client()
                                    if stack.redis else None, stack.env_of, variables,
                                    all_perms=self._all_perms(key))  # fmt: skip
            db_repo = next(iter(dbs), None)

            def db_for(repo: str) -> tuple[str, DbPort] | None:
                if repo in dbs:
                    return repo, dbs[repo]
                return (db_repo, dbs[db_repo]) if db_repo else None

            ext = stack.externals
            target = Target(
                url=stack.url, prefix=stack.prefix, db_for=db_for, headers=identities.headers,
                misses=ext.misses if ext else (lambda _s: []),
                request_count=(lambda: len(ext.requests)) if ext else (lambda: 0),
                alive=lambda r: bool(stack.apps.get(r) and stack.apps[r].proc
                                     and stack.apps[r].proc.poll() is None),  # type: ignore[union-attr]
            )  # fmt: skip
            blocked_ext = {n: f"{n} is blocked in tests" for n, e in
                           self.deps.config.acceptance.externals.items() if e.mode == "blocked"}
            timeout = self.deps.config.acceptance.request_timeout_s
            executor = Executor(target, variables, ledger, blocked_tables, blocked_ext,
                                timeout_s=timeout, dbs=dbs)
            per_case = executor.cleaned
            runnable = [c for c in cases if c.steps]
            results = executor.run(runnable)
            results += [CaseResult(case_id=c.id, criterion=c.criterion, repo=c.repo,
                                   title=c.title, status="blocked",
                                   reason=f"the tester: {c.title}")
                        for c in cases if not c.steps]  # fmt: skip
            results = self._revise(state, repos, required, runnable, results, executor, outcome)
            results = self._recheck(state, repos, required, results, executor, outcome)
            # a case still wrong after the revision is a broken test, not a code bug: never back
            # to the coder, never proven (blocked, with why)
            results = [r if r.status != "invalid" else r.model_copy(update={
                "status": "blocked", "reason": f"the test case itself is wrong: {r.reason}"})
                for r in results]  # fmt: skip
        except Exception as err:  # never into the graph; the cleanup below still runs
            log.exception("acceptance_failed")
            outcome.machine = outcome.machine or f"the test run stopped: {err}"
        finally:
            if (self.ledger_path(key)).is_file():
                cleanup_report = cleanup(Ledger.load(self.ledger_path(key)), dbs)
            if per_case.get("inserted"):
                cleanup_report = _merge_cleanup(per_case, cleanup_report)
            stack.stop()
            for db in dbs.values():
                db.close()
        if cleanup_report.get("error") and not outcome.machine:
            outcome.machine = ("the test rows couldn't all be deleted from the dev database "
                               f"({cleanup_report['error']}); run `pipeline acceptance {key} "
                               "--cleanup`")  # fmt: skip
        ok = not any(r.status == "failed" for r in results)
        whole = AcceptanceResult(ok=ok and not outcome.machine, mode=mode,  # type: ignore[arg-type]
                                 head_shas=heads, results=results,
                                 pending_migrations=[m.name for m in outcome.pending],
                                 cleanup=cleanup_report,
                                 duration_s=round(time.monotonic() - started, 1))  # fmt: skip
        outcome.result = whole
        outcome.per_repo = {r: whole.model_copy(update={
            "repo": r, "results": [x for x in results if x.repo == r],
            "ok": not any(x.status == "failed" for x in results if x.repo == r),
            "head_shas": {r: heads[r]}}) for r in repos}  # fmt: skip
        self._write(key, outcome)
        return outcome

    def _all_perms(self, key: str) -> Any:
        from pipeline.testing.identities import security_ids

        def perms(repo: str) -> list[str]:
            cfg = self.deps.config.repo(repo)
            folder = (cfg.paths or {}).get("validators") or "src/schemas"
            return security_ids(self.deps.workspaces.worktree_path(key, repo) / folder)

        return perms

    def _revise(self, state: Any, repos: list[str], required: dict,
                cases: list[AcceptanceCase], results: list[CaseResult], executor: Executor,
                outcome: Outcome) -> list[CaseResult]:  # fmt: skip
        """Invalid tester cases → the tester once (same session) → run the revised ones."""
        from pipeline.agents.tester import revise_cases
        from pipeline.graph.nodes.common import agent_slot
        from pipeline.schemas import AcceptancePlan

        invalid = [r for r in results if r.status == "invalid"
                   and r.case_id.startswith("A")]  # fmt: skip
        if not invalid:
            return results
        key = state["key"]
        agent = self.deps.workspaces.ticket_dir(key) / AGENT_DIR
        try:
            plan = AcceptancePlan.model_validate_json((agent / PLAN_FILE).read_text())
        except (OSError, ValueError):
            return results
        with agent_slot(state, self.deps, "tester") as slot:
            revised, run = revise_cases(
                self.deps, key, repos, required, plan, invalid, slot=slot.name,
                provider_override=state.get("provider_override"),
            )
        outcome.slot, outcome.runs = slot, [*outcome.runs, run]
        if revised is None:
            return results
        (agent / PLAN_FILE).write_text(revised.model_dump_json(indent=1))
        again = {r.case_id for r in invalid}
        rerun = executor.run([c for c in revised.cases if c.id in again])
        by_id = {r.case_id: r for r in rerun}
        return [by_id.get(r.case_id, r) for r in results]

    def _recheck(self, state: Any, repos: list[str], required: dict, results: list[CaseResult],
                 executor: Executor, outcome: Outcome) -> list[CaseResult]:  # fmt: skip
        """A case that fails the same way after the coder was sent it and fixed the code is
        suspect: the data may not fit the dev database (DEMO-11: periods the real partner already
        held), and another coder round would only repeat itself. The Tester looks at it once with
        both failures (same session) and the case runs again; if it fails the same way once more,
        `outcome.repeated` tells the graph to ask a person instead of the coder."""
        from pipeline.agents.tester import revise_cases
        from pipeline.graph.nodes.common import agent_slot

        key = state["key"]
        before = self.deps.store.get_note(key, FAILED_NOTE) or {}
        repeats = [r for r in results if r.status == "failed" and r.case_id.startswith("A")
                   and before.get(r.case_id) == reason_key(r.reason)]  # fmt: skip
        if not repeats:
            return results
        agent = self.deps.workspaces.ticket_dir(key) / AGENT_DIR
        try:
            plan = AcceptancePlan.model_validate_json((agent / PLAN_FILE).read_text())
        except (OSError, ValueError):
            outcome.repeated = repeats
            return results
        asked = [r.model_copy(update={"reason": (
            "this case failed the same way before and after the code was fixed, so check the "
            "case before blaming the code: does its data fit the dev database (periods other rows "
            "already hold, values that must exist, a state it assumed)? Use `lookups`, `date.*` "
            "templates or `!own` to make it independent of what is there. It failed with: "
            f"{r.reason}")}) for r in repeats]  # fmt: skip
        with agent_slot(state, self.deps, "tester") as slot:
            revised, run = revise_cases(
                self.deps, key, repos, required, plan, asked, slot=slot.name,
                provider_override=state.get("provider_override"))  # fmt: skip
        outcome.slot, outcome.runs = slot, [*outcome.runs, run]
        if revised is None:
            outcome.repeated = repeats
            return results
        fresh, _ = expand_fixtures(revised)
        (agent / PLAN_FILE).write_text(fresh.model_dump_json(indent=1))
        ids = {r.case_id for r in repeats}
        again = {r.case_id: r for r in executor.run([c for c in fresh.cases if c.id in ids])}
        results = [again.get(r.case_id, r) for r in results]
        outcome.repeated = [r for r in results if r.case_id in ids and r.status == "failed"
                            and reason_key(r.reason) == before.get(r.case_id)]  # fmt: skip
        return results

    def _write(self, key: str, outcome: Outcome) -> None:
        ws = self.deps.workspaces
        (ws.ticket_dir(key) / AGENT_DIR).mkdir(parents=True, exist_ok=True)
        (ws.ticket_dir(key) / AGENT_DIR / RESULT_FILE).write_text(
            outcome.result.model_dump_json(indent=1))
        for repo, res in outcome.per_repo.items():
            agent = ws.worktree_path(key, repo) / AGENT_DIR
            agent.mkdir(parents=True, exist_ok=True)
            (agent / RESULT_FILE).write_text(res.model_dump_json(indent=1))
