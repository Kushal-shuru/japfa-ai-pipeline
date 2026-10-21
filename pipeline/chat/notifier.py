"""Ticket messages → chat thread, with decision-type events mirrored to Jira (the audit trail).

P9: `chat.verbosity` (default `minimal`, the notification policy): the thread gets the plan, one
merge-request card (edited with the approval and the deployment note), decision cards, blockers
and the QA handoff; routine progress, hand-overs, review rounds and pushed fixes stay in the logs
(and `status`). Jira gets one delivery summary comment, edited in place (`chat/delivery.py`),
instead of a comment per event. `normal` = everything below, for debugging.

Chat-only: `progress`, `repo_progress` (the text line). Mirrored (with
idempotency markers, §9.3): started, plan_ready, mrs_opened, waiting, gate_passed, needs_human,
error. A Jira failure while reporting
`needs_human` / `error` is logged, not raised (we are already on the failure path).
"""

from __future__ import annotations

import hashlib
from typing import Any

from pipeline.chat import delivery
from pipeline.chat.cards import Card
from pipeline.chat.summaries import (
    DEFAULT_AGENT_NAMES,
    STEP_PERSONA,
    Summarizer,
    agent_name,
    jira_two_part,
    join_words,
    plural,
    step_words,
    with_persona,
)
from pipeline.integrations.base import IntegrationError
from pipeline.logging import get_logger
from pipeline.markers import make_marker

# what a paused ticket waits for, as "waiting for <label>"
GATE_LABELS: dict[str, str] = {
    "plan_approval": "someone to approve the plan",
    "review": "someone to start the review",
    "fix": "a decision on the review findings",
    "re_review": "someone to start the second review",
    "deploy_doc": "the deployment note",
    "qa_fix": "someone to choose which QA bugs to fix",
    "needs_human": "someone to help (try again, give guidance or stop)",
    "merge_all": "all merge requests to be merged",
    "qa": "QA to test it",
    "migration": "its migration to be applied on the dev database",
}


# who posts a gate's card (D111): the Coder asks about its plan and which findings to fix,
# the Reviewer about starting a review
GATE_PERSONA: dict[str, str] = {
    "plan_approval": "coder", "review": "reviewer", "fix": "coder", "re_review": "reviewer",
    "deploy_doc": "devops", "qa_fix": "coder", "needs_human": "coder",
    "merge_all": "devops", "migration": "devops",
}  # fmt: skip


def gate_label(gate: str | None) -> str:
    return GATE_LABELS.get(gate or "", (gate or "nothing").replace("_", " "))


def progress_card(key: str, repos: dict, current: str | None = None, note: str = "") -> Card:
    """Repositories in coding order, each with its state in words (D98): Not started,
    In progress (+ `note`), Done (checks), Merge request open (+ link)."""
    lines = []
    for repo, work in sorted(repos.items(), key=lambda kv: (kv[1].get("order", 99), kv[0])):
        status = work.get("status") or "pending"
        verify = work.get("verify_result")
        if repo == current:
            state = note or "In progress"
        elif status == "mr_open" and work.get("mr_url"):
            state = f"Merge request open ([view]({work['mr_url']}))"
        elif status in ("coded", "mr_open"):
            state = "Done, checks passed" if verify is None or verify.ok else "Done"
        else:
            state = {"changes_requested": "Changes requested", "approved": "Approved",
                     "merged": "Merged"}.get(status, "Not started")  # fmt: skip
        lines.append(f"**{repo}** ({work.get('role', '')}): {state}")
    return Card(title=f"{key} progress", sections=["\n".join(lines) or "(no repositories)"])


