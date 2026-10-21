"""`TicketState` — the LangGraph state of one ticket thread (CLAUDE.md §7, verbatim)."""
# ruff: noqa: E501
# fmt: off

from __future__ import annotations

from typing import Literal, TypedDict

from pipeline.schemas import (
    AcceptanceResult,
    CoderResult,
    DeployResult,
    DevopsFacts,
    EnvReport,
    ImplementationPlan,
    Provider,
    RepoRole,
    ReviewResult,
    TicketContext,
    VerifyResult,
)

GateName = Literal["plan_approval","review","fix","re_review","deploy_doc","qa_fix"]

class RepoWork(TypedDict, total=False):
    repo: str
    role: RepoRole
    order: int
    status: Literal["pending","coded","mr_open","changes_requested","approved","merged"]
    branch: str
    worktree: str
    mr_iid: int | None
    mr_url: str | None
    coder_result: CoderResult | None
    verify_result: VerifyResult | None
    review_result: ReviewResult | None
    devops_facts: DevopsFacts | None
    verify_attempts: int
    awaiting_verify: bool                # coded in a parallel batch, not verified yet (D129)
    reviewed_sha: str | None             # [P9] the commit the last review saw; approval needs it unchanged
    acceptance_result: AcceptanceResult | None   # [P10] the last live acceptance run for this repo

class TicketState(TypedDict, total=False):
    key: str
    started_by: str
    context: TicketContext | None
    plan: ImplementationPlan | None
    plan_feedback: list[str]
    plan_revisions: int
    repos: dict[str, RepoWork]
    replans: int
    review_round: int
    qa_round: int
    contract_review: ReviewResult | None
    fix_scope: dict | None               # {"mode": "major+"|"minor"|"all"|"blockers"|"ids", "ids": [...]}
    override: dict | None                # {"by": name, "at": ts, "open_findings": [...]}
    deploy_result: DeployResult | None
    waiting_for: GateName | Literal["merge_all","devops_done","qa","migration"] | None   # migration [P10]
    gate_message_id: str | None          # card to update when the gate is passed
    provider_override: Provider | None
    preferred_slots: dict[str, str]      # pool → slot name (sticky)
    bugs: list[dict]
    guidance: list[dict]                 # [P4b] {by, at, text, step}: human guidance (`guide`)
    last_event: dict | None
    halted_reason: str | None
    total_cost_usd: float
    testing: bool                        # [P10] False (default) = no live tests; `implement KEY testing=true` = on (missing = on)
    env_report: EnvReport | None         # [P10] the preflight before coding
    acceptance_attempts: int             # [P10] live acceptance runs sent back to the coder
    review_mode: Literal["local", "gitlab"]   # [D207] set at the start from git.review_mode; missing (old tickets) = gitlab
    review_log: list[dict]               # [D207] per review round: {round, repo-tagged new / resolved / rejected finding lines}; goes into the MR note
    acceptance_override: dict | None     # [P10] {"by", "at", "failing": [...], "heads": {...}}: `accept` at an acceptance stop
