"""`change` (Phase 5 §4b, D87): a person's change request while the MRs are open.

`change DEMO-3: <request>` at the review, fix or re-review gate. The request is mirrored to Jira
and as a note on every MR, kept in `state.guidance` (step `change`, so later agents and the MR
history see it), and the planner (mode `change`, read-only) returns the whole plan with each
concrete change in `changes_from_previous` as `<repo>: <what>`. Then:

- **small** (the contract is unchanged): each change becomes a `human` finding
  (`H<n>-<repo>-F<k>`, numbered with the review findings) on that repo's MR → the fix gate: the
  findings card is the confirmation, then the normal fix path (verify, push, re-review);
- **contract-affecting**: Jira In Dev → the plan-approval card (a person's change requests don't
  count against `max_replans`; they share `max_plan_revisions` per ticket, checked by the bus
  before anything runs, D131)
  with the updated plan → re-code the affected repos → push (`open_mrs`) → the review gate.
"""

from __future__ import annotations

from typing import Any

from pipeline.agents import planner
from pipeline.chat.summaries import error_words, join_words, plural
from pipeline.findings import finding_md, numbers_by_id
from pipeline.graph.nodes.common import (
    after_run,
    agent_slot,
    bullets,
    has_mr,
    ordered,
    reset_repo,
)
from pipeline.graph.nodes.planning import (
    _affected,
    _apply_tasks,
    changed_plan_md,
    contract_changed,
)
from pipeline.graph.nodes.review import mr_of
from pipeline.graph.state import RepoWork, TicketState
from pipeline.graph.wrapper import halt
from pipeline.markers import make_marker
from pipeline.schemas import Finding, ReviewResult

STEP = "change"


def split_changes(changes: list[str], repos: list[str]) -> list[tuple[str, str]]:
    """ "demo-ws1-closio: return 204 …" → (repo, what). Entries without a known repo are
    dropped (the planner's validation asks for the prefix)."""
    out = []
    for entry in changes:
        repo, sep, what = entry.partition(":")
        if sep and repo.strip() in repos and what.strip():
            out.append((repo.strip(), what.strip()))
    return out


def _request(state: TicketState, deps: Any) -> tuple[dict, list[dict], bool]:
    """(the change entry, guidance, is_new). A retry after a failure reuses the last one."""
    ev = state.get("last_event") or {}
    guidance = list(state.get("guidance") or [])
    if ev.get("type") == "gate" and ev.get("action") == "change":
        entry = {"by": ev.get("by") or "someone",
                 "at": deps.clock().isoformat(timespec="seconds"),
                 "text": str((ev.get("args") or {}).get("text") or "").strip(),
                 "step": STEP, "repo": None, "request": True}  # fmt: skip
        return entry, [*guidance, entry], True
    # retry / guide after a failure: the request itself (guidance for this step has no flag)
    entry = next(g for g in reversed(guidance) if g.get("request"))
    return entry, guidance, False


def change(state: TicketState, deps: Any) -> dict:
    key = state["key"]
    entry, guidance, is_new = _request(state, deps)
    by, request = entry["by"], entry["text"]
    n = sum(1 for g in guidance if g.get("request"))
    limit = deps.config.limits.max_plan_revisions
    if is_new and n > limit:  # backstop: the bus refuses it before this (D131)
        return halt(STEP, f"This is change request {n} on {key}, over the limit of {limit} per "
                          "ticket. Create a new ticket for it, or stop.")  # fmt: skip
    repos = ordered(state)
    if is_new:  # 4b.1: mirrored to Jira and as a note on the MRs
        for repo in (r for r in repos if has_mr(state, r)):  # D207: none before the approval
            deps.gitlab.add_note(*mr_of(deps, state, repo),
                                 f"**Change requested by {by}:** {request}",
                                 make_marker(key, STEP, n, "request"))  # fmt: skip
        deps.notifier.change_requested(key, by, request, n)

    old = state["plan"]
    ctx = state["context"]
    assert old is not None and ctx is not None
    extra = {"previous_plan": old, "change": {"request": request, "by": by},
             "guidance": guidance}  # fmt: skip
    with agent_slot(state, deps, "planner") as slot:
        deps.notifier.progress(key, f"Looking at {by}'s change request for {key}.", "planner")
        result, run = planner.plan(ctx, STEP, deps, extra,
                                   provider_override=state.get("provider_override"),
                                   slot=slot.name)  # fmt: skip
    update = {**after_run(state, deps, slot), "guidance": guidance}
    if result is None:
        return {**update, **halt(STEP, f"Planning the change request failed: "
                                       f"{error_words(run.error)}")}  # fmt: skip
    if result.status == "needs_clarification":
        questions = bullets(result.open_questions)
        reason = (
            f"I have questions about your change before I can plan it:\n\n{questions}"
            "\n\nJust answer them here, in your own words."
        )
        return {**update, **halt(STEP, reason)}

    if contract_changed(old, result):  # what the other side sees; wording doesn't count
        return {**update, **_contract_change(state, deps, result, entry, n)}
    return {**update, **_small_change(state, deps, result, entry, n)}


