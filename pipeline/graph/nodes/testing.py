"""Live acceptance testing in the graph (Phase 10).

plan approval → **env_check** → code_next_repo ⟲ verify → **acceptance** → open_mrs (first pass)
or push_fixes (a fix pass); acceptance failures → back to code_next_repo; a pending migration →
**migration_wait** ⏸ → acceptance.

- `env_check`: the preflight (`testing.preflight`): tools, the dev DB (allowlisted, reachable),
  the env, the apps boot. A problem stops the ticket **before any coder run** (a machine
  problem: Retry once it's fixed). `advisory` mode only warns.
- `acceptance`: one live run (`testing.service.AcceptanceService`). Failed cases go back to the
  coder with the evidence (as failed checks, so the coder's retry path is the same as verify's),
  up to `limits.max_acceptance_attempts`, then Needs Human, where `accept` is allowed (recorded,
  pinned to the tested commits). Blocked cases (no fixture, the test case itself wrong) never
  fail the code and never count as proven. Cases waiting for the ticket's migration →
  `migration_wait`.
- `migration_wait`: Jira Blocked, a card with the commands to apply the migration on dev
  (`pipeline migrate <KEY>` in a terminal; the pipeline never runs it on its own). The
  `MigrationPoller` resumes it when the dev DB has the migration; `test <KEY>` / `retry` check now.
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from pipeline.chat.cards import Action, Card
from pipeline.chat.summaries import join_words, plural
from pipeline.environment import LEAD
from pipeline.graph.nodes.common import after_run, bullets, is_local
from pipeline.graph.state import TicketState
from pipeline.graph.wrapper import expected_status, halt
from pipeline.integrations.base import IntegrationError
from pipeline.logging import get_logger
from pipeline.schemas import VerifyResult

FAILED_NOTE = "acceptance_failures"  # ticket note: case id → reason of the cases sent to a coder
BLAME_NOTE = "acceptance_blame"  # ticket note: the repos the last failure was sent to (when shared)
NOT_MINE = "not mine"  # a coder's open_questions entry: "the cause isn't in my repo"
MIGRATION_GATE = "migration"
MIGRATION_ACTIONS = ("migration_applied", "test", "retry", "stop")


def service(deps: Any) -> Any:
    if getattr(deps, "acceptance", None) is None:
        from pipeline.testing.service import AcceptanceService

        deps.acceptance = AcceptanceService(deps)
    return deps.acceptance


# ---------- env_check ----------


READY = ('mr_open', 'changes_requested', 'approved')  # D207: handed to a local review


def env_check(state: TicketState, deps: Any) -> dict:
    key = state["key"]
    svc = service(deps)
    if svc.mode == "off" or state.get("testing") is False:  # `implement KEY testing=false`
        return {"last_event": {"type": "env_skipped"}}
    previous = state.get("env_report")
    mrs_open = any(w.get("mr_iid") or (is_local(state) and w.get("status") in READY)
                   for w in (state.get("repos") or {}).values())  # fmt: skip
    if previous is not None and previous.ok and mrs_open:  # a change request mid-review
        return {"last_event": {"type": "env_ok", "reused": True}}
    from pipeline.testing.preflight import report_md

    report = svc.preflight(state)
    deps.store.record_event(key, "env_check", node="env_check",
                            data={"ok": report.ok, "problems": report.problems[:10]})  # fmt: skip
    warnings = [n for n in report.needs if n.status == "warning"]
    if report.ok:
        if warnings:
            deps.notifier.progress(key, "Ready for the live tests, with warnings (criteria that "
                                   "depend on them come back blocked):\n\n" + report_md(report),
                                   "coder", important=True)  # fmt: skip
        return {"env_report": report, "last_event": {"type": "env_ok"}}
    if svc.mode == "advisory":
        deps.notifier.progress(key, "The live tests can't run for this ticket (advisory mode, so "
                               "I carry on without them):\n\n" + bullets(report.problems),
                               "coder", important=True)  # fmt: skip
        return {"env_report": report, "last_event": {"type": "env_failed_advisory"}}
    reason = (f"{LEAD} the live acceptance tests can't run yet, so I stopped before writing any "
              f"code:\n\n{bullets(report.problems)}\n\n{report_md(report)}\n\nFix these, then "
              "tell me to try again.")  # fmt: skip
    return {"env_report": report, **halt("env_check", reason)}


# ---------- acceptance ----------


def _heads(state: TicketState, deps: Any, repos: list[str]) -> dict[str, str]:
    ws = deps.workspaces
    return {r: ws.head(ws.worktree_path(state["key"], r)) for r in repos}


def case_line(r: Any) -> str:
    who = f"{r.case_id}" + (f" ({r.criterion})" if r.criterion else "")
    return f"{who} {r.title}: {r.reason}" if r.reason else f"{who} {r.title}"


def blamed_repos(state: Any, repo: str) -> list[str]:
    """Repos whose code a request to `repo` passes through: `repo` itself and the services it
    calls (a gateway case can fail because of the service behind it)."""
    from pipeline.graph.nodes.waits import providers_of

    known = state.get("repos") or {}
    return [repo, *[r for r in providers_of(state, repo) if r in known and r != repo]]


def failures_md(repo: str, results: list[Any],
                blame: dict[str, list[str]] | None = None) -> str:  # fmt: skip
    """What the coder gets back (as failed checks: `.agent/verify_failures.md`). `blame`: case id
    → the repos it was sent to; when a case went to several, each coder is told to fix it only if
    the cause is in its own repo, and to say `not mine: why` otherwise."""
    blame = blame or {}
    lines = [f"# Live acceptance tests failed for {repo}", "",
             "The pipeline ran the apps with your code against the dev database and called "
             "them. These cases failed (the request, the response and the checks are below). Fix "
             "the code, not the cases: they come from the acceptance criteria.", ""]  # fmt: skip
    shared = {r.case_id: [b for b in blame.get(r.case_id, []) if b != repo] for r in results}
    if any(shared.values()):
        others = sorted({b for v in shared.values() for b in v})
        lines += ["## Who fixes it", "",
                  f"The cause may be in another repo: {', '.join(others)}. A request to the "
                  "gateway passes through the services behind it, and either side can be at "
                  "fault. **Fix it here only if the cause is in this repo's code.** If it is "
                  "not, change nothing and add an `open_questions` entry that starts with "
                  f"`{NOT_MINE}:` and says why (what the response shows). Don't work around "
                  "another repo's bug in this one.", ""]  # fmt: skip
    import json

    for r in results:
        via = (f" (the test called {r.repo}; its request passes through {repo})"
               if r.repo != repo else "")  # fmt: skip
        lines += [f"## {case_line(r)}{via}", "", "```json",
                  json.dumps(r.evidence, indent=1, default=str)[:6000], "```", ""]  # fmt: skip
    return "\n".join(lines)


def nobody_owns_it(state: Any, blamed: list[str]) -> str | None:
    """The last failure went to several repos and every one of their coders said `not mine`:
    another run of the same tests would fail the same way, so a person decides now."""
    if len(blamed) < 2:
        return None
    reasons: dict[str, str] = {}
    for repo in blamed:
        res = ((state.get("repos") or {}).get(repo) or {}).get("coder_result")
        said = [q for q in (res.open_questions if res else [])
                if q.strip().lower().startswith(NOT_MINE)]  # fmt: skip
        if not said:
            return None
        reasons[repo] = said[0].strip()
    return ("Every repo the failing request passes through says the cause isn't in its code:\n\n"
            + bullets([f"{repo}: {why}" for repo, why in reasons.items()])
            + "\n\nGive guidance (where the fix belongs), accept the failing tests as they are "
            "(recorded, for these commits only), or stop.")  # fmt: skip


def counts_words(result: Any) -> str:
    by: dict[str, int] = {}
    for r in result.results:
        by[r.status] = by.get(r.status, 0) + 1
    order = ("passed", "failed", "blocked")
    return ", ".join(f"{by[s]} {s}" for s in order if by.get(s)) or "no cases"


def live_line(result: Any) -> str:
    """ "7 passed, 1 blocked (AC9: no fixture for …)" for the MR, the card and Jira."""
    blocked = [r for r in result.results if r.status == "blocked" and r.criterion]
    text = counts_words(result)
    if blocked:
        text += " (" + "; ".join(f"{r.criterion}: {r.reason}" for r in blocked) + ")"
    return text


def acceptance(state: TicketState, deps: Any) -> dict:
    key = state["key"]
    svc = service(deps)
    if svc.mode == "off" or state.get("testing") is False:
        return {"last_event": {"type": "acceptance_skipped"}}
    repos = svc.repos(state)
    override = state.get("acceptance_override")
    if override and repos and override.get("heads") == _heads(state, deps, repos):
        return {"last_event": {"type": "acceptance_passed", "override": True}}

    blamed = deps.store.get_note(key, BLAME_NOTE) or []
    if blamed:
        deps.store.set_note(key, BLAME_NOTE, [])
        verdict = nobody_owns_it(state, blamed)
        if verdict:
            return halt("acceptance", verdict)

    def waiting(holder: str) -> None:
        deps.notifier.progress(key, f"{holder} is running its live tests right now; {key}'s "
                               "start as soon as they're done.")  # fmt: skip

    outcome = svc.run(state, waiting)
    update: dict[str, Any] = after_run(state, deps, outcome.slot) if outcome.slot else {}
    if outcome.skipped:
        return {**update, "last_event": {"type": "acceptance_skipped"}}
    work_all = dict(state["repos"])
    for repo, res in outcome.per_repo.items():
        work_all[repo] = {**work_all[repo], "acceptance_result": res}
    update["repos"] = work_all
    res = outcome.result
    deps.store.record_event(key, "acceptance", node="acceptance", data={
        "ok": res.ok, "counts": counts_words(res), "pending": res.pending_migrations,
        "cleanup": {k: res.cleanup.get(k) for k in ("deleted", "left", "error")},
        "duration_s": res.duration_s})  # fmt: skip

    if outcome.changed:
        items = bullets([f"`{m.name}` ({m.repo})" for m in outcome.changed])
        return {**update, **halt("acceptance", (
            "These migrations changed after they were applied on the dev database, so the "
            f"database has the old version:\n\n{items}\n\nUndo them on dev (`npx sequelize-cli "
            "db:migrate:undo` once per migration, newest first), apply them again (`pipeline "
            f"migrate {key}`), then tell me to try again."))}  # fmt: skip
    if outcome.machine:
        return {**update, **halt("acceptance", f"{LEAD} {outcome.machine}")}
    if outcome.tester_error:
        return {**update, **halt("acceptance", "The Tester couldn't write the live test cases: "
                                 f"{outcome.tester_error}")}  # fmt: skip

    failing = list(outcome.failing)
    if outcome.repeated and svc.mode != "advisory":
        deps.store.set_note(key, FAILED_NOTE, {})
        items = bullets([case_line(r) for r in outcome.repeated])
        return {**update, "repos": work_all, **halt("acceptance", (
            "These live tests failed the same way after the code was fixed and after the "
            f"Tester checked its own data:\n\n{items}\n\nAnother round with the Coder would "
            "repeat itself, so I need a person: give guidance (is the test or the code wrong?), "
            "accept the failing tests as they are (recorded, for these commits only), or stop."))}
    blame = {r.case_id: blamed_repos(state, r.repo) for r in failing}
    by_repo: dict[str, list[Any]] = {}  # repo → the failing cases sent to its coder
    for r in failing:
        for repo in blame[r.case_id]:
            by_repo.setdefault(repo, []).append(r)
    boot = outcome.boot_failure
    if failing or boot:
        if svc.mode == "advisory":
            listed = bullets([case_line(r) for r in failing]) or (boot or ("", ""))[1]
            deps.notifier.progress(key, f"Live tests (advisory, not blocking): {counts_words(res)}."
                                   f"\n\n{listed}", "reviewer", important=True)  # fmt: skip
            return {**update, "last_event": {"type": "acceptance_passed", "advisory": True}}
        attempts = (state.get("acceptance_attempts") or 0) + 1
        limit = deps.config.limits.max_acceptance_attempts
        listed = bullets([case_line(r) for r in failing])
        if boot:
            listed = (f"- {boot[0]} doesn't start with the new code\n" + listed).strip()
        if attempts > limit:
            return {**update, "acceptance_attempts": attempts, **halt("acceptance", (
                f"The live acceptance tests still fail after {plural(limit, 'fix', 'fixes')}:"
                f"\n\n{listed}\n\nGive guidance, accept it as it is (recorded, for these "
                "commits only), or stop."))}  # fmt: skip
        if boot:
            by_repo.setdefault(boot[0], [])
        for repo, results in by_repo.items():
            md = failures_md(repo, results, blame)
            if boot and boot[0] == repo:
                md += f"\n\n## {repo} doesn't start\n\n```\n{boot[1][-4000:]}\n```\n"
            problems = [f"live test {case_line(r)}"
                        + (f" (called {r.repo})" if r.repo != repo else "") for r in results]
            if boot and boot[0] == repo:
                problems.insert(0, f"{repo} doesn't start with the new code")
            failed = VerifyResult(repo=repo, ok=False, checks={"acceptance": "failed"},
                                  output_tail={"acceptance": md[-20_000:]},
                                  problems=problems)  # fmt: skip
            work_all[repo] = {**work_all[repo], "status": "pending", "verify_result": failed,
                              "awaiting_verify": False, "verify_attempts": 0}  # fmt: skip
        from pipeline.testing.service import reason_key

        deps.store.set_note(key, FAILED_NOTE, {r.case_id: reason_key(r.reason) for r in failing})
        found = counts_words(res)
        everywhere = bool(failing) and not boot and all(len(v) > 1 for v in blame.values())
        deps.store.set_note(key, BLAME_NOTE, sorted(by_repo) if everywhere else [])
        who = (f"the coders of {join_words(sorted(by_repo))} (each fixes it only if the cause is "
               "in its own code)" if everywhere else "the Coder")  # fmt: skip
        deps.notifier.progress(key, f"The live acceptance tests found problems ({found}); "
                               f"{who} {'are' if everywhere else 'is'} looking at them:"
                               f"\n\n{listed}", "reviewer")  # fmt: skip
        return {**update, "repos": work_all, "acceptance_attempts": attempts,
                "last_event": {"type": "acceptance_failed", "repos": sorted(by_repo)}}  # fmt: skip

    waiting_on = [r for r in outcome.blocked if "migration" in r.reason]
    if outcome.pending and waiting_on:
        return {**update, **_wait_for_migrations(state, deps, outcome)}
    deps.store.set_note(key, FAILED_NOTE, {})
    deps.notifier.live_tests(key, live_line(res), f"cleanup: deleted "
                             f"{res.cleanup.get('deleted', 0)} test rows, "
                             f"{res.cleanup.get('left', 0)} left")  # fmt: skip
    return {**update, "acceptance_attempts": 0, "last_event": {"type": "acceptance_passed"}}


def migrate_commands(deps: Any, key: str, pending: list[Any]) -> str:
    repos = sorted({m.repo for m in pending})
    by_hand = "\n".join(
        f"cd {deps.workspaces.worktree_path(key, r)} && NODE_ENV=development "
        "npx sequelize-cli db:migrate   # with the dev DB variables of your clone's .env"
        for r in repos
    )  # fmt: skip
    return (f"In a terminal: `pipeline migrate {key}` (it lists them and asks first), or by hand:"
            f"\n\n```\n{by_hand}\n```")  # fmt: skip


def _wait_for_migrations(state: TicketState, deps: Any, outcome: Any) -> dict:
    key = state["key"]
    items = bullets([f"`{m.name}` ({m.repo})" + (f": {', '.join(m.tables)}" if m.tables else "")
                     for m in outcome.pending])  # fmt: skip
    blocked = [r for r in outcome.blocked if "migration" in r.reason]
    text = (f"{key} adds database migrations that aren't on the dev database yet. I've tested "
            f"everything else ({counts_words(outcome.result)}); "
            f"{plural(len(blocked), 'case')} wait for them:\n\n{items}\n\nApply them on dev: "
            f"{migrate_commands(deps, key, outcome.pending)}\n\nI check the dev database every "
            f"few minutes and carry on by myself; `test {key}` checks now.")  # fmt: skip
    card = Card(title=f"{key} waits for a migration on the dev database",
                facts=[("Migrations", ", ".join(m.name for m in outcome.pending))],
                sections=[text],
                actions=[Action("Test again", f"test {key}", style="positive"),
                         Action("Stop", f"stop {key} --confirm", style="destructive")])  # fmt: skip
    status = deps.config.jira.statuses.blocked
    try:
        deps.jira.transition(key, status)
    except IntegrationError as err:
        get_logger(ticket=key, node="acceptance").warning("jira_transition_failed",
                                                          error=str(err))  # fmt: skip
    msg = deps.notifier.waiting(key, MIGRATION_GATE, card.to_dict(), text=text)
    deps.store.upsert_ticket(key, waiting_for=MIGRATION_GATE, jira_status=status)
    pending = [{"repo": m.repo, "name": m.name, "tables": m.tables} for m in outcome.pending]
    return {"waiting_for": MIGRATION_GATE, "gate_message_id": msg,
            "last_event": {"type": "migration_wait", "pending": pending}}  # fmt: skip


def migration_wait(state: TicketState, deps: Any) -> dict:
    key = state["key"]
    payload = interrupt({"gate": MIGRATION_GATE, "message_id": state.get("gate_message_id")})
    if not isinstance(payload, dict) or payload.get("gate") != MIGRATION_GATE \
            or payload.get("action") not in MIGRATION_ACTIONS:  # fmt: skip
        get_logger(ticket=key, node="migration_wait").warning("stale_resume", payload=payload)
        return {"waiting_for": MIGRATION_GATE}
    action = payload["action"]
    by = str(payload.get("by") or "someone")
    msg = state.get("gate_message_id")
    deps.store.record_event(key, "resume", node="migration_wait", data={"action": action,
                                                                        "by": by})  # fmt: skip
    deps.store.upsert_ticket(key, waiting_for=None)
    if action == "stop":
        deps.notifier.gate_passed(key, MIGRATION_GATE, by, f"{by} stopped the ticket.", msg)
        return {"waiting_for": None, "gate_message_id": None,
                **halt("migration_wait", f"Stopped by {by}.")}  # fmt: skip
    words = ("The migrations are on the dev database now, so I'm running the remaining live "
             "tests." if action == "migration_applied"
             else f"{by} asked me to run the live tests again.")  # fmt: skip
    expected = expected_status("acceptance", deps, state)
    if expected:
        deps.jira.transition(key, expected)
        deps.store.upsert_ticket(key, jira_status=expected)
    deps.notifier.gate_passed(key, MIGRATION_GATE, by, words, msg)
    return {"waiting_for": None, "gate_message_id": None,
            "last_event": {"type": "migration_resume", "action": action, "by": by}}  # fmt: skip


def accept_failures(state: TicketState, deps: Any, by: str) -> dict:
    """`accept` at an acceptance stop: pinned to the tested commits (a new commit tests again)."""
    repos = service(deps).repos(state)
    failing = [case_line(r) for w in (state.get("repos") or {}).values()
               if (res := w.get("acceptance_result")) is not None
               for r in res.results if r.status == "failed"]  # fmt: skip
    override = {"by": by, "at": deps.clock().isoformat(timespec="seconds"), "failing": failing,
                "heads": _heads(state, deps, repos)}  # fmt: skip
    deps.store.record_event(state["key"], "gate_pass", node="needs_human",
                            data={"acceptance_override": override})  # fmt: skip
    return override
