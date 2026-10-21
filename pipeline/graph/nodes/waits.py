"""The merge wait (Phase 6 §4): `wait_merge` ⟲ → `post_qa_notes` (RFT, `nodes/qa.py`). DevOps
works before the merges (D124: the checklist is on the deployment card), so all merged = ready
for testing.

`wait_merge` pauses first (`interrupt({"gate": "merge_all"})`) and does its work only after
the resume, so nothing runs twice when LangGraph re-runs the node. The GitLab poller
(`events.GitLabMergePoller`) resumes it with a **snapshot** of every MR (state + commits by
people), only when something differs from what the ticket already knows; the node compares the
snapshot with `state.repos` and:

- marks newly merged repos (`ticket_mrs` too) and posts "… is merged (1 of 2)"; a repo merged
  while a repo it depends on is still open → an out-of-order warning (chat + Jira);
- a closed MR → Needs Human;
- commits by people on an AI branch that the worktree doesn't have yet → back to the review
  gate; the review refreshes the worktree (K35), which makes them known;
- `change` (only before any merge) → the change loop; `fix` (only before any merge, D168) →
  the findings the review left open go back into the fix loop; `deploy-doc` → a new note;
  `stop` → halt.

Duplicate, stale or partial snapshots are harmless: the router waits again until every MR is
merged. Merges while the server was down are seen at the next poll (GitLab is the source).
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from pipeline.analysis.devops_facts import devops_reasons, is_seeder
from pipeline.chat.summaries import join_words, plural
from pipeline.graph.nodes.common import ordered
from pipeline.graph.nodes.review import mr_of
from pipeline.graph.state import RepoWork, TicketState
from pipeline.graph.wrapper import halt
from pipeline.logging import get_logger

MERGE_GATE = "merge_all"
MERGE_ACTIONS = ("mr_update", "change", "deploy_doc", "fix", "stop")


def round_kind(state: TicketState) -> str:
    qa = state.get("qa_round", 0)
    return f"qa{qa}" if qa else "initial"


def new_foreign_commits(
    deps: Any, state: TicketState, repo: str, commits: list[dict]
) -> list[dict]:
    """Commits on the MR branch that the worktree doesn't have yet (a review since then pulled
    them in, so they count once). Every commit the pipeline makes is in its worktree, so any
    other commit is new, **whatever author name it carries** (P9, audit F3: a commit that
    borrows the bot's name used to slip through)."""
    ws = deps.workspaces
    path = ws.worktree_path(state["key"], repo)
    return [c for c in commits if not ws.has_commit(path, str(c.get("id") or ""))]


def snapshot(deps: Any, state: TicketState) -> dict:
    """What GitLab says now: {"states": {repo: state}, "commits": {repo: [new foreign]}}."""
    states: dict[str, str] = {}
    commits: dict[str, list[dict]] = {}
    for repo in ordered(state):
        project, iid = mr_of(deps, state, repo)
        states[repo] = deps.gitlab.mr_state(project, iid)
        if states[repo] == "opened":
            found = new_foreign_commits(deps, state, repo, deps.gitlab.mr_commits(project, iid))
            if found:
                commits[repo] = [{"id": c.get("id"), "short_id": c.get("short_id"),
                                  "author_name": c.get("author_name"), "title": c.get("title")}
                                 for c in found]  # fmt: skip
    return {"states": states, "commits": commits}


def has_news(state: TicketState, snap: dict) -> bool:
    """Would this snapshot change anything? (the poller resumes only then)"""
    repos = state.get("repos") or {}
    for repo, st in (snap.get("states") or {}).items():
        known = repos.get(repo, {}).get("status")
        if st == "closed" or (st == "merged" and known != "merged"):
            return True
    return bool(snap.get("commits"))


def providers_of(state: TicketState, repo: str) -> list[str]:
    """Repos that must be merged before `repo`: the providers of the endpoints it consumes
    (contract); without contract data, every repo earlier in the merge order."""
    plan = state.get("plan")
    order = ordered(state)
    if plan is not None:
        task = next((t for t in plan.tasks if t.repo == repo), None)
        by_id = {e.id: e.provider_repo for e in plan.contract.internal_endpoints}
        found = sorted({by_id[i] for i in (task.consumes if task else []) if i in by_id} - {repo})
        if found or task is not None and task.consumes:
            return [r for r in order if r in found]
    return order[: order.index(repo)] if repo in order else []


# ---------- wait_merge ----------


def wait_merge(state: TicketState, deps: Any) -> dict:
    key = state["key"]
    payload = interrupt({"gate": MERGE_GATE, "message_id": None})
    if not isinstance(payload, dict) or payload.get("gate") != MERGE_GATE \
            or payload.get("action") not in MERGE_ACTIONS:  # fmt: skip
        get_logger(ticket=key, node="wait_merge").warning("stale_resume", payload=payload)
        return {"waiting_for": MERGE_GATE}
    action = payload["action"]
    by = str(payload.get("by") or "someone")
    deps.store.record_event(key, "resume", node="wait_merge",
                            data={"action": action, "by": by})  # fmt: skip
    if action == "stop":
        deps.notifier.gate_passed(key, MERGE_GATE, by, f"{by} stopped the ticket.")
        deps.store.upsert_ticket(key, waiting_for=None)
        return {"waiting_for": None, **halt("wait_merge", f"Stopped by {by}.")}
    if action == "fix":
        return _fix_left_open(state, deps, by, dict(payload.get("args") or {}))
    if action in ("change", "deploy_doc"):
        deps.store.upsert_ticket(key, waiting_for=None)
        return {"waiting_for": None,
                "last_event": {"type": "gate", "gate": MERGE_GATE, "action": action, "by": by,
                               "args": dict(payload.get("args") or {})}}  # fmt: skip
    return _apply_snapshot(state, deps, payload)


def _apply_snapshot(state: TicketState, deps: Any, snap: dict) -> dict:
    key = state["key"]
    order = ordered(state)
    repos: dict[str, RepoWork] = {r: dict(w) for r, w in state["repos"].items()}  # type: ignore[misc]
    states = snap.get("states") or {}

    closed = [r for r in order if states.get(r) == "closed"]
    if closed:
        links = join_words([f"{r} ({repos[r].get('mr_url')})" for r in closed])
        deps.store.upsert_ticket(key, waiting_for=None)
        return {"waiting_for": None, **halt(
            "wait_merge", f"The merge request for {links} was closed without merging. Reopen it "
            "and try again, or stop the ticket.")}  # fmt: skip

    newly = [r for r in order if states.get(r) == "merged" and repos[r].get("status") != "merged"]
    for repo in newly:
        repos[repo]["status"] = "merged"
        deps.store.upsert_mr(key, repo, round_kind(state), state="merged")
        merged = [r for r in order if repos[r].get("status") == "merged"]
        iid = repos[repo].get("mr_iid")
        text = f"{repo} !{iid} is merged ({len(merged)} of {len(order)})."
        if len(merged) < len(order):
            left = [r for r in order if repos[r].get("status") != "merged"]
            text += f" Still to merge: {join_words(left)}."
        deps.notifier.merged(key, text, repo, round_kind(state))
        early = [p for p in providers_of(state, repo) if repos[p].get("status") != "merged"]
        if early:
            deps.notifier.warning(
                key, f"{repo} was merged before {join_words(early)}, which it depends on. "
                f"Deploy {join_words(early)} first, or {repo}'s new calls will fail until "
                "then.", "merge_order", repo)  # fmt: skip

    commits = {r: c for r, c in (snap.get("commits") or {}).items()
               if c and repos.get(r, {}).get("status") != "merged"}  # fmt: skip
    if commits:
        return _back_to_review(state, deps, repos, commits)

    if all(repos[r].get("status") == "merged" for r in order):
        deps.store.upsert_ticket(key, waiting_for=None)
        return {"repos": repos, "waiting_for": None, "last_event": {"type": "all_merged"}}
    deps.store.upsert_ticket(key, waiting_for=MERGE_GATE)
    return {"repos": repos, "waiting_for": MERGE_GATE,
            "last_event": {"type": "merge_progress", "merged": newly}}  # fmt: skip


def _fix_left_open(state: TicketState, deps: Any, by: str, args: dict) -> dict:
    """D168: a person asks for findings the review left open (minors, nits) to be fixed after
    the approval: the merge requests go back to "open" and the fix loop takes them, then the
    review approves the new commits again."""
    from pipeline.findings import numbers_by_id
    from pipeline.graph.nodes.fixing import choose, refs

    key = state["key"]
    scope = choose(state, deps, args, by)
    repos: dict[str, RepoWork] = {r: dict(w) for r, w in state["repos"].items()}  # type: ignore[misc]
    for repo in repos:
        if repos[repo].get("status") == "approved":
            repos[repo]["status"] = "mr_open"
    deps.store.upsert_ticket(key, waiting_for=None)  # still In Dev (D127)
    deps.store.record_event(key, "gate_pass", node="wait_merge",
                            data={"action": "fix", "by": by, "ids": scope["ids"]})  # fmt: skip
    deps.notifier.gate_passed(key, MERGE_GATE, by, (
        f"{by} asked for {refs(scope['ids'], numbers_by_id(state))} to be fixed before the "
        "merge. The merge requests are reviewed and approved again after that."),
        round=state.get("review_round", 0))  # fmt: skip
    return {
        "repos": repos,
        "waiting_for": None,
        "fix_scope": scope,
        "last_event": {"type": "gate", "gate": MERGE_GATE, "action": "fix", "by": by},
    }


def _back_to_review(state: TicketState, deps: Any, repos: dict[str, RepoWork],
                    commits: dict[str, list[dict]]) -> dict:  # fmt: skip
    key = state["key"]
    people = sorted({str(c.get("author_name") or "someone") for cs in commits.values()
                     for c in cs})  # fmt: skip
    where = join_words(list(commits))
    count = sum(len(c) for c in commits.values())
    for repo in repos:
        if repos[repo].get("status") == "approved":
            repos[repo]["status"] = "mr_open"
    deps.store.upsert_ticket(key, waiting_for=None)  # still In Dev (D127)
    first = min(str(c.get("id") or "") for cs in commits.values() for c in cs)
    deps.notifier.warning(
        key, f"{join_words(people)} pushed {plural(count, 'commit')} to {where} after the "
        "approval. The code has to be reviewed again, and the deployment note is updated "
        "after that.", "human_commits", first[:12],
    )  # fmt: skip
    return {"repos": repos, "waiting_for": None,
            "last_event": {"type": "human_commits", "repos": list(commits)}}  # fmt: skip


# ---------- DevOps checklist (on the deployment card, before the merges; D124) ----------


def checklist_md(order: list[str], facts: dict) -> str:
    """What DevOps must do before the merge requests are merged, per repo, from the facts."""
    lines = [f"Deploy in this order: {' → '.join(order)}.", ""]
    for repo in order:
        f = facts.get(repo)
        if f is None or not devops_reasons(f):
            lines.append(f"- **{repo}**: deploy as usual (nothing extra).")
            continue
        items = []
        env = f.new_env_vars + f.undeclared_env_vars + f.build_time_env_vars
        if env:
            items.append("set " + ", ".join(f"`{v}`" for v in dict.fromkeys(env)))
        if f.removed_env_vars:
            items.append("remove " + ", ".join(f"`{v}`" for v in f.removed_env_vars))
        migrations = [m for m in f.migration_files if not is_seeder(m)]
        seeders = [m for m in f.migration_files if is_seeder(m)]
        for scripts, what in ((migrations, "migration"), (seeders, "seeder")):
            if scripts:
                names = ", ".join(f"`{m.rsplit('/', 1)[-1]}`" for m in scripts)
                then = " after the migrations" if what == "seeder" and migrations else ""
                items.append(f"run {plural(len(scripts), what)}{then} ({names})")
        if f.infra_files_changed:
            files = ", ".join(f"`{p}`" for p in f.infra_files_changed)
            items.append(f"apply infrastructure changes ({files})")
        items += [n for n in f.notes if n.startswith(("Queue:", "Cron:", "Worker:"))]
        lines.append(f"- **{repo}**: " + "; ".join(items) + ".")
    return "\n".join(lines)
