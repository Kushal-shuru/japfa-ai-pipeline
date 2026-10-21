"""Helpers shared by nodes: agent slots (queueing message, stop), sticky slots, cost, repos."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from pipeline.agents.pool import Slot
from pipeline.graph.state import RepoWork, TicketState
from pipeline.graph.wrapper import stop_requested

POOL_WORDS = {"coder": "coders", "reviewer": "reviewers", "deploy": "deployment writers"}


@contextmanager
def agent_slot(state: TicketState, deps: Any, role: str) -> Iterator[Slot]:
    """Hold a slot of the role's pool while the agent runs (§3.7). Posts the queue position
    once if the ticket has to wait; gives up if the ticket is stopped while queued."""
    key = state["key"]
    pool = deps.pool.pool_for(role)
    preferred = (state.get("preferred_slots") or {}).get(pool)

    def queued(position: int) -> None:
        where = "next in line" if position == 1 else f"number {position} in line"
        deps.notifier.progress(key, f"All {POOL_WORDS[pool]} are busy right now; {key} is "
                                    f"{where}.")  # fmt: skip

    # one provider per run (D129): without an override, only slots of the configured provider;
    # an override (`--provider`) decides the provider whatever slot the run holds
    provider = None if state.get("provider_override") else deps.config.resolve_provider(role)
    with deps.pool.slot(
        key, role, preferred, on_queued=queued,
        cancelled=lambda: bool(stop_requested(deps, key)), provider=provider,
    ) as slot:  # fmt: skip
        yield slot


def provider_of(state: TicketState, slot: Slot) -> str:
    return state.get("provider_override") or slot.provider


def after_run(state: TicketState, deps: Any, slot: Slot) -> dict:
    """Sticky slot preference (4.3) + reported cost so far."""
    return {
        "preferred_slots": {**(state.get("preferred_slots") or {}), slot.pool: slot.name},
        "total_cost_usd": deps.store.ticket_cost(state["key"]),
    }


def ordered(state: TicketState) -> list[str]:
    repos = state.get("repos") or {}
    return sorted(repos, key=lambda r: repos[r].get("order", 99))


def pending_repos(state: TicketState) -> list[str]:
    repos = state.get("repos") or {}
    return [r for r in ordered(state) if repos[r].get("status") == "pending"]


def is_local(state: Any) -> bool:
    """D207: this ticket reviews locally; its merge requests open after the approval. Tickets
    started before the flag existed have no `review_mode` and keep the GitLab flow."""
    return (state or {}).get("review_mode") == "local"


def has_mr(state: Any, repo: str) -> bool:
    return bool(((state.get("repos") or {}).get(repo) or {}).get("mr_iid"))


def review_diff(deps: Any, state: Any, repo: str) -> str:
    """What the reviewer reads for `repo`: the merge request's diff once it exists, else the
    branch against its default branch, from git (D207)."""
    work = state["repos"][repo]
    cfg = deps.config.repo(repo)
    if work.get("mr_iid"):
        return deps.gitlab.mr_diff(cfg.gitlab_project, work["mr_iid"])
    return deps.workspaces.diff_vs_default(deps.workspaces.worktree_path(state["key"], repo),
                                           cfg.default_branch)


def reset_repo(work: RepoWork) -> RepoWork:
    return {**work, "status": "pending", "coder_result": None, "verify_result": None,
            "verify_attempts": 0, "awaiting_verify": False}  # fmt: skip


def bullets(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items)


def board_id(state: Any) -> str:
    """One progress card per coding pass: the initial one, one per QA round, and one per
    review-fix pass (P5)."""
    scope = state.get("fix_scope") or {}
    fix = f"f{state.get('review_round', 0)}" if scope.get("active") else ""
    return f"q{state.get('qa_round', 0)}{fix}"


def jira_url(deps: Any, key: str) -> str:
    """Browser link to the ticket ('' without a Jira base URL)."""
    secrets = getattr(deps, "secrets", None)
    base = (getattr(secrets, "jira_base_url", None) or "").rstrip("/")
    return f"{base}/browse/{key}" if base else ""
