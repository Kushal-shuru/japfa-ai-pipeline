"""`plan`, the plan-approval gate (`plan_gate` + `plan_gate_wait`) and `replan`."""

from __future__ import annotations

from typing import Any

from pipeline.agents import planner
from pipeline.chat.cards import Action, Card
from pipeline.chat.summaries import error_words, join_words, plural
from pipeline.context import build_context, context_ok
from pipeline.criteria import extractor
from pipeline.graph.gates import gate_prepare, gate_wait
from pipeline.graph.nodes.common import (
    after_run,
    agent_slot,
    bullets,
    jira_url,
    ordered,
    reset_repo,
)
from pipeline.graph.state import RepoWork, TicketState
from pipeline.graph.wrapper import halt
from pipeline.intake import intake_of
from pipeline.schemas import ImplementationPlan


def _apply_tasks(state: TicketState, plan: ImplementationPlan) -> dict[str, RepoWork]:
    """Order/role from the plan's tasks; repos without a task leave the coding loop."""
    repos = state.get("repos") or {}
    out: dict[str, RepoWork] = {}
    for task in sorted(plan.tasks, key=lambda t: t.order):
        if task.repo in repos:
            out[task.repo] = {**repos[task.repo], "order": task.order, "role": task.role}
    return out


def _run_planner(state: TicketState, deps: Any, node: str, mode: str,
                 extra: dict | None) -> tuple[ImplementationPlan | None, dict]:  # fmt: skip
    key = state["key"]
    from pipeline.graph.nodes.qa import qa_branch

    extra = {**(extra or {}), "branch": qa_branch(state)}
    with agent_slot(state, deps, "planner") as slot:
        doing = {
            "initial": f"Working on a plan for {key}. This usually takes a few minutes.",
            "revise": f"Updating the plan for {key} with your feedback.",
            "replan": f"Updating the plan for {key} so the coding can continue.",
            "qa_fix": f"Planning the fixes for the QA bugs on {key}.",
        }  # fmt: skip
        deps.notifier.progress(key, doing.get(mode, doing["initial"]), "planner")
        result, run = planner.plan(
            state["context"], mode, deps, extra,
            provider_override=state.get("provider_override"), slot=slot.name,
        )  # fmt: skip
    update = after_run(state, deps, slot)
    if result is None:
        return None, {**update, **halt(node, f"The planning step failed: {error_words(run.error)}")}
    if mode != "replan" or result.status != "ready":  # a ready replan: only its changes, below
        deps.notifier.plan_ready(key, planner.render_plan_summary_md(result),
                                 revision=f"{mode}{state.get('plan_revisions', 0)}"
                                 f"r{state.get('replans', 0)}", plan=result)  # fmt: skip
    if result.status == "needs_clarification":
        reason = (
            "I need answers to the questions above before I can plan this. Just answer them "
            "here in the chat, or add the answers to the Jira ticket (for example under "
            "'Clarifications') and tell me to try again: I read the ticket and the spec again."
        )
        return None, {**update, **halt(node, reason)}
    return result, update


def refresh_context(state: TicketState, deps: Any, node: str) -> tuple[Any, dict | None]:
    """Re-read the Jira ticket and spec, so answers added after a clarification (then `retry`)
    reach the planner. The repo set is fixed once the ticket has started."""
    key = state["key"]
    problems: list[str] = []
    ctx = build_context(key, deps.jira, deps.spec, deps.config, problems,
                        intake_of(deps.store, key), extractor(deps, key))  # fmt: skip
    ok, more = context_ok(ctx, deps.config)
    problems += more
    if not ok:
        return None, halt(node, f"The Jira ticket is no longer complete:\n\n{bullets(problems)}")
    before = state["context"].repos if state.get("context") else ctx.repos
    if set(ctx.repos) != set(before):
        reason = (
            f"The ticket's repositories changed ({', '.join(before)} → "
            f"{', '.join(ctx.repos)}). Stop it; a developer then runs `pipeline reset {key}` "
            f"and you can start it again."
        )
        return None, halt(node, reason)
    return ctx.model_copy(update={"repos": list(before)}), None


