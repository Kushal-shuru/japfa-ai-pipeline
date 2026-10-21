"""Assemble the ticket graph (Phases 3, 5, 6: ends at RFT). Thread id = Jira key.

plan approval → env_check (P10: can it be tested live?) → code_next_repo/verify ⟲ → acceptance
(P10: live tests; failures → code_next_repo, a pending migration → migration_wait ⏸) →
open_mrs → review_gate → review → (approve | fix_gate → fix → code_next_repo/verify (fix mode)
→ push_fixes → re_review_gate → review); `accept` at the fix gate → approve.
approve → deploy_doc_gate → deploy_doc → wait_merge ⟲ (poller snapshots; `change` → change,
`deploy-doc` → deploy_doc, people's commits → review_gate) → all merged → post_qa_notes (RFT;
DevOps works before the merges, D124) → wait_qa ⟲ (bug tickets, D206) → qa_done → END, or
`fix 1 2` → qa_fix → plan (qa_fix) → the same loop → post_qa_notes ("Resolved: 1, 2")
(D121: the note follows the approval).

Local review (D207, `git.review_mode: local`, the default): acceptance → start_review (no push, no
merge request) → review_gate → … → approve → publish_mrs (rebase, push, open the merge requests,
the review summary, the approval) → deploy_doc_gate. `gitlab` keeps open_mrs first.

All edges are conditional: every router first sends a halted state (`halted_reason` set by a
node or the wrapper) to `needs_human`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.graph import END, START, StateGraph

from pipeline.graph.nodes.change import change
from pipeline.graph.nodes.coding import code_next_repo, verify
from pipeline.graph.nodes.common import is_local, pending_repos
from pipeline.graph.nodes.context import load_context
from pipeline.graph.nodes.deploy import deploy_doc, deploy_doc_gate, deploy_doc_gate_wait
from pipeline.graph.nodes.fixing import (
    fix,
    fix_active,
    fix_gate,
    fix_gate_wait,
    push_fixes,
    re_review_gate,
    re_review_gate_wait,
)
from pipeline.graph.nodes.human import RETRY_NODES, needs_human, needs_human_wait
from pipeline.graph.nodes.mrs import open_mrs, publish_mrs, start_review
from pipeline.graph.nodes.planning import plan, plan_gate, plan_gate_wait, replan
from pipeline.graph.nodes.qa import post_qa_notes, qa_done, qa_fix, wait_qa
from pipeline.graph.nodes.review import approve, review, review_gate, review_gate_wait
from pipeline.graph.nodes.testing import MIGRATION_GATE, acceptance, env_check, migration_wait
from pipeline.graph.nodes.waits import wait_merge
from pipeline.graph.state import TicketState
from pipeline.graph.wrapper import wrap

NH = "needs_human"


def _event(state: TicketState) -> dict:
    return state.get("last_event") or {}


def routed(fn: Callable[[TicketState], str]) -> Callable[[TicketState], str]:
    def router(state: TicketState) -> str:
        return NH if state.get("halted_reason") else fn(state)

    return router


@routed
def after_load_context(state: TicketState) -> str:
    return END if _event(state).get("type") == "context_missing" else "plan"


@routed
def after_plan(state: TicketState) -> str:
    return "plan_gate"


@routed
def after_plan_gate(state: TicketState) -> str:
    return "plan_gate_wait" if state.get("waiting_for") == "plan_approval" else "env_check"


@routed
def after_plan_gate_wait(state: TicketState) -> str:
    if state.get("waiting_for") == "plan_approval":  # stale resume: keep waiting
        return "plan_gate_wait"
    return "plan" if _event(state).get("action") == "revise_plan" else "env_check"


@routed
def after_env_check(state: TicketState) -> str:
    return "code_next_repo"


def _coded(state: TicketState) -> str:
    """Once every repo is coded and checked: the live acceptance tests (P10) first."""
    return "acceptance"


@routed
def after_acceptance(state: TicketState) -> str:
    """Failures → the coder again; a migration not on the dev DB → wait for it; else a review
    fix pass pushes its fixes to the open MRs, the first pass opens them."""
    kind = _event(state).get("type")
    if kind == "acceptance_failed":
        return "code_next_repo"
    if kind == "migration_wait":
        return "migration_wait"
    if fix_active(state):
        return "push_fixes"
    return "start_review" if is_local(state) else "open_mrs"


@routed
def after_migration_wait(state: TicketState) -> str:
    return "migration_wait" if state.get("waiting_for") == MIGRATION_GATE else "acceptance"


@routed
def after_code(state: TicketState) -> str:
    kind = _event(state).get("type")
    if kind == "deviation":
        return "replan"
    if kind == "all_coded" or not pending_repos(state):
        return _coded(state)
    return "verify"


@routed
def after_verify(state: TicketState) -> str:
    return "code_next_repo" if pending_repos(state) else _coded(state)


@routed
def after_replan(state: TicketState) -> str:
    return "code_next_repo"


def _resync_failed(state: TicketState) -> bool:
    """D125: after the rebase before a push the checks failed → back to the coder."""
    return _event(state).get("type") == "resync_failed"


@routed
def after_open_mrs(state: TicketState) -> str:
    return "code_next_repo" if _resync_failed(state) else "review_gate"


@routed
def after_start_review(state: TicketState) -> str:
    return "review_gate"


@routed
def after_publish_mrs(state: TicketState) -> str:
    """D207: after the approval, the branches are rebased and pushed. A failed check goes back to
    the coder; code that changed in the rebase goes back to the review."""
    if _resync_failed(state):
        return "code_next_repo"
    return "review_gate" if _event(state).get("type") == "stale_review" else "deploy_doc_gate"


def _gate_router(gate: str, wait_node: str, passed: str) -> Callable[[TicketState], str]:
    """`<gate>` and `<gate>_wait` both go to the wait node while the gate waits (a stale
    resume waits again), else on; a person's `change` request goes to `change` (§4b)."""

    @routed
    def router(state: TicketState) -> str:
        if state.get("waiting_for") == gate:
            return wait_node
        return "change" if _event(state).get("action") == "change" else passed

    return router


