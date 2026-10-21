"""Test cases for the live acceptance run (P10).

Two sources:

- **Contract cases** (`contract_cases`, no AI): from the planner's interface contract, so the
  coder can't influence them. Per endpoint the ticket implements: no auth → 401; for a body
  endpoint with required fields, an empty body → 4xx (validation). (Not "without the route's
  permission → 403": not every gateway route checks permissions, DEMO-8; the Tester adds that
  case when a criterion is about permissions.) Only checks whose
  outcome doesn't depend on business data: happy paths need data, so they come from the
  tester, which seeds it.
- **Tester cases** (`agents/tester.py`): one or more per acceptance criterion, as plain data;
  `plan_problems` checks them before they run (and the problems go back to the tester once).

Rules both kinds follow (enforced by the executor too): a PUT / PATCH / DELETE only touches a
row the run created (`{{seed.…}}` / `{{saved.…}}` in its path), every DB check is one SELECT,
and only known identities, repos and externals are used.
"""

from __future__ import annotations

import re
from typing import Any

from pipeline.schemas import AcceptanceCase, AcceptancePlan, AcceptanceStep, StepExpect
from pipeline.testing.envfile import template_names
from pipeline.testing.identities import VARIANTS, split_variant

WRITE_METHODS = frozenset({"PUT", "PATCH", "DELETE"})
OWN_REF = re.compile(r"\{\{\s*(seed|saved)\.[\w.]+\s*\}\}")
PATH_PARAM = re.compile(r":(\w+)|\{(\w+)\}")
TEMPLATE_ROOTS = ("run", "seed", "saved", "url", "mock", "date", "lookup")
_DATE_NAME = re.compile(r"^(today|(m0|p\d{1,2}|n\d{1,2})(_first|_last)?)$")
PLACEHOLDER_ID = "00000000-0000-4000-8000-000000000000"  # a path id no row has (auth checks)


def fill_path_params(path: str) -> str:
    """`/configs/:id` → `/configs/<an id no row has>` (for auth / validation checks)."""
    return PATH_PARAM.sub(PLACEHOLDER_ID, path)


def _required_body(schema: dict) -> bool:
    body = (schema or {}).get("body") if "body" in (schema or {}) else schema
    return bool(isinstance(body, dict) and body.get("required"))


def expand_fixtures(plan: AcceptancePlan) -> tuple[AcceptancePlan, list[str]]:
    """Each case's `use` names → the fixtures' seed rows, put before the case's own (so a case
    writes `use: ["future_cfg"]` instead of repeating the rows). Returns the plan with the cases
    expanded (`use` emptied) and the problems (an unknown fixture name)."""
    problems: list[str] = []
    cases: list[AcceptanceCase] = []
    for case in plan.cases:
        rows = []
        for name in case.use:
            if name not in plan.fixtures:
                problems.append(f"case {case.id}: unknown fixture `{name}` "
                                f"({', '.join(plan.fixtures) or 'none defined'})")  # fmt: skip
                continue
            rows += plan.fixtures[name]
        cases.append(case.model_copy(update={"seed": [*rows, *case.seed], "use": []}) if case.use
                     else case)  # fmt: skip
    return plan.model_copy(update={"cases": cases}), problems


def contract_cases(plan: Any, repos: list[str], config: Any) -> list[AcceptanceCase]:
    """The contract's auth and validation checks for the repos under test."""
    acc = config.acceptance
    out: list[AcceptanceCase] = []
    n = 0

    def add(repo: str, title: str, step: AcceptanceStep) -> None:
        nonlocal n
        n += 1
        out.append(AcceptanceCase(id=f"C{n}", repo=repo, source="contract", title=title,
                                  steps=[step]))  # fmt: skip

    tasks = {t.repo: t for t in plan.tasks}
    for ep in plan.contract.internal_endpoints:
        repo = ep.provider_repo
        task = tasks.get(repo)
        if repo not in repos or (task is not None and ep.id not in task.implements and
                                 not ep.is_new):  # fmt: skip
            continue
        if acc.default_identity.get(repo, "none") == "none":
            continue  # no identity configured: nothing to compare "no auth" with
        path = fill_path_params(ep.path)
        add(repo, f"{ep.method} {ep.path} without client credentials is refused",
            AcceptanceStep(repo=repo, method=ep.method, path=path, identity="none",
                           body={} if ep.method in ("POST", "PUT", "PATCH") else None,
                           expect=StepExpect(status=[401, 403])))  # fmt: skip
        if ep.method in ("POST", "PUT", "PATCH") and _required_body(ep.request_schema) \
                and "{" not in path and ":" not in ep.path:  # fmt: skip
            add(repo, f"{ep.method} {ep.path} with an empty body is rejected",
                AcceptanceStep(repo=repo, method=ep.method, path=path, body={},
                               expect=StepExpect(status=[400, 422])))  # fmt: skip
    gateway = next((r for r in repos if config.repo(r).role == "gateway"), None)
    for ep in plan.contract.public_endpoints if gateway else []:
        assert gateway is not None
        ident = acc.default_identity.get(gateway, "none")
        if ident == "none":
            continue
        path = fill_path_params(ep.path)
        # the gateway validates a body before it checks the login (validateSchema runs first),
        # so an empty body is refused with 400 and never reaches the login check (DEMO-10): for
        # body methods "refused" is 400 / 401 / 403; the Tester proves the 401 with a valid body
        body_method = ep.method in ("POST", "PUT", "PATCH")
        add(gateway, f"{ep.method} {ep.path} without a login is refused",
            AcceptanceStep(repo=gateway, method=ep.method, path=path, identity="none",
                           body={} if body_method else None,
                           expect=StepExpect(status=[400, 401, 403] if body_method
                                             else [401, 403])))  # fmt: skip
    return out