def plan(state: TicketState, deps: Any) -> dict:
    ctx, problem = refresh_context(state, deps, "plan")
    if problem is not None:
        return problem
    state = {**state, "context": ctx}
    # a QA round plans the chosen bugs (`qa_fix`) until people ask for changes to that plan
    qa = state.get("qa_round", 0) > 0 and not state.get("plan_revisions")
    mode = "initial" if state.get("plan") is None else "qa_fix" if qa else "revise"
    extra: dict = {"guidance": state.get("guidance") or []}
    if mode == "revise":
        extra.update(previous_plan=state["plan"], feedback=state.get("plan_feedback", []))
    elif mode == "qa_fix":
        from pipeline.bugs import in_round

        bugs = [{k: b.get(k) for k in ("n", "text", "by", "source", "ticket")}
                for b in in_round(state.get("bugs"), state.get("qa_round", 0))]  # fmt: skip
        extra.update(previous_plan=state["plan"], bugs=bugs)
    result, update = _run_planner(state, deps, "plan", mode, extra)
    update["context"] = ctx
    if result is None:
        return update
    repos = _apply_tasks(state, result)
    dropped = [r for r in state.get("repos") or {} if r not in repos]
    if dropped:
        them = "it" if len(dropped) == 1 else "them"
        deps.notifier.progress(state["key"], f"The plan doesn't need changes in "
                               f"{join_words(dropped)}, so I'm leaving {them} out.",
                               "planner")  # fmt: skip
    return {**update, "plan": result, "repos": repos, "last_event": {"type": "planned"}}


# ---------- plan approval gate ----------


def plan_card(state: TicketState, deps: Any) -> Card:
    """One line and a link: the plan itself is the Jira comment (D128)."""
    key = state["key"]
    plan_ = state["plan"]
    assert plan_ is not None
    revisions = state.get("plan_revisions", 0)
    repos = [t.repo for t in sorted(plan_.tasks, key=lambda t: t.order)]
    c = plan_.contract
    new = len([e for e in c.internal_endpoints if e.is_new]) + len(c.public_endpoints)
    line = (f"{plural(len(repos), 'repository', 'repositories')} ({' → '.join(repos)}), "
            f"{plural(new, 'new or changed endpoint')}.")  # fmt: skip
    if plan_.mode in ("change", "replan"):  # an update after coding started (D130)
        work = state.get("repos") or {}
        redo = [r for r in repos if (work.get(r) or {}).get("status") == "pending"]
        n = len(plan_.changes_from_previous)
        line = (f"Updated plan: {plural(n, 'change')}; redo "
                f"{join_words(redo) if redo else 'nothing'}, the rest stays as it is.")  # fmt: skip
    url = jira_url(deps, key)
    link = f"[View the plan on Jira]({url})" if url else "The plan is on the Jira ticket."
    left = max(deps.config.limits.max_plan_revisions - revisions, 0)
    return Card(
        title=f"{key}: plan ready for your approval"
        + (f" (version {revisions + 1})" if revisions else ""),
        sections=[line, link],
        facts=[("Changes you can still ask for", str(left))] if revisions else [],
        actions=[
            Action("Approve plan", f"approve {key}", style="positive"),  # D168
            Action("Request changes", f"guide {key}:", input_hint="<feedback>"),
            Action("Stop", f"stop {key} --confirm", style="destructive"),
        ],
    )


def plan_gate(state: TicketState, deps: Any) -> dict:
    return gate_prepare(
        "plan_approval", state, deps, plan_card, round=state.get("plan_revisions", 0)
    )


def plan_gate_wait(state: TicketState, deps: Any) -> dict:
    revisions = state.get("plan_revisions", 0)
    update = gate_wait(
        "plan_approval",
        state,
        deps,
        ("approve_plan", "revise_plan"),
        round=revisions,
        node="plan_gate_wait",
    )
    ev = update.get("last_event") or {}
    if ev.get("type") == "gate" and ev.get("action") == "revise_plan":
        revisions += 1
        feedback = str(ev.get("args", {}).get("feedback", "")).strip()
        update["plan_feedback"] = [*state.get("plan_feedback", []), feedback]
        update["plan_revisions"] = revisions
        limit = deps.config.limits.max_plan_revisions
        if revisions > limit:
            update.update(halt("plan_gate_wait", f"The plan has been changed the maximum number "
                                                 f"of times ({limit})."))  # fmt: skip
    return update