after_review_gate = _gate_router("review", "review_gate_wait", "review")
after_re_review_gate = _gate_router("re_review", "re_review_gate_wait", "review")


@routed
def route_review(state: TicketState) -> str:
    """All approve → approve; changes requested → fix gate (the round limit and repeated
    disputes halt inside `review`, so they reach needs_human through `routed`)."""
    return "approve" if _event(state).get("approved") else "fix_gate"


@routed
def after_fix_gate(state: TicketState) -> str:
    if state.get("waiting_for") == "fix":
        return "fix_gate_wait"
    action = _event(state).get("action")
    return {"accept": "approve", "change": "change"}.get(action or "", "fix")


@routed
def after_change(state: TicketState) -> str:
    """Small → the fix gate (the findings card confirms); contract-affecting → plan approval."""
    return "plan_gate" if _event(state).get("type") == "change_replan" else "fix_gate"


@routed
def after_fix(state: TicketState) -> str:
    return "re_review_gate" if _event(state).get("type") == "nothing_to_fix" else "code_next_repo"


@routed
def after_push_fixes(state: TicketState) -> str:
    return "code_next_repo" if _resync_failed(state) else "re_review_gate"


@routed
def after_deploy_doc_gate(state: TicketState) -> str:
    return "deploy_doc_gate_wait" if state.get("waiting_for") == "deploy_doc" else "deploy_doc"


@routed
def after_deploy_doc(state: TicketState) -> str:
    return "wait_merge"


@routed
def after_approve(state: TicketState) -> str:
    """P9: an MR moved since the review → review again (the gate says whether a person starts
    it) instead of approving a commit nobody reviewed."""
    if _event(state).get("type") == "stale_review":
        return "review_gate"
    return "publish_mrs" if _event(state).get("type") == "approved_local" else "deploy_doc_gate"


@routed
def after_wait_merge(state: TicketState) -> str:
    ev = _event(state)
    if ev.get("type") == "gate":  # a person's command during the wait
        return {"change": "change", "deploy_doc": "deploy_doc",
                "fix": "fix"}.get(ev.get("action") or "", "wait_merge")  # fmt: skip
    if ev.get("type") == "human_commits":
        return "review_gate"
    return "post_qa_notes" if ev.get("type") == "all_merged" else "wait_merge"


@routed
def after_post_qa_notes(state: TicketState) -> str:
    return "wait_qa"


@routed
def after_wait_qa(state: TicketState) -> str:
    ev = _event(state)
    if ev.get("type") == "qa_passed":
        return "qa_done"
    return "qa_fix" if ev.get("type") == "gate" and ev.get("action") == "fix_bugs" else "wait_qa"


@routed
def after_qa_fix(state: TicketState) -> str:
    return "plan"


def after_needs_human_wait(state: TicketState) -> str:
    ev = _event(state)
    if ev.get("type") == "stopped":
        return END
    if ev.get("type") in ("retry", "guide", "accept") and not state.get("halted_reason"):
        return ev["node"]
    return "needs_human_wait"


RETRY_TARGETS = sorted(RETRY_NODES)