def _name_problem(where: str, name: str, lookups: set[str]) -> list[str]:
    """`{{date.…}}` / `{{lookup.…}}` names that don't exist (the others are checked in place)."""
    root, _, rest = name.partition(".")
    if root == "date" and not _DATE_NAME.match(rest):
        return [f"{where}: unknown {{{{{name}}}}} (use date.today, date.m0, date.p1…p48, "
                "date.n1…n36, each also with _first / _last)"]
    if root == "lookup" and rest.split(".")[0] not in lookups:
        return [f"{where}: {{{{{name}}}}} names no lookup of this case"]
    return []


def _expects_only_errors(step: AcceptanceStep) -> bool:
    """A call that must fail (4xx / 5xx only) creates nothing to track."""
    allowed = step.expect.status
    codes = allowed if isinstance(allowed, list) else [allowed]
    return bool(codes) and all(isinstance(c, int) and c >= 400 for c in codes)


def plan_problems(plan: AcceptancePlan, required: dict[str, list[str]], repos: list[str],
                  config: Any) -> list[str]:  # fmt: skip
    """What's wrong with the tester's cases (empty = fine). `required`: repo → criterion ids.
    With a real partner (`acceptance.run_values.partner_id`), every POST must `track` the rows
    it creates: the cleanup can't sweep a partner that has other data."""
    acc = config.acceptance
    shared_partner = "partner_id" in acc.run_values
    plan, problems = expand_fixtures(plan)
    ids: set[str] = set()
    all_criteria = {c for cs in required.values() for c in cs}
    covered = {c.criterion for c in plan.cases} | {str(b.get("criterion")) for b in plan.blocked}
    for case in plan.cases:
        where = f"case {case.id}"
        if case.id in ids:
            problems.append(f"{where}: the id is used twice")
        ids.add(case.id)
        if case.repo not in repos:
            problems.append(f"{where}: repo `{case.repo}` isn't one this ticket tests "
                            f"({', '.join(repos)})")  # fmt: skip
        if case.criterion and case.criterion not in all_criteria:
            problems.append(f"{where}: `{case.criterion}` isn't a criterion of the tasks "
                            f"({', '.join(sorted(all_criteria)) or 'none'})")  # fmt: skip
        if not case.steps:
            problems.append(f"{where}: no steps")
        for name in case.externals:
            if name not in acc.externals:
                problems.append(f"{where}: unknown external `{name}` "
                                f"({', '.join(acc.externals) or 'none configured'})")  # fmt: skip
        aliases = {s.alias for s in case.seed if s.alias}
        lookups = {look.name for look in case.lookups}
        saved: set[str] = set()
        for look in case.lookups:
            if not re.match(r"^\s*(select|with)\b", look.sql, re.I):
                problems.append(f"{where}: lookup `{look.name}` must be a SELECT query")
        for name in template_names([[s.values for s in case.seed], [x.sql for x in case.lookups],
                                    [c.sql for c in case.db], [c.equals for c in case.db]]):
            problems += _name_problem(where, name, lookups)  # fmt: skip
        for i, step in enumerate(case.steps, 1):
            at = f"{where} step {i}"
            if step.repo not in repos:
                problems.append(f"{at}: repo `{step.repo}` isn't under test")
            base, variant = split_variant(step.identity)
            if step.identity not in ("default", "none") and (
                    base not in acc.identities or variant not in ("", *VARIANTS)):  # fmt: skip
                problems.append(f"{at}: unknown identity `{step.identity}` (use "
                                f"{', '.join(['default', 'none', *acc.identities])})")  # fmt: skip
            if step.method in WRITE_METHODS and not OWN_REF.search(step.path):
                problems.append(f"{at}: {step.method} must target a row this case created "
                                "(put {{seed.<alias>.<column>}} or {{saved.<name>}} in the "
                                "path); existing data is read-only")  # fmt: skip
            for name in template_names([step.path, step.query, step.body, step.headers]):
                root = name.split(".")[0].split(":")[0]
                if root not in TEMPLATE_ROOTS:
                    problems.append(f"{at}: unknown template {{{{{name}}}}}")
                elif root == "seed" and name.split(".")[1] not in aliases:
                    problems.append(f"{at}: {{{{{name}}}}} names no seed alias of this case")
                elif root == "saved" and name.split(".")[1] not in saved:
                    problems.append(f"{at}: {{{{{name}}}}} isn't saved by an earlier step")
                else:
                    problems += _name_problem(at, name, lookups)
            # a call made as the run's own partner (`!own`) creates rows the cleanup sweeps
            if shared_partner and variant != "own" and step.method == "POST" \
                    and step.expect.status is not None and not step.track \
                    and not _expects_only_errors(step):  # fmt: skip
                problems.append(f"{at}: the partner is a real one with existing data, so a POST "
                                "must `track` the rows it creates (for the cleanup)")  # fmt: skip
            saved |= set(step.save)
            if step.expect.status is None:
                problems.append(f"{at}: expect.status is required")
        for check in case.db:
            if not re.match(r"^\s*(select|with)\b", check.sql, re.I):
                problems.append(f"{where}: a db check must be a SELECT query")
    missing = sorted(all_criteria - covered)
    if missing:
        problems.append("no case or blocked entry for: " + ", ".join(missing))
    return problems
