"""Node wrapper (Phase 3 7.2): guards, events, stage, errors.

Before a node: while `$PIPELINE_HOME/PAUSE` exists → wait (1.1); a stop request → halt; Jira
status must match what the pipeline last set (else halt `moved_by_human`, §9.13). Around it:
`node_start` / `node_end` / `error` events and `tickets.stage`. Exceptions never escape into
the graph: they become `halted_reason`, and every router sends a halted state to
`needs_human`. LangGraph interrupts (gates) pass through untouched.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from langgraph.errors import GraphBubbleUp

from pipeline.agents.pool import SlotCancelled
from pipeline.chat.summaries import step_words
from pipeline.environment import plain_reason
from pipeline.graph.state import TicketState
from pipeline.logging import get_logger
from pipeline.pause import pause_info, paused_words, wait_while_paused

NodeFn = Callable[[TicketState, Any], dict]
STOP_PREFIX = "stop_requested"
MOVED_BY_HUMAN = "moved_by_human"

# Jira status (config key) a node expects to find; None = no check. D127: the whole way from the
# plan to the last merge is In Dev (plan, code, review, fixes, deployment note, merge wait); the
# testing steps don't check (QA moves it between Ready for Test, Testing and Blocked).
DEV_NODES = (
    "plan", "plan_gate", "plan_gate_wait", "env_check", "code_next_repo", "verify", "replan",
    "acceptance", "open_mrs",
    "review_gate", "review_gate_wait", "review", "fix_gate", "fix_gate_wait", "fix",
    "push_fixes", "re_review_gate", "re_review_gate_wait", "approve", "change",
    "deploy_doc_gate", "deploy_doc_gate_wait", "deploy_doc", "wait_merge", "post_qa_notes",
)  # fmt: skip
EXPECTED_STATUS: dict[str, str | None] = {
    "load_context": None,
    **{n: "in_dev" for n in DEV_NODES},
    "wait_qa": None,
    "qa_fix": None,
    "qa_done": None,
    "migration_wait": None,  # P10: Blocked while it waits for the migration on the dev DB
}


def expected_status(node: str, deps: Any, state: Any = None) -> str | None:
    name = EXPECTED_STATUS.get(node)
    return getattr(deps.config.jira.statuses, name) if name else None


def stop_requested(deps: Any, key: str) -> str | None:
    row = deps.store.get_ticket(key) or {}
    reason = row.get("halted_reason") or ""
    return reason if reason.startswith(STOP_PREFIX) else None


def halt(node: str, reason: str, **extra: Any) -> dict:
    """Stop at `node` for a person. A machine problem (Redis down, disk full …) gets its plain
    cause on top of the reason (1.9)."""
    reason, machine = plain_reason(reason)
    if machine:
        extra["machine"] = True
    return {"halted_reason": reason, "last_event": {"type": "halt", "node": node, **extra}}


def _guard(name: str, state: TicketState, deps: Any) -> dict | None:
    key = state["key"]
    info = pause_info(deps.home)
    if info is not None:  # 1.1: wait here (no Needs Human); `unpause` lets it carry on
        deps.notifier.progress(key, f"The pipeline is {paused_words(info)}, so {key} waits "
                               f"before {step_words(name)}. I'll carry on by myself as soon "
                               "as it's resumed.")  # fmt: skip
        deps.store.upsert_ticket(key, waiting_for="paused")
        wait_while_paused(deps.home, lambda: bool(stop_requested(deps, key)))
        deps.store.upsert_ticket(key, waiting_for=None)
    stop = stop_requested(deps, key)
    if stop:
        by = stop.split(" by ", 1)[1] if " by " in stop else "someone"
        return halt(name, f"Stopped by {by}.")
    expected = expected_status(name, deps, state)
    if expected:
        current = deps.jira.current_status(key)
        if current.casefold() != expected.casefold():
            return halt(
                name,
                f"Someone moved the Jira ticket to “{current}” while I expected "
                f"“{expected}”. Move it back and try again, or stop.",
                moved_by_human=True,
            )
    return None


def wrap(name: str, fn: NodeFn, deps: Any, guards: bool = True) -> Callable[[TicketState], dict]:
    def node(state: TicketState) -> dict:
        key = state["key"]
        log = get_logger(ticket=key, node=name)
        store = deps.store
        store.record_event(key, "node_start", node=name)
        store.upsert_ticket(key, stage=name)
        started = time.monotonic()
        try:
            update = _guard(name, state, deps) if guards else None
            if update is not None:
                store.record_event(key, "halt", node=name, data={"reason": update["halted_reason"]})
                log.warning("node_halted", reason=update["halted_reason"])
                return update
            update = fn(state, deps) or {}
        except GraphBubbleUp:
            raise  # interrupt(): the gate is waiting
        except SlotCancelled as err:
            store.record_event(key, "halt", node=name, data={"reason": str(err)})
            return halt(name, str(err))
        except Exception as err:  # never into the graph: needs_human
            log.exception("node_failed")
            store.record_event(key, "error", node=name, data={"error": str(err)[:2000]})
            reason, machine = plain_reason(
                f"Something went wrong on my side ({step_words(name)}): {err}"
            )
            return {
                "halted_reason": reason,
                "last_event": {
                    "type": "error",
                    "node": name,
                    "error": str(err)[:2000],
                    **({"machine": True} if machine else {}),
                },  # fmt: skip
            }
        store.record_event(
            key, "node_end", node=name, data={"duration_s": round(time.monotonic() - started, 2)}
        )
        return update

    node.__name__ = name
    return node
