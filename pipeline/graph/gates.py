"""Gates (CLAUDE.md §3.5, Phase 3 §3): `manual` → card + interrupt; `auto` → continue.

A gate is two nodes, because LangGraph re-runs an interrupted node from its first line on
resume and drops whatever it did before `interrupt()`:

- `<gate>` → `gate_prepare()`: auto → record `gate_pass` (by auto) and continue. Manual → post
  the card, mirror "Waiting for …" on Jira, set `waiting_for` + `gate_message_id` (saved in
  the checkpoint), record `gate_wait`.
- `<gate>_wait` → `gate_wait()`: only `interrupt({"gate": …})`. On resume: validate the payload
  (a stale or foreign payload leaves `waiting_for` set, so the router waits again), update the
  card to a sentence ("Kushal Soni approved the plan."), record `gate_pass`, clear `waiting_for`.

The command bus only resumes a thread whose pending interrupt matches the command (§9.10).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langgraph.types import interrupt

from pipeline.chat.cards import Card
from pipeline.graph.state import TicketState
from pipeline.graph.wrapper import halt
from pipeline.logging import get_logger

CardBuilder = Callable[[TicketState, Any], Card]

# Action taken when a gate is `auto`.
AUTO_ACTION: dict[str, str] = {
    "plan_approval": "approve_plan",
    "review": "review",
    "re_review": "review",
    "fix": "fix",
    "deploy_doc": "deploy_doc",
    "qa_fix": "fix_bugs",
}
# what a person did at a gate, as a sentence (card update + Jira; D98)
ACTION_LABELS: dict[str, str] = {
    "approve_plan": "{by} approved the plan.",
    "revise_plan": "{by} asked for changes to the plan",
    "review": "{by} started the review.",
    "fix": "{by} asked for the review findings to be fixed.",
    "accept": "{by} accepted it as it is.",
    "deploy_doc": "{by} asked for the deployment note.",
    "fix_bugs": "{by} asked for the QA bugs to be fixed.",
    "retry": "{by} asked me to try again.",
    "stop": "{by} stopped the ticket.",
    "change": "{by} asked for a change",
}


def fix_words(args: dict) -> str:
    """What a `fix` decision asks for, in words: "#1 and #3", "the blockers" …"""
    from pipeline.chat.summaries import join_words
    from pipeline.findings import SCOPE_WORDS

    picked = [f"#{n}" for n in args.get("finding_numbers") or []] + \
        list(args.get("finding_ids") or [])  # fmt: skip
    if picked:
        return join_words(picked)
    return SCOPE_WORDS.get(args.get("scope") or "", "the review findings")


# In a QA round the review runs by itself (D126): the bug fixes go straight to the Reviewer,
# people still choose what to fix and merge.
QA_ROUND_AUTO_GATES = frozenset({"review", "re_review"})


def gate_mode(name: str, deps: Any, state: Any = None) -> str:
    if name in QA_ROUND_AUTO_GATES and (state or {}).get("qa_round", 0) > 0:
        return "auto"
    return deps.config.gates.get(name, "manual")


def gate_prepare(
    name: str, state: TicketState, deps: Any, card_builder: CardBuilder, round: int | str = 0
) -> dict:
    key = state["key"]
    if gate_mode(name, deps, state) == "auto":
        deps.store.record_event(key, "gate_pass", node=name, data={"by": "auto"})
        return {
            "waiting_for": None,
            "gate_message_id": None,
            "last_event": {"type": "gate", "gate": name, "action": AUTO_ACTION[name],
                           "by": "auto", "args": {}},
        }  # fmt: skip
    card = card_builder(state, deps)
    msg = deps.notifier.waiting(key, name, card.to_dict(), round=round)
    deps.store.upsert_ticket(key, waiting_for=name)
    deps.store.record_event(key, "gate_wait", node=name, data={"message_id": msg})
    return {"waiting_for": name, "gate_message_id": msg,
            "last_event": {"type": "gate_wait", "gate": name}}  # fmt: skip


def gate_wait(
    name: str,
    state: TicketState,
    deps: Any,
    actions: tuple[str, ...],
    round: int | str = 0,
    node: str | None = None,
) -> dict:
    """`node` = the wait node's name (recorded as the failed step if the gate is stopped)."""
    key = state["key"]
    payload = interrupt({"gate": name, "message_id": state.get("gate_message_id")})
    if not isinstance(payload, dict):
        payload = {}
    action = payload.get("action")
    if payload.get("gate") != name or action not in (*actions, "stop"):
        get_logger(ticket=key, node=f"{name}_wait").warning("stale_resume", payload=payload)
        return {}  # waiting_for unchanged → wait again
    by = str(payload.get("by") or "someone")
    deps.store.record_event(key, "resume", node=name, data={"action": action, "by": by})
    label = ACTION_LABELS.get(action, "{by}: " + str(action)).format(by=by)
    if action == "fix":
        label = f"{by} asked me to fix {fix_words(payload.get('args') or {})}."
    args_ = payload.get("args") or {}
    feedback = (
        args_.get("feedback")
        or args_.get("notes")
        or (args_.get("text") if action == "change" else None)
    )
    if feedback:
        label = label.rstrip(".") + f": “{feedback}”"
    deps.notifier.gate_passed(key, name, by, label, state.get("gate_message_id"), round)
    deps.store.upsert_ticket(key, waiting_for=None)
    deps.store.record_event(
        key, "gate_pass", node=name, data={"by": by, "action": action, "args": payload.get("args")}
    )
    if action == "stop":
        return {
            "waiting_for": None,
            "gate_message_id": None,
            **halt(node or f"{name}_wait", f"Stopped by {by}."),
        }
    return {
        "waiting_for": None,
        "gate_message_id": None,
        "last_event": {"type": "gate", "gate": name, "action": action, "by": by,
                       "args": dict(payload.get("args") or {})},
    }  # fmt: skip


def gate_action(state: TicketState, name: str) -> str | None:
    """The action that passed gate `name` (None if it is still waiting)."""
    ev = state.get("last_event") or {}
    if ev.get("type") == "gate" and ev.get("gate") == name:
        return ev.get("action")
    return None