class Notifier:
    """`llm` / `llm_cfg`: the Chat LLM chain and `chat_llm` config (P4b); without them every
    message uses its template (exact commands and cards never depend on the LLM)."""

    def __init__(self, chat: Any, jira: Any, store: Any = None, llm: Any = None,
                 llm_cfg: Any = None, agents: dict[str, str] | None = None,
                 verbosity: str = "normal",
                 auto_plan: bool = False) -> None:  # fmt: skip
        self.chat = chat
        self.minimal = verbosity == "minimal"  # P9
        self.auto_plan = auto_plan  # plan_approval gate is auto: the plan needs its own message
        self._mem: dict[str, dict] = {}  # the delivery record without a store (unit tests)
        self.jira = jira
        self.store = store
        self.names = agents or DEFAULT_AGENT_NAMES  # `chat.agents` (D111)
        self.words = Summarizer(llm, llm_cfg, self.names)

    def _as(self, persona: str, text: str) -> str:
        """ "**Reviewer:** …": the agent that speaks for the role (D111)."""
        return with_persona(persona, text, self.names)

    def _two(self, summary: str, details_md: str, persona: str) -> str:
        return jira_two_part(summary, details_md, persona, names=self.names)

    def agent(self, persona: str) -> str:
        return agent_name(persona, self.names)

    def _card_by(self, persona: str, card: dict | None) -> dict | None:
        """The card with its author (the agent posting it), unless it already has one."""
        if card is None or card.get("author"):
            return card
        return {**card, "author": self.agent(persona)}

    # ---------- internals ----------

    def _jira(self, key: str, body_md: str, node: str, round: int | str = 0, extra: str = "",
              swallow: bool = False) -> None:  # fmt: skip
        try:
            self.jira.comment(key, body_md, make_marker(key, node, round, extra))
        except IntegrationError as err:
            if not swallow:
                raise
            get_logger(ticket=key, node=node).warning("jira_mirror_failed", error=str(err))

    # ---------- minimal mode (P9): quiet updates + the delivery record ----------

    def _quiet(self, key: str, what: str, text: str = "") -> str:
        """Routine progress: the logs (and `status`), not the chat or Jira."""
        get_logger(ticket=key).info("quiet_update", what=what, text=(text or "")[:300])
        return ""

    def _delivery(self, key: str, decision: str = "", **changes: Any) -> dict:
        """Update the delivery record and the Jira summary comment (edited in place)."""
        note = delivery.load(self.store, self._mem, key)
        note.update(changes)
        if decision:
            delivery.add_decision(note, decision)
        delivery.save(self.store, self._mem, key, note)
        try:
            self.jira.upsert_comment(key, delivery.summary_md(key, note),
                                     make_marker(key, "summary", 0))  # fmt: skip
        except IntegrationError as err:  # the chat has it; the next update tries again
            get_logger(ticket=key, node="summary").warning("jira_summary_failed", error=str(err))
        return note

    def _show_mr_card(self, key: str, note: dict) -> str:
        """Edit the merge-request card, or post it when there is none yet."""
        card = self._card_by("coder", delivery.mr_card(key, note).to_dict())
        if note.get("mr_message"):
            self.chat.update(note["mr_message"], card=card)
            return str(note["mr_message"])
        msg = self.chat.post(key, "", card)
        note["mr_message"] = msg
        delivery.save(self.store, self._mem, key, note)
        return msg

    # ---------- helpers ----------

    def started(self, key: str, by: str, repos: list[str], summary: str = "") -> str:
        if self.minimal:  # no start acknowledgement: the command's reply already said it
            self._delivery(key, f"{by} started the AI pipeline on it.",
                           status="Planning. Repositories, in coding order: "
                           + ", ".join(repos))  # fmt: skip
            return ""
        about = f" (*{summary}*)" if summary else ""
        text = (f"I've started on {key}{about}. It touches {join_words(repos)}. "
                "I'll draft a plan first.")  # fmt: skip
        msg = self.chat.post(key, self._as("coder", text))
        self._jira(key, f"The AI pipeline started work on this ticket (requested by {by}). "
                   f"Repositories, in coding order: {', '.join(repos)}.", "started")  # fmt: skip
        return msg

    def plan_ready(self, key: str, plan_md: str, revision: int | str = 0,
                   plan: Any = None) -> str:  # fmt: skip
        """The short plan on Jira. The chat gets it only when the planner has questions; a
        ready plan is shown by the plan card, which links to Jira (D128)."""
        if self.minimal:
            # every plan version is its own Jira comment, never edited (earlier plans stay
            # readable); the delivery summary only names the current one
            ready = plan is None or plan.status == "ready"
            title = plan_title(plan)
            self._jira(key, f"**{title}**\n\n{plan_md}", "plan", revision, _fingerprint(plan_md))
            self._delivery(key, status="Plan ready" if ready else "The plan has open questions",
                           plan_rev=title)  # fmt: skip
            own_message = self.auto_plan and plan is not None and plan.mode == "initial"
            if ready and not own_message:  # the plan card (gate) is the message, or a replan
                return self._quiet(key, "plan_ready")
        facts, fallback, event = plan_facts(key, plan)
        summary = self.words.summary(key, "planner", event, facts, plan_md, fallback)
        body = self._two(summary, plan_md, "planner") if summary else plan_md
        if not self.minimal:
            self._jira(key, body, "plan", revision, _fingerprint(plan_md))
        if plan is not None and plan.status == "ready" and not self.minimal:
            return ""
        text = f"{summary}\n\n{plan_md}" if summary else plan_md
        return self.chat.post(key, self._as("planner", text))

    def progress(self, key: str, text: str, persona: str = "bot", important: bool = False) -> str:
        """`important`: shown in minimal mode too (a reminder people asked for)."""
        if self.minimal and not important:
            return self._quiet(key, "progress", text)
        return self.chat.post(key, self._as(persona, text))

    def verify_failed(self, key: str, repo: str, details_md: str, attempt: int,
                      limit: int) -> str:  # fmt: skip
        """Failed checks that go back to the coder: a plain explanation + the failing lines."""
        if self.minimal:
            return self._quiet(key, "verify_failed", repo)
        fallback = f"The automated checks failed for {repo}. I'm fixing it and will try again."
        summary = self.words.summary(
            key, "coder", f"the automated checks failed on {repo}; the coder fixes it and the "
            "checks run again automatically (nobody needs to do anything yet)",
            {"repository": repo}, details_md, fallback,
        )  # fmt: skip
        return self.progress(key, f"{summary}\n\n{details_md}", "coder")

    def repo_progress(self, key: str, repos: dict, current: str | None = None, note: str = "",
                      text: str | None = None, board: str = "q0") -> str:  # fmt: skip
        """Coding progress: post `text` (nothing if None)."""
        if self.minimal:
            return self._quiet(key, "repo_progress", text or "")
        if not getattr(self.chat, "supports_updates", False) or self.store is None:
            return self.progress(key, text, "coder") if text else ""
        card = self._card_by("coder", progress_card(key, repos, current, note).to_dict())
        existing = [e for e in self.store.events_for(key, "chat_post")
                    if (e.get("data") or {}).get("board") == board]  # fmt: skip
        if existing and existing[-1]["data"].get("message_id"):
            msg = existing[-1]["data"]["message_id"]
            self.chat.update(msg, card=card)
            return msg
        msg = self.chat.post(key, "", card)
        if msg:
            self.store.record_event(key, "chat_post", data={"message_id": msg, "board": board})
        return msg

    def mrs_opened(self, key: str, mrs: list[dict], round: str = "initial") -> str:
        """`mrs`: [{"repo", "role", "url"}] in merge order."""
        if self.minimal:  # one card per round of merge requests, edited later
            note = delivery.load(self.store, self._mem, key)
            if note.get("round") == round and note.get("mr_message"):
                return self._show_mr_card(key, self._delivery(key, mrs=mrs))
            note = self._delivery(key, status="Merge requests open; reviewing them", mrs=mrs,
                                  round=round, merged=[], mr_status="Reviewing", deploy=None,
                                  left=[], mr_message="")  # fmt: skip
            return self._show_mr_card(key, note)
        lines = [f"{n}. **{m['repo']}** ({m['role']}): {m['url']}" for n, m in enumerate(mrs, 1)]
        details = "Merge requests, in the order to merge them:\n\n" + "\n".join(lines)
        order = [m["repo"] for m in mrs]
        count = plural(len(mrs), "merge request")
        fallback = (f"{key} is ready for review: {count}, listed below in the order to merge "
                    "them." if mrs else "")  # fmt: skip
        summary = self.words.summary(
            key, "coder", "the merge requests are open (one per repository, linked to each "
            "other)",
            {"merge order": order, "count": len(mrs),
             "next step": "a person asks the reviewer to review them (the Start review button "
                           "below); after the review they are merged in this order"},
            "", fallback,
        )  # fmt: skip
        order_md = "In merge order:\n\n" + "\n".join(lines)
        card = Card(title=f"{key}: merge requests", sections=[order_md])
        msg = self.chat.post(key, self._as("coder", summary),
                             self._card_by("coder", card.to_dict()))  # fmt: skip
        self._jira(key, self._two(summary, details, "coder") if summary else details,
                   "open_mrs", round)  # fmt: skip
        return msg

    # ---------- review loop (P5) ----------

    def review_done(self, key: str, round_: int, approved: bool, facts: dict,
                    details_md: str, next_auto: bool = False) -> str:  # fmt: skip
        """Review result. Chat: one or two plain sentences (Chat LLM, template fallback); the
        findings card (fix gate) that follows carries the list (D109). Jira: the summary plus
        the numbered list (the audit trail). `next_auto`: the fix gate is auto, so the
        Reviewer hands the findings to the Coder itself (D112) instead of people choosing."""
        if self.minimal:  # findings live on the merge requests; a decision gets its own card
            now = f"{facts.get('total', 'findings')}; " + (
                "being fixed" if next_auto else "waiting for a decision"
            )
            self._delivery(key, status=f"Review round {round_}: "
                           + ("passed" if approved else now))  # fmt: skip
            return self._quiet(key, "review_done", str(round_))
        if approved:  # the approval message (`approved`) follows; chat only for leftovers
            passed = f"Code review passed (round {round_})."
            msg = ""
            left = sum(1 for ln in details_md.splitlines() if ln.startswith("- "))
            if left:
                msg = self.chat.post(key, self._as(
                    "reviewer", f"Code review passed for {key}. {plural(left, 'finding')} "
                    f"{'stays' if left == 1 else 'stay'} open, as decided."))  # fmt: skip
            self._jira(key, self._two(passed, details_md, "reviewer") if details_md
                       else passed, "review", round_)  # fmt: skip
            return msg
        coder = self.agent("coder")
        then = "" if next_auto else f" {coder} will ask you which ones to fix."
        fallback = f"Review round {round_}: {facts.get('total', 'some findings')}.{then}"
        nxt = (f"the Reviewer hands the must-fix findings to {coder} right away (say nothing "
               "about it; the hand-over message follows)" if next_auto else
               f"{coder} asks which findings to fix (don't list commands; the card shows the "
               "choices)")  # fmt: skip
        summary = self.words.summary(
            key, "reviewer", f"review round {round_} found problems",
            {"round": round_, **facts, "next step": nxt}, details_md, fallback,
        )  # fmt: skip
        msg = self.chat.post(key, self._as("reviewer", summary))
        self._jira(key, self._two(summary, details_md, "reviewer"), "review", round_)
        return msg

    def handoff(self, key: str, frm: str, to: str, text: str) -> str:
        """One team member hands the work to another in the chat (the automatic review loop,
        D112): "**Coder:** @Reviewer, the merge requests for DEMO-3 are ready …"."""
        if self.minimal:
            return self._quiet(key, "handoff", text)
        return self.chat.post(key, self._as(frm, f"@{self.agent(to)}, {text}"))

    def change_requested(self, key: str, by: str, request: str, n: int) -> str:
        """A person's change request (§4b): in the chat thread and on Jira (audit trail)."""
        if self.minimal:  # the command's reply is the chat message
            self._delivery(key, f"{by} asked for a change: {request}",
                           status="Working on a change request")  # fmt: skip
            return ""
        text = f"{by} asked for a change: “{request}”. I'll work out what it means for the code."
        msg = self.chat.post(key, self._as("coder", text))
        self._jira(key, f"Change requested by {by}: {request}", "change", n)
        return msg

    def fixes_pushed(self, key: str, round_: int, scope_words: str, fixed: str,
                     repos: list[str], disputed: list[str], not_applicable: str = "",
                     next_auto: bool = False, pushed_word: str = "pushed") -> str:  # fmt: skip
        """`next_auto`: the re-review gate is auto, so the Coder hands over itself (D112).
        `pushed_word`: "saved" while the changes are still only on this machine (D207)."""
        if self.minimal:
            return self._quiet(key, "fixes_pushed", fixed)
        where = join_words(repos) if repos else "no repository"
        check = "" if next_auto else (f" Ask {self.agent('reviewer')} to check again when "
                                      "you're ready.")  # fmt: skip
        text = (f"Fixed {fixed} and {pushed_word}.{check}" if fixed != "none"
                else f"I answered the findings and {pushed_word}.{check}")  # fmt: skip
        if not_applicable:
            text += f" Not applicable: {not_applicable}."
        if disputed:
            text += "\n\nNot changed:\n" + "\n".join(f"- {d}" for d in disputed)
        msg = self.chat.post(key, self._as("coder", text))
        self._jira(key, f"Fixes pushed for review round {round_} (scope: {scope_words}; "
                   f"{where}). {text}", "push_fixes", round_)  # fmt: skip
        return msg

    def approved(self, key: str, mrs: list[dict], override: dict | None,
                 round: int | str = 0, left: list[str] | None = None) -> str:  # fmt: skip
        """Ready to Merge: MR links in merge order (+ the override, if people accepted). The
        deployment note follows (D121) and says whether DevOps is needed."""
        if self.minimal:  # the merge-request card changes; people outside hear it (channel)
            opened = (override.get("open_findings") or []) if override else []
            decision = ""
            if override:
                decision = (f"{override.get('by')} accepted it as it is, with "
                            f"{plural(len(opened), 'open finding')}"
                            + (": " + "; ".join(opened) if opened else ""))  # fmt: skip
            status = (f"Accepted as it is by {override.get('by')}" if override
                      else "Approved: ready to merge")  # fmt: skip
            note = self._delivery(key, decision, mr_status=status, left=list(left or []),
                                  status=f"{status}; waiting for the merges")  # fmt: skip
            if not note.get("mrs"):
                note = self._delivery(key, mrs=mrs)
            return self._show_mr_card(key, note)
        lines = [f"{n}. **{m['repo']}** ({m['role']}): {m['url']}" for n, m in enumerate(mrs, 1)]
        order = ("Merge them in this order (services first, then the gateway):\n\n"
                 + "\n".join(lines))  # fmt: skip
        if override:
            n = len(override.get("open_findings") or [])
            lead = (f"{override.get('by')} accepted {key} as it is, with "
                    f"{plural(n, 'open finding')}. I've approved the merge requests.")  # fmt: skip
        else:
            lead = f"Code review passed for {key}. I've approved the merge requests."
        card = Card(title=f"{key}: ready to merge", sections=[order])
        msg = self.chat.post(key, self._as("reviewer", lead),
                             self._card_by("reviewer", card.to_dict()))  # fmt: skip
        details = order
        if override and override.get("open_findings"):
            details += "\n\nOpen findings accepted as they are:\n\n" + \
                "\n".join(f"- {x}" for x in override["open_findings"])  # fmt: skip
        self._jira(key, self._two(lead, details, "reviewer"), "approve", round)
        return msg

    # ---------- deployment, merges (P6; DevOps works before the merges, D124) ----------

    def deploy_note(self, key: str, summary: str, filename: str, card: dict,
                    n: int | str = 1, updated: bool = False) -> str:  # fmt: skip
        """The deployment note: the Deployment card by DevOps in chat; on Jira only a comment
        pointing at the attached file (`[^file]`, attached before this call; D122)."""
        if self.minimal:  # added to the merge-request card; its own message only for DevOps work
            facts = [list(f) for f in card.get("facts") or []]
            deploy = {"file": filename, "line": "updated" if updated else "", "facts": facts,
                      "sections": list(card.get("sections") or [])}  # fmt: skip
            note = self._delivery(key, deploy=deploy)
            msg = self._show_mr_card(key, note)
            devops = any(f[0] == "DevOps required" and str(f[1]).startswith("yes") for f in facts)
            if devops:  # people must act before merging: that's worth a message
                return self.chat.post(key, self._as("devops", summary))
            return msg
        msg = self.chat.post(key, self._as("devops", summary), self._card_by("devops", card))
        what = "Updated deployment note" if updated else "Deployment note"
        self._jira(key, f"{what}: [^{filename}]", "deploy_doc", n)
        return msg

    def merged(self, key: str, text: str, repo: str, n: int | str = 0) -> str:
        """Merge progress ("demo-ws2 !9 is merged (1 of 2)."), mirrored to Jira."""
        if self.minimal:  # shown on the merge-request card, not posted
            note = delivery.load(self.store, self._mem, key)
            merged = sorted({*note.get("merged", []), repo})
            done = {m["repo"] for m in note.get("mrs") or []} <= set(merged)
            note = self._delivery(key, merged=merged,
                                  **({"mr_status": "All merged"} if done else {}))  # fmt: skip
            if note.get("mr_message"):
                self._show_mr_card(key, note)
            return self._quiet(key, "merged", text)
        msg = self.chat.post(key, self._as("devops", text))
        self._jira(key, text, "merge", n, repo)
        return msg

    def warning(self, key: str, text: str, node: str, extra: str = "") -> str:
        """A warning people should see (out-of-order merge), in chat and on Jira."""
        msg = self.chat.post(key, self._as("devops", text))
        if not self.minimal:
            self._jira(key, text, node, 0, extra)
        return msg

    def testing_off(self, key: str, by: str) -> str:
        """P10: started without `testing=true`: said once, in the thread and on Jira."""
        text = (f"The live acceptance tests are off for {key} ({by} started it without "
                "testing=true), so it goes from coding straight to the merge requests.")
        if self.minimal:
            self._delivery(key, text)
        else:
            self._jira(key, text, "testing_off", 0, swallow=True)
        return self.chat.post(key, self._as("coder", text))

    def live_tests(self, key: str, line: str, details: str = "") -> str:
        """P10: the live acceptance result. Minimal: the Jira summary and the merge-request card
        show it; normal: a message and a Jira comment."""
        if self.minimal:
            self._delivery(key, live=line)
            return ""
        text = f"Live acceptance tests: {line}" + (f" ({details})" if details else "") + "."
        self._jira(key, text, "acceptance", 0, swallow=True)
        return self.chat.post(key, self._as("reviewer", text))

    # ---------- testing (P7; the Coder's, D113) ----------

    def qa(self, key: str, text: str, card: dict | None = None, jira_md: str | None = None,
           node: str = "qa", n: int | str = 0, extra: str = "") -> str:  # fmt: skip
        """A testing message by the Coder in the chat (+ a card); `jira_md` is what goes on
        Jira (often more than the chat shows: the notes, the numbered bug list)."""
        msg = self.chat.post(key, self._as("coder", text), self._card_by("coder", card))
        if jira_md is not None:
            self._jira(key, jira_md, node, n, extra)
        return msg

    def waiting(self, key: str, gate: str, card: dict | None, round: int | str = 0,
                text: str = "") -> str:  # fmt: skip
        """Post the gate card (it says what it waits for); mirror it on Jira. Returns the card
        message id."""
        msg = self.chat.post(key, text, self._card_by(GATE_PERSONA.get(gate, "coder"), card))
        if self.minimal:
            self._delivery(key, status=f"Waiting for {gate_label(gate)} (in the terminal)")
        else:
            self._jira(key, f"Waiting for {gate_label(gate)} (in the terminal).", "gate_wait",
                       round, gate)  # fmt: skip
        return msg

    def gate_passed(self, key: str, gate: str, by: str, action: str,
                    message_id: str | None = None, round: int | str = 0) -> None:  # fmt: skip
        """`action`: a whole sentence ("Kushal Soni approved the plan."), shown on the card
        and in Jira."""
        if message_id:
            self.chat.update(message_id, text_md=action)
        elif not self.minimal:
            self.chat.post(key, action)
        if self.minimal:
            self._delivery(key, action, status="In progress")
        else:
            self._jira(key, action, "gate_pass", round, gate)

    def needs_human(self, key: str, reason: str, card: dict | None = None,
                    round: int | str = 0, step: str | None = None) -> str:  # fmt: skip
        """`reason` stays below as the details; the Chat LLM explains it in plain words on top
        (3.4), speaking as the role of the failed step."""
        stopped = reason.lower().startswith(("stopped by", "stop_requested"))
        persona = "bot" if stopped else STEP_PERSONA.get(step or "", "bot")
        where = step_words(step)
        if stopped:
            lead = f"{key} is paused and waiting for someone to decide what to do next."
            event = f"a person paused the ticket during {where}; it waits for a person"
        else:
            lead = f"I've paused {key} because I need your help."
            event = f"work paused during {where} because it needs a person"
        options = ("Tell me to try again, tell me what to do differently, or stop the ticket; "
                   "just write it in your own words.")  # fmt: skip
        summary = self.words.summary(
            key, persona, event,
            {"step": where, "what people can do (in their own words, no commands)":
             ["tell me to try again", "tell me what to do differently or what changed",
              "stop the ticket"]},  # fmt: skip
            reason, "",
        )  # fmt: skip
        plain = f"{lead} {summary or options}"
        text = plain if stopped else f"{plain}\n\n{reason}"
        body = plain if stopped else self._two(plain, reason, persona)
        msg = self.chat.post(key, self._as(persona, text), self._card_by(persona, card))
        if self.minimal:
            self._delivery(key, status=f"Needs a person: {plain}"
                           + ("" if stopped else f"\n\n{reason}"))  # fmt: skip
        else:
            self._jira(key, body, "needs_human", round, swallow=True)
        return msg

    def error(self, key: str, text: str, round: int | str = 0) -> str:
        msg = self.chat.post(key, text)
        if self.minimal:
            self._delivery(key, status=text)
        else:
            self._jira(key, text, "error", round, swallow=True)
        return msg


