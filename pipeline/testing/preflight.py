"""The preflight before coding (P10, the `env_check` node): can this ticket be tested live?

Checked in this order, cheapest first, so a missing tool is reported in a second:

1. **tools**: `node`, `redis-server`, `openssl` (the local Redis' TLS certificate);
2. per repo with a `stack` block: its `env_from` file exists; the database host is in
   `acceptance.allowed_db_hosts`; the database answers `SELECT 1` (the VPN is usually the
   reason it doesn't); variables the repo's `.env.example` names that neither `env_from` nor
   `stack.env` sets (names only, never values; a warning);
3. the external services the plan says the code calls (`external_calls`): configured, with
   fixtures or reachable; a missing one is a warning (its criteria come back blocked);
4. with `acceptance.preflight_boot`: the whole stack starts once on the worktrees' current code
   (the default branch, before coding) and every health endpoint answers.

Problems in 1, 2 (except the warning) and 4 stop the ticket before any coder run: they need a
person (start the VPN, install Redis), not a coder.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from typing import Any

from pipeline.schemas import EnvReport, ServiceNeed
from pipeline.testing.db import DbGuardError, PgDb, params_from_env
from pipeline.testing.envfile import expand_path, parse_env, read_env_file
from pipeline.testing.mock_server import load_fixtures
from pipeline.testing.stack import Stack, StackError

BLOCKING = frozenset({"missing", "failed"})


def repos_under_test(deps: Any, repos: list[str]) -> list[str]:
    """The ticket's repos that have a `stack` block, in coding order."""
    return [r for r in deps.config.repo_order(repos) if deps.config.repo(r).stack is not None]


def _tools(deps: Any) -> list[ServiceNeed]:
    wanted = [("node", "Node.js, to run the apps"),
              ("redis-server", "the private Redis for each test run (brew install redis)")]
    if deps.config.acceptance.redis_tls:
        wanted.append(("openssl", "the local Redis' TLS certificate"))
    return [ServiceNeed(kind="tool", name=name,
                        status="ok" if shutil.which(name) else "missing",
                        detail="" if shutil.which(name) else f"not installed: needed for {why}")
            for name, why in wanted]  # fmt: skip


def _repo_needs(deps: Any, key: str, repo: str) -> list[ServiceNeed]:
    cfg = deps.config.repo(repo).stack
    out: list[ServiceNeed] = []
    try:
        env = read_env_file(cfg.env_from, deps.home)
    except FileNotFoundError as err:
        return [ServiceNeed(repo=repo, kind="env", name="stack.env_from", status="missing",
                            detail=f"{err.filename} doesn't exist")]  # fmt: skip
    if cfg.env_from:
        out.append(ServiceNeed(repo=repo, kind="env", name="stack.env_from", status="ok",
                               detail=f"{len(env)} variables"))  # fmt: skip
    example = deps.workspaces.worktree_path(key, repo) / ".env.example"
    if example.is_file():
        wanted = set(parse_env(example.read_text(errors="replace")))
        unset = sorted(wanted - set(env) - set(cfg.env) - {cfg.port_env, "NODE_ENV", "PGAPPNAME"})
        if unset:
            shown = ", ".join(unset[:12]) + (f" (+{len(unset) - 12} more)" if len(unset) > 12
                                             else "")  # fmt: skip
            out.append(ServiceNeed(repo=repo, kind="env", name="unset variables",
                                   status="warning", detail=shown))  # fmt: skip
    if cfg.db is not None:
        out.append(_database(deps, key, repo, env, cfg.db))
    return out


def _database(deps: Any, key: str, repo: str, env: dict[str, str], names: Any) -> ServiceNeed:
    try:
        params = params_from_env(env, names)
    except KeyError as err:
        return ServiceNeed(repo=repo, kind="database", name="connection", status="missing",
                           detail=f"env_from doesn't set {err.args[0]}")  # fmt: skip
    except ValueError as err:
        return ServiceNeed(repo=repo, kind="database", name="connection", status="failed",
                           detail=str(err))  # fmt: skip
    try:
        db = PgDb(params, deps.config.acceptance.allowed_db_hosts, f"aipl_check_{key}")
    except DbGuardError as err:
        return ServiceNeed(repo=repo, kind="database", name=params.where(), status="failed",
                           detail=str(err))  # fmt: skip
    except Exception as err:  # psycopg: unreachable (VPN), refused, wrong password …
        first = str(err).strip().splitlines()[0][:300] if str(err).strip() else type(err).__name__
        return ServiceNeed(repo=repo, kind="database", name=params.where(), status="failed",
                           detail=f"can't connect ({first}). Is the VPN on?")  # fmt: skip
    try:
        db.scalar("SELECT 1", [])
    finally:
        db.close()
    return ServiceNeed(repo=repo, kind="database", name=params.where(), status="ok")


