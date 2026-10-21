"""Deployment note (Phase 6 §2): the `deploy_doc` gate and the `deploy_doc` node.

One note per ticket, for every repo in it. `deploy_doc` runs right **after** the MRs are
approved (D121: the review passed or a person accepted), and again on `deploy-doc KEY` while
the ticket waits for the merges (2.6):

1. collect deterministic DevOps facts per repo from the worktrees, which are at the reviewed,
   approved commits (P9; `analysis/devops_facts.py`); `needs_devops` comes from these facts only;
2. the Deployment Agent (deploy slot, read-only) writes the note for the change set;
3. consistency: every fact must appear, and a repo with migrations needs its Rollback (undo)
   section (D123; else "Auto-detected but not described" is appended);
   `deploy_order` must list every repo once with services → gateway → frontend (else the
   pipeline's order is used and the note says so);
4. outputs: Jira (the `.md` attached + a one-line comment pointing at it, D122) and the
   Deployment card by DevOps in the chat (D113), which links to Jira (D114). Nothing goes to
   GitLab (D121);
5. the ticket then waits for the merges (`waiting_for` = `merge_all`).
"""

from __future__ import annotations

from typing import Any

from pipeline.agents import deployment
from pipeline.analysis.devops_facts import (
    append_missing,
    collect,
    devops_reasons,
    missing_facts,
    missing_rollbacks,
    needs_devops,
)
from pipeline.chat.cards import Action, Card
from pipeline.chat.summaries import error_words, join_words, plural
from pipeline.graph.gates import gate_prepare, gate_wait
from pipeline.graph.nodes.common import after_run, agent_slot, jira_url, ordered, review_diff
from pipeline.graph.nodes.waits import checklist_md
from pipeline.graph.state import RepoWork, TicketState
from pipeline.graph.wrapper import halt
from pipeline.schemas import DeployResult, DevopsFacts

ROLE_RANK = {"service": 0, "gateway": 1, "frontend": 2}


def attachment_name(key: str, qa_round: int = 0) -> str:
    """One file per round: a QA round's note doesn't replace the first one (D126)."""
    return f"{key}-deployment-qa{qa_round}.md" if qa_round else f"{key}-deployment.md"


def notes_written(deps: Any, key: str) -> int:
    return sum(1 for e in deps.store.events_for(key, "node_end") if e.get("node") == "deploy_doc")


# ---------- gate ----------


def deploy_card(state: TicketState, deps: Any) -> Card:
    key = state["key"]
    return Card(
        title=f"{key}: deployment note",
        sections=[
            "The merge requests are approved. Next I write the deployment note for the whole "
            "change set and post it here and on Jira."
        ],  # fmt: skip
        actions=[
            Action("Write deployment note", f"deploy-doc {key}", style="positive"),
            Action("Stop", f"stop {key} --confirm", style="destructive"),
        ],
    )


def deploy_doc_gate(state: TicketState, deps: Any) -> dict:
    return gate_prepare("deploy_doc", state, deps, deploy_card,
                        round=state.get("review_round", 0))  # fmt: skip


def deploy_doc_gate_wait(state: TicketState, deps: Any) -> dict:
    return gate_wait("deploy_doc", state, deps, ("deploy_doc",),
                     round=state.get("review_round", 0), node="deploy_doc_gate_wait")  # fmt: skip


# ---------- order ----------


def check_order(state: TicketState, proposed: list[str]) -> tuple[list[str], str]:
    """(order to use, note). The agent's order is kept when it lists every repo once and never
    puts a consumer before a provider by role (services → gateway → frontend); services may be
    reordered among themselves (the planner may order dependent services)."""
    default = ordered(state)
    repos = state["repos"]

    def rank(r: str) -> int:
        return ROLE_RANK.get(repos[r].get("role", "service"), 0)

    if sorted(proposed) == sorted(default) and len(set(proposed)) == len(proposed):
        ranks = [rank(r) for r in proposed]
        if ranks == sorted(ranks):
            return proposed, ""
    note = (f"The pipeline corrected the deploy order to {' → '.join(default)} (services first, "
            f"then the gateway, then the frontend); the note proposed "
            f"{' → '.join(proposed) or 'no order'}.")  # fmt: skip
    return default, note


# ---------- node ----------


def _facts(state: TicketState, deps: Any) -> dict[str, DevopsFacts]:
    key = state["key"]
    ws = deps.workspaces
    out: dict[str, DevopsFacts] = {}
    for repo in ordered(state):
        cfg = deps.config.repo(repo)
        path = ws.worktree_path(key, repo)
        # P9 (audit F3): no refresh here: the worktree is at the reviewed, approved commit (the
        # approval checked the MR head), so the note describes exactly that code. Commits
        # pushed later are seen by the merge wait, which sends the ticket back to review.
        out[repo] = collect(cfg, path, f"origin/{cfg.default_branch}")
    return out


def counts_line(f: DevopsFacts) -> str:
    env = len(set(f.new_env_vars + f.removed_env_vars + f.undeclared_env_vars
                  + f.build_time_env_vars))  # fmt: skip
    parts = [plural(env, "environment variable change") if env else "no environment variable "
             "changes",
             plural(len(f.migration_files), "migration") if f.migration_files else "no migrations",
             plural(len(f.infra_files_changed), "infrastructure file") if f.infra_files_changed
             else "no infrastructure changes"]  # fmt: skip
    return ", ".join(parts)