# ---------- replan (contract deviation) ----------


# wording, not behaviour: ignored when deciding whether the interface between services changed
WORDING_KEYS = frozenset({"description", "title", "note", "notes", "comment", "why", "reason",
                          "when", "example", "examples", "$comment"})  # fmt: skip


def _no_wording(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _no_wording(v) for k, v in value.items() if k not in WORDING_KEYS}
    if isinstance(value, list):
        return [_no_wording(v) for v in value]
    return value


def wire_view(endpoint: Any) -> dict:
    """What the other side of an endpoint actually sees (method, path, auth, request / response
    shapes, error codes and their mapping); descriptions and notes left out (D130)."""
    data = endpoint.model_dump(mode="json")
    data.pop("is_new", None)
    if "errors" in data:  # status + code matter; `when` is wording
        data["errors"] = sorted((e["status"], e["code"]) for e in data["errors"])
    if "consumer_repos" in data:
        data["consumer_repos"] = sorted(data["consumer_repos"])
    return _no_wording(data)


def contract_changed(old: ImplementationPlan, new: ImplementationPlan) -> bool:
    """True only when something a service or the gateway sees changed (not wording)."""

    def view(plan_: ImplementationPlan) -> dict:
        c = plan_.contract
        return {e.id: wire_view(e) for e in [*c.internal_endpoints, *c.public_endpoints]}

    return view(old) != view(new)


def _endpoint_dump(plan_: ImplementationPlan, ids: list[str]) -> dict:
    eps = {e.id: e for e in [*plan_.contract.internal_endpoints, *plan_.contract.public_endpoints]}
    return {i: wire_view(eps[i]) if i in eps else None for i in ids}


def changed_plan_md(plan_: ImplementationPlan, redo: list[str]) -> str:
    """A plan updated after coding started (change request, replan): only what changed and
    which repositories are redone; the rest of the plan stays as approved (D130)."""
    changes = plan_.changes_from_previous or ["(no changes were listed)"]
    out = ["**What changed**", "", bullets(changes), ""]
    if redo:
        out.append(f"**Redone:** {join_words(redo)}. Only these changes are applied; the rest "
                   "of the code stays as it is.")  # fmt: skip
    else:
        out.append("**Redone:** nothing; the code already matches.")
    return "\n".join(out)


def _affected(old: ImplementationPlan, new: ImplementationPlan, deviated: set[str]) -> set[str]:
    old_tasks = {t.repo: t for t in old.tasks}
    out = set(deviated)
    for t in new.tasks:
        before = old_tasks.get(t.repo)
        ids = [*t.implements, *t.consumes]
        changed = before is None or before.model_dump() != t.model_dump()
        if changed or _endpoint_dump(old, ids) != _endpoint_dump(new, ids):
            out.add(t.repo)
    return out


def replan(state: TicketState, deps: Any) -> dict:
    repos = state.get("repos") or {}
    deviations = [
        {"repo": r, "deviation": d}
        for r in ordered(state)
        if (res := repos[r].get("coder_result")) is not None
        for d in res.contract_deviations
    ]
    old = state["plan"]
    assert old is not None
    replans = state.get("replans", 0) + 1
    result, update = _run_planner(
        {**state, "replans": replans}, deps, "replan", "replan",
        {"previous_plan": old, "deviations": deviations,
         "guidance": state.get("guidance") or []},
    )  # fmt: skip
    update["replans"] = replans
    deps.store.upsert_ticket(state["key"], replans=replans)
    if result is None:
        return update
    affected = _affected(old, result, {d["repo"] for d in deviations})
    new_repos = _apply_tasks(state, result)
    for r in new_repos:
        if r in affected:
            new_repos[r] = reset_repo(new_repos[r])
    redo = [r for r in ordered({"repos": new_repos}) if r in affected]
    deps.notifier.plan_ready(state["key"], changed_plan_md(result, redo),
                             revision=f"replan{replans}", plan=result)  # fmt: skip
    deps.notifier.progress(state["key"], "I've updated the plan: " + (
        f"redoing only the changed parts of {join_words(redo)}." if redo
        else "nothing needs to be redone."), "planner")  # fmt: skip
    return {**update, "plan": result, "repos": new_repos, "last_event": {"type": "replanned"}}
