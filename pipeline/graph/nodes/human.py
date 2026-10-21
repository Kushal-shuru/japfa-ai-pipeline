"""`needs_human` (+ `needs_human_wait`): Jira **Blocked** (D127), chat card with Retry / Stop,
pause. The Jira status it had before is remembered, so `retry` / `guide` put it back (In Dev, or
RFT / Testing when it got stuck while QA had it).

The thread pauses on an interrupt (`{"gate": "needs_human"}`) instead of ending, so `retry`
resumes it at the failed step and `stop` ends it (§3.3). `guide` (P4b) = retry plus the
human's guidance, kept in `state.guidance` for the planner / coder that runs next. The failed
node is kept in `last_event.node`; `waiting_for` stays None (GateName is fixed, §7).
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from pipeline.chat.cards import Action, Card
from pipeline.chat.summaries import ERROR_WORDS, step_words
from pipeline.environment import LEAD
from pipeline.graph.nodes.common import pending_repos
from pipeline.graph.state import TicketState
from pipeline.graph.wrapper import expected_status
from pipeline.integrations.base import IntegrationError
from pipeline.logging import get_logger

# A failed wait node is retried from its gate's first node (so the card is posted again).
RETRY_TARGET: dict[str, str] = {"plan_gate_wait": "plan_gate", "review_gate_wait": "review_gate",
                                "fix_gate_wait": "fix_gate",
                                "re_review_gate_wait": "re_review_gate",
                                "deploy_doc_gate_wait": "deploy_doc_gate",
                                "migration_wait": "acceptance"}  # fmt: skip
RETRY_NODES = frozenset(
    {"load_context", "plan", "plan_gate", "env_check", "code_next_repo", "verify", "replan",
     "acceptance", "open_mrs", "start_review", "publish_mrs",
     "review_gate", "review", "fix_gate", "fix", "push_fixes", "re_review_gate", "approve",
     "change", "deploy_doc_gate", "deploy_doc", "wait_merge", "post_qa_notes",
     "wait_qa", "qa_fix", "qa_done"}
)  # fmt: skip


# P9 (DEMO-9): where a stop is a decision on the review's findings (a dispute twice, two fix
# rounds without progress, the round limit), "accept it as it is" is a valid answer
ACCEPT_AT = frozenset({"review", "acceptance"})  # P10: live tests that keep failing


def retry_target(node: str | None) -> str:
    target = RETRY_TARGET.get(node or "", node or "load_context")
    return target if target in RETRY_NODES else "load_context"


def halt_kind(reason: str, event: dict) -> str:
    """Why a ticket stopped, for the card (1.3): provider_limit,
    machine, rounds, stopped or other. From the plain words the steps use
    (`summaries.ERROR_WORDS`, the round-limit halts)."""
    low = reason.lower()
    if low.startswith(("stopped by", "stop_requested")):
        return "stopped"
    if event.get("machine") or low.startswith(LEAD.lower()):
        return "machine"
    for kind in ("provider_limit",):
        if ERROR_WORDS[kind].lower() in low:
            return kind
    if "(the limit)" in low:
        return "rounds"
    return "other"


KIND_WORDS = {
    "provider_limit": "the AI service's usage limit was reached",
    "machine": "a problem on the machine that runs the pipeline",
    "rounds": "the limit of review or QA rounds was reached",
}


def _limit_facts(kind: str, key: str, deps: Any) -> list[tuple[str, str]]:
    if kind == "provider_limit":
        fallback = deps.config.agents.fallback_provider
        return [("Fallback", f"{fallback} (also failed)" if fallback else "none configured")]
    return []


def needs_human(state: TicketState, deps: Any) -> dict:
    key = state["key"]
    reason = state.get("halted_reason") or "unknown problem"
    ev = state.get("last_event") or {}
    failed = ev.get("node") or "load_context"
    moved = bool(ev.get("moved_by_human"))
    status = deps.config.jira.statuses.blocked
    before = None
    if not moved:
        try:
            before = deps.jira.current_status(key)
            deps.jira.transition(key, status)
        except IntegrationError as err:
            get_logger(ticket=key, node="needs_human").warning("jira_transition_failed",
                                                               error=str(err))  # fmt: skip
    round_ = len(deps.store.events_for(key, "halt")) + len(deps.store.events_for(key, "error"))
    data = {"reason": reason, "failed_node": failed, "kind": halt_kind(reason, ev)}
    deps.store.record_event(key, "halt", node="needs_human", data=data)
    kind = halt_kind(reason, ev)
    facts = [("Stopped at", step_words(failed))]
    if kind in KIND_WORDS:
        facts.append(("Why", KIND_WORDS[kind]))
    facts += _limit_facts(kind, key, deps)
    actions = [
        Action("Retry", f"retry {key}", style="positive"),
        Action("Guide", f"guide {key}:", input_hint="<guidance>"),
    ]
    if failed in ACCEPT_AT:  # P9: the review stopped for a person's decision on its findings
        actions.append(Action("Accept as is", f"accept {key} --confirm", style="destructive"))
    actions.append(Action("Stop", f"stop {key} --confirm", style="destructive"))
    card = Card(title=f"{key} needs your help", facts=facts, actions=actions)
    msg = deps.notifier.needs_human(key, reason, card.to_dict(), round=round_, step=failed)
    deps.store.upsert_ticket(
        key, stage="needs_human", halted_reason=reason, waiting_for="needs_human",
        **({} if moved else {"jira_status": status}),
    )  # fmt: skip
    return {
        "gate_message_id": msg,
        "last_event": {"type": "needs_human", "node": failed, "reason": reason, "round": round_,
                       "status_before": before},
    }  # fmt: skip


def needs_human_wait(state: TicketState, deps: Any) -> dict:
    key = state["key"]
    ev = state.get("last_event") or {}
    payload = interrupt({"gate": "needs_human", "message_id": state.get("gate_message_id")})
    if not isinstance(payload, dict):
        payload = {}
    action = payload.get("action")
    if payload.get("gate") != "needs_human" or action not in ("retry", "stop", "guide", "accept"):
        get_logger(ticket=key, node="needs_human_wait").warning("stale_resume", payload=payload)
        return {}
    by = str(payload.get("by") or "someone")
    msg = state.get("gate_message_id")
    round_ = ev.get("round", 0)
    deps.store.record_event(key, "resume", node="needs_human", data={"action": action, "by": by})

    if action == "stop":
        deps.notifier.gate_passed(key, "needs_human", by, f"{by} stopped the ticket.", msg, round_)
        deps.store.upsert_ticket(
            key, stage="stopped", waiting_for=None, halted_reason=f"Stopped by {by}.",
            finished_at=deps.clock(),
        )  # fmt: skip
        return {"gate_message_id": None, "last_event": {"type": "stopped", "by": by}}

    if action == "accept":  # only after a review stop (the bus checks); like the fix gate's accept
        if ev.get("node") == "acceptance":
            return _accept_live(state, deps, by, msg, round_)
        return _accept(state, deps, by, msg, round_)

    target = retry_target(ev.get("node"))
    guidance = list(state.get("guidance") or [])
    label = f"{by} asked me to try {step_words(target)} again."
    if action == "guide":  # P4b: remember the guidance for the agent that runs next
        text = str((payload.get("args") or {}).get("text") or "").strip()
        pending = pending_repos(state)
        repo = pending[0] if target in ("code_next_repo", "verify") and pending else None
        guidance.append({"by": by, "at": deps.clock().isoformat(timespec="seconds"),
                         "text": text, "step": target, "repo": repo})  # fmt: skip
        label = f"{by} gave guidance for {step_words(target)}: “{text}”"
    before = ev.get("status_before")
    if before and before.casefold() == deps.config.jira.statuses.blocked.casefold():
        before = None
    expected = expected_status(target, deps, state) or before
    if expected:
        deps.jira.transition(key, expected)
    deps.notifier.gate_passed(key, "needs_human", by, label, msg, round_)
    deps.store.upsert_ticket(
        key, halted_reason=None, waiting_for=None, **({"jira_status": expected} if expected else {})
    )
    repos = {
        r: ({**w, "verify_attempts": 0} if r in pending_repos(state) else w)
        for r, w in (state.get("repos") or {}).items()
    }
    return {
        "halted_reason": None,
        "gate_message_id": None,
        "repos": repos,
        "guidance": guidance,
        "last_event": {"type": action, "node": target, "by": by},
    }


def _accept_live(state: TicketState, deps: Any, by: str, msg: str | None, round_: int) -> dict:
    """P10: a person accepts the failing live tests for **these** commits (a new commit tests
    again); recorded like any override (§9.15)."""
    from pipeline.graph.nodes.testing import accept_failures

    key = state["key"]
    override = accept_failures(state, deps, by)
    expected = expected_status("acceptance", deps, state)
    if expected:
        deps.jira.transition(key, expected)
    deps.notifier.gate_passed(key, "needs_human", by, f"{by} accepted the failing live tests "
                              "as they are.", msg, round_)  # fmt: skip
    deps.store.upsert_ticket(key, halted_reason=None, waiting_for=None,
                             **({"jira_status": expected} if expected else {}))  # fmt: skip
    return {"halted_reason": None, "gate_message_id": None, "acceptance_override": override,
            "last_event": {"type": "accept", "node": "acceptance", "by": by}}  # fmt: skip


def _accept(state: TicketState, deps: Any, by: str, msg: str | None, round_: int) -> dict:
    """A person accepts the open findings as they are: recorded as an override (§9.15), then the
    approval, as from the fix gate."""
    from pipeline.findings import numbered, one_line

    key = state["key"]
    items = numbered(state)
    override = {"by": by, "at": deps.clock().isoformat(timespec="seconds"),
                "open_findings": [one_line(n, f) for n, f in items],
                "finding_ids": [f.id for _, f in items]}  # fmt: skip
    deps.store.record_event(key, "gate_pass", node="needs_human", data={"override": override})
    expected = expected_status("approve", deps, state)
    if expected:
        deps.jira.transition(key, expected)
    deps.notifier.gate_passed(key, "needs_human", by, f"{by} accepted it as it is.", msg, round_)
    deps.store.upsert_ticket(key, halted_reason=None, waiting_for=None,
                             **({"jira_status": expected} if expected else {}))  # fmt: skip
    return {"halted_reason": None, "gate_message_id": None, "override": override,
            "last_event": {"type": "accept", "node": "approve", "by": by}}  # fmt: skip