def _externals(deps: Any, plan: Any) -> list[ServiceNeed]:
    acc = deps.config.acceptance
    out: list[ServiceNeed] = []
    for name in sorted({c.service for c in getattr(plan, "external_calls", None) or []}):
        ext = acc.externals.get(name)
        if ext is None:
            why = ("not in acceptance.externals: its calls get no answer, so criteria that need "
                   "it come back blocked")
            out.append(ServiceNeed(kind="external", name=name, status="warning", detail=why))
            continue
        if ext.mode == "blocked":
            out.append(ServiceNeed(kind="external", name=name, status="warning",
                                   detail="blocked in tests: criteria that need it come back "
                                   "blocked"))  # fmt: skip
            continue
        if ext.mode == "real_dev":
            out.append(ServiceNeed(kind="external", name=name, status="ok",
                                   detail="reads go to the real dev service (writes refused)"))
            continue
        directory = (expand_path(ext.fixtures, deps.home) if ext.fixtures
                     else deps.home / "fixtures" / name)  # fmt: skip
        count = len(load_fixtures(directory))
        out.append(ServiceNeed(kind="external", name=name,
                               status="ok" if count else "warning",
                               detail=f"{count} fixtures" if count else "no fixtures yet "
                               f"(`pipeline record {name} …`): its criteria come back blocked"))
    return out


def preflight(deps: Any, key: str, repos: list[str], plan: Any = None,
              boot: bool | None = None) -> EnvReport:  # fmt: skip
    """The report (never raises)."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    under_test = repos_under_test(deps, repos)
    if not under_test:
        return EnvReport(ok=True, checked_at=now, needs=[ServiceNeed(
            kind="app", name="live tests", status="warning",
            detail="none of this ticket's repos has a `stack` block, so nothing is tested live",
        )])  # fmt: skip
    needs = _tools(deps)
    for repo in under_test:
        needs += _repo_needs(deps, key, repo)
    needs += _externals(deps, plan)
    problems = [_line(n) for n in needs if n.status in BLOCKING]
    want_boot = deps.config.acceptance.preflight_boot if boot is None else boot
    if want_boot and not problems:
        stack = Stack(deps, key, under_test)
        try:
            stack.start()
            needs += [ServiceNeed(repo=r, kind="app", name="starts", status="ok",
                                  detail=f"health answered on port {stack.apps[r].port}")
                      for r in under_test]  # fmt: skip
        except StackError as err:
            tail = f"\n\n```\n{err.log[-1500:]}\n```" if err.log else ""
            needs.append(ServiceNeed(repo=err.repo, kind="app", name="starts", status="failed",
                                     detail=f"{err}{tail}"))  # fmt: skip
        finally:
            stack.stop()
        problems = [_line(n) for n in needs if n.status in BLOCKING]
    return EnvReport(ok=not problems, needs=needs, problems=problems, checked_at=now)


def _line(n: ServiceNeed) -> str:
    where = f"{n.repo}: " if n.repo else ""
    return f"{where}{n.name}: {n.detail or n.status}"


def report_md(report: EnvReport) -> str:
    """The report as a short table (chat, Jira, `pipeline env`)."""
    icon = {"ok": "✅", "warning": "⚠️", "missing": "❌", "failed": "❌"}
    lines = ["| What | | Detail |", "|---|---|---|"]
    for n in report.needs:
        what = f"{n.repo}: {n.name}" if n.repo else n.name
        detail = (n.detail.split("\n\n```")[0] if n.detail else "").replace("|", "\\|")
        lines.append(f"| {what} | {icon.get(n.status, n.status)} | {detail} |")
    return "\n".join(lines)