def _contract_change(state: TicketState, deps: Any, result: Any, entry: dict, n: int) -> dict:
    key = state["key"]
    old = state["plan"]
    assert old is not None
    affected = _affected(old, result, set())
    new_repos: dict[str, RepoWork] = _apply_tasks(state, result)
    for r in new_repos:
        if r in affected:
            new_repos[r] = reset_repo(new_repos[r])
    status = deps.config.jira.statuses.in_dev
    deps.jira.transition(key, status)
    deps.store.upsert_ticket(key, jira_status=status)
    redo = [r for r in ordered({"repos": new_repos}) if r in affected]
    deps.notifier.progress(
        key, "This change affects the agreed interface between the services, so I've updated "
        "the plan: " + (f"only the changed parts of {join_words(redo)} will be redone" if redo
                        else "no code needs to change") + ". Please approve it before I change "
        "the code.", "planner",
    )  # fmt: skip
    deps.notifier.plan_ready(key, changed_plan_md(result, redo), revision=f"change{n}",
                             plan=result)  # fmt: skip
    return {"plan": result, "repos": new_repos,
            "last_event": {"type": "change_replan", "n": n}}  # fmt: skip


def _small_change(state: TicketState, deps: Any, result: Any, entry: dict, n: int) -> dict:
    key = state["key"]
    by, request = entry["by"], entry["text"]
    round_ = state.get("review_round", 0)
    items = split_changes(result.changes_from_previous, ordered(state))
    repos: dict[str, RepoWork] = {r: dict(w) for r, w in state["repos"].items()}  # type: ignore[misc]
    added: list[Finding] = []
    per_repo: dict[str, int] = {}
    for repo, what in items:
        per_repo[repo] = per_repo.get(repo, 0) + 1
        f = Finding(
            id=f"H{n}-{repo}-F{per_repo[repo]}",
            repo=repo,
            severity="human",
            category="change request",
            comment=f"{what[:1].upper()}{what[1:].rstrip('.')}. Requested by {by}: “{request}”",
        )
        added.append(f)  # fmt: skip
        work = repos[repo]
        res = work.get("review_result") or ReviewResult(scope=repo, verdict="changes_requested",
                                                         summary="", findings=[])  # fmt: skip
        work["review_result"] = res.model_copy(update={
            "findings": [*res.findings, f], "verdict": "changes_requested"})  # fmt: skip
        work["status"] = "changes_requested"
    if not added:
        return halt(STEP, "I couldn't work out what exactly to change in the code for your "
                          "request. Tell me in a bit more detail what should change, or stop "
                          "the ticket.")  # fmt: skip
    numbers = numbers_by_id({**state, "repos": repos})
    for f in (f for f in added if has_mr(state, f.repo)):
        deps.gitlab.add_inline(*mr_of(deps, state, f.repo), "", 0,
                               finding_md(numbers.get(f.id), f, inline=False),
                               make_marker(key, "review", round_, f.id))  # fmt: skip
    refs = join_words([f"#{numbers[f.id]} in {f.repo}" for f in added])
    deps.notifier.progress(
        key, f"This change fits inside the agreed interface, so I turned it into "
        f"{plural(len(added), 'change')} for the coder: {refs}. Choose what to fix on the card "
        "to go ahead.", "planner",
    )  # fmt: skip
    return {"plan": result, "repos": repos, "last_event": {"type": "change_small", "n": n}}