def _nothing_for_devops(state: TicketState, deps: Any, facts: dict[str, DevopsFacts]) -> dict:
    key = state["key"]
    order = ordered(state)
    text = (f"The QA fixes for {key} change nothing for DevOps (no environment variables, "
            "migrations or infrastructure), so the deployment note from before still applies. "
            f"Merge them in this order: {' → '.join(order)}.")  # fmt: skip
    deps.notifier.warning(key, text, "deploy_doc", f"qa{state.get('qa_round', 0)}")
    result = DeployResult(doc_markdown="", deploy_order=order, risk_level="low",
                          needs_devops=False)  # fmt: skip
    new_repos: dict[str, RepoWork] = {r: {**w, "devops_facts": facts.get(r)}
                                      for r, w in state["repos"].items()}  # fmt: skip
    deps.store.upsert_ticket(key, waiting_for="merge_all")
    return {"repos": new_repos, "deploy_result": result, "waiting_for": "merge_all",
            "last_event": {"type": "deploy_doc", "regenerate": False, "n": 0}}  # fmt: skip


def deploy_doc(state: TicketState, deps: Any) -> dict:
    key = state["key"]
    qa = state.get("qa_round", 0)
    repos = ordered(state)
    regenerate = state.get("deploy_result") is not None  # `deploy-doc` again, or a re-review
    facts = _facts(state, deps)
    all_facts = [facts[r] for r in repos]
    if qa and not needs_devops(all_facts):  # D126: a QA round's fixes need no new note
        return _nothing_for_devops(state, deps, facts)
    diffs = {r: review_diff(deps, state, r) for r in repos}
    with agent_slot(state, deps, "deployment") as slot:
        deps.notifier.progress(key, f"Writing the deployment note for {key}.", "devops")
        result, run = deployment.write_note(
            deps, key, repos, all_facts, diffs, override=state.get("override"),
            slot=slot.name, provider_override=state.get("provider_override"),
        )  # fmt: skip
    update = after_run(state, deps, slot)
    if result is None:
        return {**update, **halt("deploy_doc", "Writing the deployment note failed twice: "
                                               f"{error_words(run.error)}")}  # fmt: skip

    order, order_note = check_order(state, list(result.deploy_order))
    doc = result.doc_markdown.rstrip() + "\n"
    if order_note:
        doc += f"\n> {order_note}\n"
    missing = missing_facts(doc, all_facts) + missing_rollbacks(doc, all_facts)
    doc = append_missing(doc, missing)
    devops = needs_devops(all_facts)
    result = result.model_copy(update={"doc_markdown": doc, "deploy_order": order,
                                       "needs_devops": devops})  # fmt: skip

    n = notes_written(deps, key) + 1
    deps.jira.attach(key, attachment_name(key, qa), doc.encode())
    reasons = sorted({r for f in all_facts for r in devops_reasons(f)})
    devops_words = f"yes ({join_words(reasons)})" if devops else "no"
    per_repo = [f"- **{r}** ({state['repos'][r].get('role', '')}): {counts_line(facts[r])}"
                for r in order]  # fmt: skip
    url = jira_url(deps, key)
    where = f"[the Jira ticket]({url})" if url else "the Jira ticket"
    sections = ["\n".join(per_repo)]
    if devops:  # D124: DevOps does its part before the merge requests are merged
        sections.append("**Before merging, DevOps:**\n\n" + checklist_md(order, facts))
    sections.append(f"The whole note is attached to {where} as {attachment_name(key, qa)}.")
    card = Card(
        title=f"{key}: deployment note",
        facts=[("Risk", result.risk_level), ("Deploy order", " → ".join(order)),
               ("DevOps required", devops_words)],
        sections=sections,
    )  # fmt: skip
    if missing:
        card.sections.append(f"I added {plural(len(missing), 'detected item')} the note didn't "
                             "describe at the end of the note. Please check them.")  # fmt: skip
    again = "an updated" if regenerate else "the"
    fallback = (
        f"I've written {again} deployment note for {key} and posted it on Jira. Risk "
        f"is {result.risk_level}; DevOps {'is needed' if devops else 'is not needed'}."
    )
    after = (
        (
            "DevOps does the steps on the card before the merges; after the last merge it "
            "goes to testing"
        )
        if devops
        else "after the last merge it goes to testing"
    )
    notifier = deps.notifier
    # P9: minimal mode posts this sentence only when DevOps has work; don't ask the Chat LLM
    # for words nobody reads (the card carries the facts)
    summary = fallback if notifier.minimal and not devops else notifier.words.summary(
        key, "devops", "the deployment note for the change set is written",
        {"risk": result.risk_level, "deploy order": order, "DevOps needed": devops_words,
         "updated note": regenerate, "per repository": per_repo,
         "next step": f"people merge the merge requests in the deploy order; {after}",
         "important": "this is information, not a question: nothing needs approving and there "
                      "is no plan to confirm; don't ask people to approve or try again"},
        "", fallback,
    )  # fmt: skip
    notifier.deploy_note(key, summary, attachment_name(key, qa), card.to_dict(), n, regenerate)
    if not regenerate:  # 2.1: people outside the thread merge and do the DevOps work
        mrs = plural(len(order), "merge request")
        deps.notifier.channel(key, f"approved and ready to merge ({mrs}, in the order "
                              f"{' → '.join(order)}). "
                              + (f"DevOps first: {join_words(reasons)}." if devops
                                 else "Nothing for DevOps."))  # fmt: skip

    new_repos: dict[str, RepoWork] = {r: {**w, "devops_facts": facts.get(r)}
                                      for r, w in state["repos"].items()}  # fmt: skip
    deps.store.upsert_ticket(key, waiting_for="merge_all")
    return {**update, "repos": new_repos, "deploy_result": result, "waiting_for": "merge_all",
            "last_event": {"type": "deploy_doc", "regenerate": regenerate, "n": n}}  # fmt: skip