def build_graph(deps: Any, checkpointer: Any = None) -> Any:
    g: StateGraph = StateGraph(TicketState)

    def add(name: str, fn: Callable[[TicketState, Any], dict], guards: bool = True) -> None:
        g.add_node(name, wrap(name, fn, deps, guards))

    add("load_context", load_context)
    add("plan", plan)
    add("plan_gate", plan_gate)
    add("plan_gate_wait", plan_gate_wait)
    add("env_check", env_check)
    add("code_next_repo", code_next_repo)
    add("verify", verify)
    add("replan", replan)
    add("acceptance", acceptance)
    add("migration_wait", migration_wait)
    add("open_mrs", open_mrs)
    add("start_review", start_review)
    add("publish_mrs", publish_mrs)
    add("review_gate", review_gate)
    add("review_gate_wait", review_gate_wait)
    add("review", review)
    add("fix_gate", fix_gate)
    add("fix_gate_wait", fix_gate_wait)
    add("fix", fix)
    add("push_fixes", push_fixes)
    add("re_review_gate", re_review_gate)
    add("re_review_gate_wait", re_review_gate_wait)
    add("deploy_doc_gate", deploy_doc_gate)
    add("deploy_doc_gate_wait", deploy_doc_gate_wait)
    add("deploy_doc", deploy_doc)
    add("approve", approve)
    add("wait_merge", wait_merge)
    add("post_qa_notes", post_qa_notes)
    add("wait_qa", wait_qa)
    add("qa_fix", qa_fix)
    add("qa_done", qa_done)
    add("change", change)
    add(NH, needs_human, guards=False)
    add("needs_human_wait", needs_human_wait, guards=False)

    g.add_edge(START, "load_context")
    g.add_conditional_edges("load_context", after_load_context, ["plan", NH, END])
    g.add_conditional_edges("plan", after_plan, ["plan_gate", NH])
    g.add_conditional_edges("plan_gate", after_plan_gate, ["plan_gate_wait", "env_check", NH])
    g.add_conditional_edges(
        "plan_gate_wait", after_plan_gate_wait, ["plan_gate_wait", "plan", "env_check", NH]
    )
    g.add_conditional_edges("env_check", after_env_check, ["code_next_repo", NH])
    g.add_conditional_edges("code_next_repo", after_code, ["verify", "replan", "acceptance", NH])
    g.add_conditional_edges("verify", after_verify, ["code_next_repo", "acceptance", NH])
    g.add_conditional_edges(
        "acceptance", after_acceptance,
        ["code_next_repo", "migration_wait", "open_mrs", "start_review", "push_fixes", NH],
    )  # fmt: skip
    g.add_conditional_edges("migration_wait", after_migration_wait,
                            ["migration_wait", "acceptance", NH])  # fmt: skip
    g.add_conditional_edges("replan", after_replan, ["code_next_repo", NH])
    g.add_conditional_edges("open_mrs", after_open_mrs, ["review_gate", "code_next_repo", NH])
    g.add_conditional_edges("start_review", after_start_review, ["review_gate", NH])
    g.add_conditional_edges("publish_mrs", after_publish_mrs,
                            ["deploy_doc_gate", "review_gate", "code_next_repo", NH])  # fmt: skip
    g.add_conditional_edges(
        "review_gate", after_review_gate, ["review_gate_wait", "review", "change", NH]
    )
    g.add_conditional_edges(
        "review_gate_wait", after_review_gate, ["review_gate_wait", "review", "change", NH]
    )
    g.add_conditional_edges("review", route_review, ["approve", "fix_gate", NH])
    g.add_conditional_edges(
        "fix_gate", after_fix_gate, ["fix_gate_wait", "fix", "approve", "change", NH]
    )
    g.add_conditional_edges(
        "fix_gate_wait", after_fix_gate, ["fix_gate_wait", "fix", "approve", "change", NH]
    )
    g.add_conditional_edges("change", after_change, ["fix_gate", "plan_gate", NH])
    g.add_conditional_edges("fix", after_fix, ["code_next_repo", "re_review_gate", NH])
    g.add_conditional_edges("push_fixes", after_push_fixes,
                            ["re_review_gate", "code_next_repo", NH])  # fmt: skip
    g.add_conditional_edges(
        "re_review_gate", after_re_review_gate, ["re_review_gate_wait", "review", "change", NH]
    )
    g.add_conditional_edges(
        "re_review_gate_wait",
        after_re_review_gate,
        ["re_review_gate_wait", "review", "change", NH],
    )
    g.add_conditional_edges(
        "deploy_doc_gate", after_deploy_doc_gate, ["deploy_doc_gate_wait", "deploy_doc", NH]
    )
    g.add_conditional_edges(
        "deploy_doc_gate_wait", after_deploy_doc_gate, ["deploy_doc_gate_wait", "deploy_doc", NH]
    )
    g.add_conditional_edges("deploy_doc", after_deploy_doc, ["wait_merge", NH])
    g.add_conditional_edges("approve", after_approve,
                            ["deploy_doc_gate", "publish_mrs", "review_gate", NH])  # fmt: skip
    g.add_conditional_edges(
        "wait_merge",
        after_wait_merge,
        ["wait_merge", "post_qa_notes", "review_gate", "change", "deploy_doc", "fix", NH],
    )
    g.add_conditional_edges("post_qa_notes", after_post_qa_notes, ["wait_qa", NH])
    g.add_conditional_edges("wait_qa", after_wait_qa, ["wait_qa", "qa_done", "qa_fix", NH])
    g.add_conditional_edges("qa_fix", after_qa_fix, ["plan", NH])
    g.add_edge("qa_done", END)
    g.add_edge(NH, "needs_human_wait")
    g.add_conditional_edges(
        "needs_human_wait", after_needs_human_wait, [*RETRY_TARGETS, "needs_human_wait", END]
    )
    return g.compile(checkpointer=checkpointer)