def _fingerprint(text: str) -> str:
    """Part of a plan comment's marker: a different plan is a new comment even when mode and
    counters repeat (DEMO-10: the questions and the plan after them shared `initial0r0`, so the
    plan was never posted); the same plan posted again is still skipped."""
    return hashlib.sha256(text.encode()).hexdigest()[:10]


PLAN_TITLES = {
    "initial": "Implementation plan",
    "revise": "Implementation plan, revised with people's feedback",
    "change": "Plan update for a change request",
    "replan": "Plan update during coding",
    "qa_fix": "Plan to fix the QA bugs",
}


def plan_title(plan: Any) -> str:
    """The heading of a plan's Jira comment (P9: one comment per plan version)."""
    if plan is not None and plan.status == "needs_clarification":
        return "Questions before planning"
    return PLAN_TITLES.get(getattr(plan, "mode", "initial"), "Implementation plan")


def plan_facts(key: str, plan: Any) -> tuple[dict, str, str]:
    """(facts, template summary, event) for a plan (None → no facts, no template)."""
    if plan is None:
        return {}, "", "plan ready"
    repos = [t.repo for t in sorted(plan.tasks, key=lambda t: t.order)]
    c = plan.contract
    new = [e.id for e in c.internal_endpoints if e.is_new] + [e.id for e in c.public_endpoints]
    if plan.status == "needs_clarification":
        n = len(plan.open_questions)
        facts = {"open questions": n, "what people can do":
                 "answer the questions here in the chat, in their own words (or add the answers "
                 "to the Jira description and tell me to try again)"}  # fmt: skip
        template = (f"Before I can plan {key}, I need answers to {plural(n, 'question')} "
                    "(below). Just answer them here, or add them to the Jira description and "
                    "tell me to try again.")  # fmt: skip
        return facts, template, "the planner needs answers before it can plan"
    if plan.mode in ("change", "replan"):  # an update after coding started: only the changes
        n = len(plan.changes_from_previous)
        what = "a change request" if plan.mode == "change" else "what came up while coding"
        facts = {"kind of plan": f"update for {what}", "changes": n,
                 "important": "only these changes are redone; the rest of the code stays",
                 "what people can do": "say if the plan looks good, or say what to change, in "
                 "their own words"}  # fmt: skip
        template = (f"I updated the plan for {key} for {what}: {plural(n, 'change')}, and only "
                    "those parts are redone. Approve it, or tell me what to change.")  # fmt: skip
        return facts, template, "an updated plan (only the changes) is ready for approval"
    facts = {
        "kind of plan": {
            "initial": "first plan",
            "revise": "updated with people's feedback",
            "replan": "updated during coding",
            "qa_fix": "plan to fix QA bugs",
        }.get(plan.mode, plan.mode),  # fmt: skip
        "repositories in coding order": repos,
        "new or changed endpoints": len(new),
        "how it works": plan.flow or None,
        "changes from the previous plan": (len(plan.changes_from_previous) or None)
        if plan.mode != "initial"
        else None,
        "what people can do": "say if the plan looks good, or say what to change, in their own "
        "words",
    }
    template = (f"The plan for {key} is ready: {plural(len(repos), 'repository', 'repositories')}"
                f" and {plural(len(new), 'new or changed endpoint')}. Approve it, or tell me what "
                "to change.")  # fmt: skip
    return facts, template, "the plan is ready for approval"
