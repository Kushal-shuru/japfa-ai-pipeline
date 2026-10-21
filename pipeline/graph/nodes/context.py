"""`load_context`: Jira + repos + spec + ACs. Missing pieces → explain and END; else In Dev."""

from __future__ import annotations

from typing import Any

from pipeline.context import build_context, context_ok
from pipeline.graph.nodes.common import bullets
from pipeline.graph.state import RepoWork, TicketState
from pipeline.intake import intake_of
from pipeline.workspace import branch_name


def load_context(state: TicketState, deps: Any) -> dict:
    key = state["key"]
    by = state.get("started_by") or "someone"
    problems: list[str] = []
    ctx = build_context(key, deps.jira, deps.spec, deps.config, problems,
                        intake_of(deps.store, key))  # fmt: skip
    ok, more = context_ok(ctx, deps.config)
    problems += more
    deps.chat.ensure_thread(key)

    if not ok:
        deps.notifier.error(
            key, f"I can't start {key} yet:\n\n{bullets(problems)}\n\nFix that and tell me "
            "when it's done, and I'll pick it up.",
        )  # fmt: skip
        deps.store.upsert_ticket(
            key, repos=ctx.repos, started_by=by, stage="context_missing",
            finished_at=deps.clock(), waiting_for=None,
        )  # fmt: skip
        return {"context": ctx, "last_event": {"type": "context_missing", "problems": problems}}

    status = deps.config.jira.statuses.in_dev
    deps.jira.transition(key, status)
    deps.store.upsert_ticket(
        key, repos=ctx.repos, started_by=by, jira_status=status, halted_reason=None,
        finished_at=None, waiting_for=None,
    )  # fmt: skip
    deps.notifier.started(key, by, ctx.repos, ctx.summary)
    if state.get("testing") is False:
        deps.notifier.testing_off(key, by)

    branch = branch_name(key, ctx.summary)
    ws = deps.workspaces
    repos: dict[str, RepoWork] = {}
    for n, name in enumerate(ctx.repos, 1):
        repos[name] = {
            "repo": name, "role": deps.config.repo(name).role, "order": n, "status": "pending",
            "branch": branch, "worktree": str(ws.worktree_path(key, name)), "mr_iid": None,
            "mr_url": None, "coder_result": None, "verify_result": None, "review_result": None,
            "devops_facts": None, "verify_attempts": 0,
        }  # fmt: skip
    return {
        "context": ctx,
        "repos": repos,
        "plan": None,
        "plan_feedback": [],
        "plan_revisions": 0,
        "replans": 0,
        "review_round": 0,
        "qa_round": 0,
        "waiting_for": None,
        "gate_message_id": None,
        "halted_reason": None,
        "bugs": [],
        "review_mode": deps.config.git.review_mode,
        "review_log": [],
        "preferred_slots": state.get("preferred_slots") or {},
        "total_cost_usd": deps.store.ticket_cost(key),
        "last_event": {"type": "context_ok"},
    }
