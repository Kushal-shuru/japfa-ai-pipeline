"""Testing (Phase 7, D126): `post_qa_notes` (RFT) → `wait_qa` ⟲ → `qa_done` (QA - Passed) or
`qa_fix` → the normal loop (plan approval, coding, MRs, review, merge) → `post_qa_notes` again.

- `post_qa_notes`: first RFT → the QA notes agent writes copy-paste test cases (Jira) and the
  Coder posts the ready-for-testing card; after a QA round → "Resolved: 1, 2" on Jira and a short
  chat line. Jira → RFT; the ticket then waits for testing.
- `wait_qa` interrupts first (`{"gate": "qa"}`, or `"qa_fix"` while bugs are open) and works after
  the resume:
  - `fix KEY DEMO-13 DEMO-14` (D206): QA files each bug as its own Jira ticket; each ticket's
    summary and description become a numbered bug (`pipeline/bugs.py`), listed on the main
    ticket's Jira, and all of them go into the next QA round;
  - `bug KEY: …` typed in the terminal: recorded the same way (numbered, on Jira);
  - `fix 1 2` / `fix all` (no tickets): the bugs already recorded and still open (the rest stay
    open), a number that isn't an open bug is refused in words and the ticket keeps waiting;
  - `qa_passed` (poller: a person moved Jira to QA - Passed) → `qa_done`; `stop` → Needs Human
    (Jira Blocked).
- `qa_fix`: `qa_round += 1` (over `max_qa_rounds` → Needs Human), Jira In Dev, a fresh worktree
  per repo on `ai/<KEY>-…-qa<n>` from the latest default branch, clean per-repo state; then
  `plan` runs in `qa_fix` mode and the plan approval card follows.
- `qa_done`: a final message, the workspace removed, the ticket finished.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from langgraph.types import interrupt

from pipeline import bugs as B
from pipeline.agents import qa_notes
from pipeline.chat.cards import Action, Card
from pipeline.chat.summaries import error_words, plural
from pipeline.graph.gates import gate_mode
from pipeline.graph.nodes.common import after_run, agent_slot, jira_url, ordered
from pipeline.graph.state import RepoWork, TicketState
from pipeline.graph.wrapper import halt
from pipeline.integrations.base import IntegrationError
from pipeline.logging import get_logger
from pipeline.workspace import branch_name

QA_GATES = ("qa", "qa_fix")
QA_ACTIONS = ("fix", "fix_bugs", "bug", "qa_passed", "stop")
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
HOW_TO_REPORT = (
    "Found problems? File each bug as its own Jira ticket (the bug in the description), move this "
    "ticket to Blocked, then tell me the bug tickets here, for example `fix {key} DEMO-13 "
    "DEMO-14`."
)


# ---------- post_qa_notes (RFT) ----------


def post_qa_notes(state: TicketState, deps: Any) -> dict:
    key = state["key"]
    round_ = state.get("qa_round", 0)
    bugs = list(state.get("bugs") or [])
    update: dict = {}
    how = HOW_TO_REPORT.format(key=key)
    if round_ == 0:
        routes, route_notes = qa_notes.notes_inputs(deps, state)
        with agent_slot(state, deps, "qa_notes") as slot:
            deps.notifier.progress(key, f"All merge requests for {key} are merged. I'm writing "
                                   "the test notes for QA.", "coder")  # fmt: skip
            result, run = qa_notes.write_notes(
                deps, key, ordered(state), routes, route_notes, slot=slot.name,
                provider_override=state.get("provider_override"),
            )  # fmt: skip
        update = after_run(state, deps, slot)
        if result is None:
            return {**update, **halt("post_qa_notes", "Writing the test notes for QA failed: "
                                     f"{error_words(run.error)}")}  # fmt: skip
        md = result.comment_markdown.strip()
        n = qa_notes.count_tests(md)
        url = jira_url(deps, key)
        where = f"[the Jira ticket]({url})" if url else "the Jira ticket"
        card = Card(title=f"{key}: ready for testing", facts=[("Test cases", str(n))],
                    sections=[f"The test notes are on {where}.", how])
        deps.notifier.qa(key, f"{key} is merged and ready for testing. I've written "
                         f"{plural(n, 'test case')} for QA on Jira.", card.to_dict(),
                         f"**Test notes for QA**\n\n{md}", "post_qa_notes", 0)  # fmt: skip
    else:
        fixed = [b["n"] for b in B.in_round(bugs, round_)]
        bugs = B.mark(bugs, fixed, "fixed")
        resolved = B.list_md([b for b in B.items(bugs) if b["n"] in fixed])
        still = [b["n"] for b in B.open_bugs(bugs)]
        jira_md = (f"QA round {round_} is merged and ready for testing again.\n\n"
                   f"**Resolved:**\n\n{resolved}")  # fmt: skip
        if still:
            jira_md += f"\n\n**Still open (not chosen for this round):** {B.numbers_words(still)}"
        deps.notifier.qa(key, f"{key} is ready for testing again: I fixed "
                         f"{B.numbers_words(fixed)} (details on Jira). {how}", None,
                         jira_md, "post_qa_notes", round_)  # fmt: skip
    status = deps.config.jira.statuses.rft
    deps.jira.transition(key, status)
    if not any(r.get("round") == round_ for r in B.items(bugs, "rft")):
        bugs.append({"kind": "rft", "round": round_})
    deps.store.upsert_ticket(key, jira_status=status, waiting_for="qa")
    return {**update, "bugs": bugs, "waiting_for": "qa", "gate_message_id": None,
            "last_event": {"type": "rft", "round": round_}}  # fmt: skip


# ---------- wait_qa ----------


def _gate_now(bugs: list[dict]) -> str:
    return "qa_fix" if B.open_bugs(bugs) else "qa"


def bugs_card(state: TicketState, deps: Any, open_ns: list[int]) -> Card:
    key = state["key"]
    return Card(
        title=f"{key}: bugs from testing",
        sections=[
            f"Open bugs: {B.numbers_words(open_ns)} (the text is on Jira). Which should I fix?"
        ],  # fmt: skip
        actions=[
            Action("Fix all", f"fix {key} all", style="positive"),
            Action("Fix selected…", f"fix {key}:", input_hint="1, 2"),
            Action("Stop", f"stop {key} --confirm", style="destructive"),
        ],  # fmt: skip
    )


def _announce(state: TicketState, deps: Any, bugs: list[dict], added: list[dict]) -> str | None:
    """Jira: the numbered list (with the text); chat: a short ping + the choice card."""
    key = state["key"]
    round_ = state.get("qa_round", 0)
    ns = [b["n"] for b in added]
    open_ns = [b["n"] for b in B.open_bugs(bugs)]
    jira_md = "**Bugs found in testing**\n\n"
    jira_md += B.list_md(added)
    statuses = deps.config.jira.statuses
    try:
        if statuses.in_testing(deps.jira.current_status(key)):  # RFT or Testing
            deps.jira.transition(key, statuses.blocked)  # the board has no QA Failed
            deps.store.upsert_ticket(key, jira_status=statuses.blocked)
    except IntegrationError as err:
        get_logger(ticket=key, node="wait_qa").warning("blocked_transition", error=str(err))
    text = f"QA found {plural(len(ns), 'bug')} on {key} ({B.numbers_words(ns)}); they're on Jira."
    if gate_mode("qa_fix", deps) == "auto":
        text += (f" I'll start fixing {'it' if len(open_ns) == 1 else 'them'} in "
                 f"{B.QUIET_MINUTES} minutes unless more come in.")  # fmt: skip
        return deps.notifier.qa(key, text, None, jira_md, "qa_bugs", round_, str(ns[0]))
    text += " Which should I fix? Tell me the numbers, or “all of them”."
    card = bugs_card(state, deps, open_ns).to_dict()
    return deps.notifier.qa(key, text, card, jira_md, "qa_bugs", round_, str(ns[0]))


def _keep_waiting(state: TicketState, deps: Any, bugs: list[dict], msg: str | None,
                  added: list[dict]) -> dict:  # fmt: skip
    gate = _gate_now(bugs)
    deps.store.upsert_ticket(state["key"], waiting_for=gate)
    return {"bugs": bugs, "waiting_for": gate,
            "gate_message_id": msg or state.get("gate_message_id"),
            "last_event": {"type": "qa_bugs", "added": [b["n"] for b in added]}}  # fmt: skip


def wait_qa(state: TicketState, deps: Any) -> dict:
    key = state["key"]
    bugs = list(state.get("bugs") or [])
    gate = _gate_now(bugs)
    payload = interrupt({"gate": gate, "message_id": state.get("gate_message_id")})
    if not isinstance(payload, dict) or payload.get("gate") not in QA_GATES \
            or payload.get("action") not in QA_ACTIONS:  # fmt: skip
        get_logger(ticket=key, node="wait_qa").warning("stale_resume", payload=payload)
        return {"waiting_for": gate}
    action = payload["action"]
    by = str(payload.get("by") or "someone")
    args = dict(payload.get("args") or {})
    deps.store.record_event(key, "resume", node="wait_qa", data={"action": action, "by": by})
    if action == "stop":
        deps.notifier.gate_passed(key, gate, by, f"{by} stopped the ticket.",
                                  state.get("gate_message_id"),
                                  state.get("qa_round", 0))  # fmt: skip
        deps.store.upsert_ticket(key, waiting_for=None)
        return {"waiting_for": None, **halt("wait_qa", f"Stopped by {by}.")}
    if action == "qa_passed":
        deps.store.upsert_ticket(key, waiting_for=None)
        return {"waiting_for": None, "last_event": {"type": "qa_passed", "by": by}}

    at = deps.clock().isoformat(timespec="seconds")
    added: list[dict] = []
    from_tickets: list[dict] = []
    if action == "bug":
        bugs, added = B.add_typed(bugs, str(args.get("text") or ""), by, at,
                                  str(payload.get("source") or "chat"))  # fmt: skip
    elif args.get("bug_tickets"):  # D206: one Jira ticket per bug, the bug in its description
        issues = []
        try:
            for bug_key in args["bug_tickets"]:
                issues.append(deps.jira.get_issue(bug_key))
        except IntegrationError as err:
            deps.notifier.qa(key, f"I couldn't read the bug ticket {bug_key}: "
                             f"{error_words(str(err))}. Check the number and ask me again.")
            return _keep_waiting(state, deps, bugs, None, [])
        before = {b["n"] for b in B.items(bugs)}
        bugs, from_tickets = B.add_tickets(bugs, issues, at, by)
        added = [b for b in from_tickets if b["n"] not in before]
    msg = _announce(state, deps, bugs, added) if added else None
    if action not in ("fix", "fix_bugs"):
        return _keep_waiting(state, deps, bugs, msg, added)

    if from_tickets:
        chosen, unknown = [b["n"] for b in from_tickets], []
    else:
        chosen, unknown = B.choose(bugs, args.get("scope"),
                                   list(args.get("finding_numbers") or []))
    open_ns = [b["n"] for b in B.open_bugs(bugs)]
    if not open_ns:
        deps.notifier.qa(key, f"I have no bugs for {key} yet. Give me the bug tickets: "
                         f"`fix {key} {deps.config.jira.project}-13`.")  # fmt: skip
        return _keep_waiting(state, deps, bugs, msg, added)
    if unknown or not chosen:
        what = (f"{B.numbers_words(unknown)} {'is' if len(unknown) == 1 else 'are'} not an open "
                "bug") if unknown else "That picks no open bug"  # fmt: skip
        deps.notifier.qa(key, f"{what} on {key}. The open bugs are {B.numbers_words(open_ns)} "
                         "(listed on Jira).")  # fmt: skip
        return _keep_waiting(state, deps, bugs, msg, added)

    round_ = state.get("qa_round", 0) + 1
    bugs = B.mark(bugs, chosen, "fixing", round_)
    notes = str(args.get("notes") or "").strip()
    label = f"{by} asked me to fix {B.numbers_words(chosen)}."
    if notes:
        label = label.rstrip(".") + f": “{notes}”"
    deps.notifier.gate_passed(key, "qa_fix", by, label, msg or state.get("gate_message_id"),
                              round_)  # fmt: skip
    deps.store.record_event(key, "gate_pass", node="qa_fix",
                            data={"by": by, "action": "fix_bugs", "bugs": chosen})  # fmt: skip
    deps.store.upsert_ticket(key, waiting_for=None)
    guidance = list(state.get("guidance") or [])
    if notes:
        guidance.append({"by": by, "at": at, "text": notes, "step": "qa_fix", "repo": None})
    return {"bugs": bugs, "guidance": guidance, "waiting_for": None, "gate_message_id": None,
            "last_event": {"type": "gate", "gate": "qa_fix", "action": "fix_bugs", "by": by,
                           "bugs": chosen}}  # fmt: skip


# ---------- qa_fix: a new QA round ----------


def qa_branch(state: TicketState, round_: int | None = None) -> str:
    ctx = state["context"]
    round_ = state.get("qa_round", 0) if round_ is None else round_
    return branch_name(state["key"], ctx.summary if ctx else "", f"qa{round_}" if round_ else "")


def qa_fix(state: TicketState, deps: Any) -> dict:
    key = state["key"]
    round_ = state.get("qa_round", 0) + 1
    chosen = [b["n"] for b in B.in_round(state.get("bugs"), round_)]
    limit = deps.config.limits.max_qa_rounds
    if round_ > limit:
        return halt("qa_fix", f"{key} has come back from testing {plural(limit, 'time')} already "
                    f"(the limit), so a person should look at bugs {B.numbers_words(chosen)} "
                    "before I try again.")  # fmt: skip
    status = deps.config.jira.statuses.in_dev
    deps.jira.transition(key, status)
    ctx = state["context"]
    assert ctx is not None
    branch = qa_branch(state, round_)
    ws = deps.workspaces
    repos: dict[str, RepoWork] = {}
    for n, name in enumerate(deps.config.repo_order(list(ctx.repos)), 1):
        path = ws.switch_worktree(key, name, branch)
        repos[name] = {
            "repo": name, "role": deps.config.repo(name).role, "order": n, "status": "pending",
            "branch": branch, "worktree": str(path), "mr_iid": None, "mr_url": None,
            "coder_result": None, "verify_result": None, "review_result": None,
            "devops_facts": None, "verify_attempts": 0,
        }  # fmt: skip
    deps.store.upsert_ticket(key, qa_rounds=round_, jira_status=status, waiting_for=None)
    deps.notifier.progress(key, f"Starting QA round {round_} for {key}: bugs "
                           f"{B.numbers_words(chosen)}. I'll plan the fixes first.",
                           "coder")  # fmt: skip
    return {"qa_round": round_, "repos": repos, "review_round": 0, "replans": 0,
            "plan_revisions": 0, "plan_feedback": [], "fix_scope": None, "override": None,
            "review_log": [],
            "contract_review": None, "deploy_result": None,
            "last_event": {"type": "qa_round", "round": round_}}  # fmt: skip


# ---------- qa_done ----------


def qa_done(state: TicketState, deps: Any) -> dict:
    key = state["key"]
    ev = state.get("last_event") or {}
    text = f"{key} passed testing. Thanks, everyone: I'm closing it on my side."
    deps.notifier.qa(key, text, None, f"QA passed (seen by {ev.get('by') or 'the pipeline'}). "
                     "The AI pipeline has finished this ticket.", "qa_done", 0)  # fmt: skip
    try:
        deps.workspaces.remove_ticket_workspace(key)
    except IntegrationError as err:  # a leftover folder must not block closing
        get_logger(ticket=key, node="qa_done").warning("workspace_remove_failed", error=str(err))
    deps.store.upsert_ticket(key, stage="finished", waiting_for=None, finished_at=deps.clock(),
                             jira_status=deps.config.jira.statuses.qa_passed)  # fmt: skip
    return {"waiting_for": None, "last_event": {"type": "finished"}}
