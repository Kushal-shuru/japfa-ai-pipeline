"""Merge requests. `open_mrs` (review_mode gitlab): push each repo's branch, one MR per repo (short
description, D109), cross-linked; Jira stays In Dev (D127).

Local review (D207, the default): `start_review` only marks the repos ready for the Reviewer (no
push, no MR); after the approval `publish_mrs` rebases onto the latest default branch, pushes,
opens the MRs, posts one review summary note per MR and applies the Reviewer's approval."""

from __future__ import annotations

import re
from typing import Any

from pipeline.findings import numbered, numbers_by_id, one_line, open_findings, short
from pipeline.graph.nodes.common import board_id, has_mr, ordered, review_diff
from pipeline.graph.nodes.sync import sync_before_push
from pipeline.graph.nodes.waits import round_kind
from pipeline.graph.state import RepoWork, TicketState
from pipeline.markers import make_marker


def mr_title(key: str, summary: str, repo: str, role: str, qa_round: int = 0) -> str:
    if qa_round:
        return f"[{key}] QA fixes round {qa_round} — {repo}"
    return f"[{key}] {summary} — {repo} ({role})"


def _plain(text: str) -> str:
    """The whole text on one line (the first sentence only, cut at 200 characters, before)."""
    return short(text or "")


def mr_explanation(state: TicketState, deps: Any, repo: str) -> str:
    """2–3 plain sentences for reviewers (the `summary` agent, D209); two points as the fallback."""
    ctx = state["context"]
    plan = state.get("plan")
    task = next((t for t in plan.tasks if t.repo == repo), None) if plan else None
    coder = state["repos"][repo].get("coder_result")
    points = [p for p in (_plain(task.summary) if task else "",
                          _plain(coder.summary) if coder else "") if p]  # fmt: skip
    fallback = "\n".join(f"- {p}" for p in points)
    if not points or getattr(deps, "chat_llm", None) is None:
        return fallback
    from pipeline.chat.summaries import Summarizer

    words = Summarizer(deps.chat_llm, deps.config.chat_llm, deps.config.chat.agents)
    return words.summary(
        state["key"], "coder",
        "describe this merge request for code reviewers in 2 or 3 short sentences: what it "
        "changes and why; no file lists, no test counts, no next steps",
        {"ticket": ctx.summary if ctx else "", "repository": repo,
         "the task": task.summary if task else None,
         "what the coder did": coder.summary if coder else None},
        "", fallback,
    )  # fmt: skip


def mr_body(state: TicketState, deps: Any, repo: str, explanation: str,
            linked: bool = False) -> str:  # fmt: skip
    """Short (D109): the Jira link, a few sentences, and (once all MRs exist) one merge-order
    line linking the others."""
    key = state["key"]
    ctx = state["context"]
    jira = deps.secrets.jira_base_url.rstrip("/")
    title = f"{key}: {ctx.summary}" if ctx else key
    out = [f"**Jira:** [{title}]({jira}/browse/{key})" if jira else f"**Jira:** {title}"]
    if explanation.strip():
        out.append(explanation.strip())
    live = ((state.get("repos") or {}).get(repo) or {}).get("acceptance_result")
    if live is not None and live.results:  # P10
        from pipeline.graph.nodes.testing import live_line

        out.append(f"**Live acceptance tests:** {live_line(live)}")
    if linked:
        order = ordered(state)
        n = order.index(repo) + 1
        links = [f"{i}. [{r}]({state['repos'][r].get('mr_url')})" if r != repo else f"{i}. {r} "
                 "(this one)" for i, r in enumerate(order, 1)]  # fmt: skip
        if len(order) > 1:
            out.append(f"**Merge order** ({n} of {len(order)}): " + " → ".join(links))
    return "\n\n".join(out) + "\n"


def _open_all(
    state: TicketState, deps: Any, repos: dict, leases: dict[str, str], todo: list[str],
    status: str,
) -> tuple[dict[str, RepoWork], dict[str, str]]:
    """Push each repo of `todo`, open (or find) its MR, then link them all. Returns the repos
    with `mr_iid` / `mr_url` / `status` and the one-paragraph explanations."""
    key = state["key"]
    ctx = state["context"]
    ws = deps.workspaces
    explained: dict[str, str] = {}
    for repo in todo:
        work = repos[repo]
        cfg = deps.config.repo(repo)
        ws.push(ws.worktree_path(key, repo), work["branch"], cfg.default_branch, leases.get(repo))
        explained[repo] = mr_explanation(state, deps, repo)
        body = mr_body({**state, "repos": repos}, deps, repo, explained[repo])
        existing = deps.gitlab.find_mr(cfg.gitlab_project, work["branch"])
        if existing is not None and existing.get("state") == "opened":
            mr = existing
        else:
            title = mr_title(key, ctx.summary, repo, cfg.role, state.get("qa_round", 0))
            mr = deps.gitlab.create_mr(
                cfg.gitlab_project, work["branch"], cfg.default_branch, title, body
            )
        work.update(status=status, mr_iid=mr["iid"], mr_url=mr["web_url"])
        deps.store.upsert_mr(
            key, repo, round_kind(state), branch=work["branch"], mr_iid=mr["iid"],
            mr_url=mr["web_url"], state="opened",
        )  # fmt: skip
    linked = {**state, "repos": repos}
    for repo in todo:
        cfg = deps.config.repo(repo)
        body = mr_body(linked, deps, repo, explained[repo], linked=True)
        deps.gitlab.update_mr_description(cfg.gitlab_project, repos[repo]["mr_iid"], body)
    return repos, explained


def open_mrs(state: TicketState, deps: Any) -> dict:
    key = state["key"]
    # D125: rebase onto the latest default branch (and re-check) before anything is pushed
    back, leases, repos = sync_before_push(state, deps, "open_mrs", ordered(state))
    if back is not None:
        return back
    repos, _ = _open_all(state, deps, repos, leases, ordered(state), "mr_open")
    deps.notifier.repo_progress(
        key, repos, board=board_id(state)
    )  # final state of each repository
    deps.notifier.mrs_opened(
        key,
        [{"repo": r, "role": repos[r]["role"], "url": repos[r]["mr_url"]} for r in ordered(state)],
        round=f"qa{state.get('qa_round', 0)}" if state.get("qa_round") else "initial",
    )
    deps.store.upsert_ticket(key, waiting_for=None)  # Jira stays In Dev until merged (D127)
    return {"repos": repos, "last_event": {"type": "mrs_opened"}}


# ---------- local review (D207) ----------


def start_review(state: TicketState, deps: Any) -> dict:
    """Coded, checked and live-tested: ready for the Reviewer. Nothing leaves this machine."""
    key = state["key"]
    repos: dict[str, RepoWork] = {r: dict(w) for r, w in state["repos"].items()}  # type: ignore[misc]
    for repo in ordered(state):
        if repos[repo].get("status") != "merged":
            repos[repo]["status"] = "mr_open"  # "ready for review"; no merge request yet
    deps.store.upsert_ticket(key, waiting_for=None)
    return {"repos": repos, "last_event": {"type": "review_ready"}}


def _patch(text: str) -> str:
    """A diff without what a rebase legitimately changes (blob ids, hunk line numbers): two
    equal patches are the same change."""
    out = []
    for line in text.splitlines():
        if line.startswith("index "):
            continue
        out.append(re.sub(r"^@@ [^@]*@@", "@@", line) if line.startswith("@@") else line)
    return "\n".join(out)


def review_note(state: TicketState, repo: str) -> str:
    """The one comment on a published MR: how the local review went. Whole, never shortened."""
    work = state["repos"][repo]
    res = work.get("review_result")
    log = [e for e in state.get("review_log") or [] if e.get("repo") == repo]
    rounds = state.get("review_round", 0)
    override = state.get("override")
    out = [f"**Reviewed locally before this merge request was opened** · "
           f"{rounds} review round{'s' if rounds != 1 else ''}"]
    if override:
        out[0] += f" · accepted as it is by {override.get('by')} ({override.get('at')})"
    if res is not None and res.summary.strip():
        out.append(res.summary.strip())
    titles = (("new", "Raised"), ("resolved", "Fixed"), ("rejected", "Closed by a person"))
    for rnd in sorted({e["round"] for e in log}):
        lines = [f"- {word}: {e['line']}" for kind, word in titles for e in log
                 if e["round"] == rnd and e["kind"] == kind]  # fmt: skip
        if lines:
            out.append(f"**Round {rnd}**\n\n" + "\n".join(lines))
    numbers = numbers_by_id(state)
    left = [f for f in open_findings(state) if f.repo == repo]
    if left:
        what = "Accepted without fixing" if override else "Left open (not blocking)"
        out.append(f"**{what}**\n\n" + "\n".join(f"- {one_line(numbers.get(f.id, 0), f)}"
                                                    for f in left))  # fmt: skip
    if res is not None and res.acceptance_criteria_check:
        rows = []
        for c in res.acceptance_criteria_check:
            tail = f" ({c.evidence})" if c.evidence else ""
            tail += f": {c.note}" if c.note else ""
            rows.append(f"- {c.criterion}: {c.status}{tail}")
        out.append("**Acceptance criteria**\n\n" + "\n".join(rows))
    return "\n\n".join(out)


def publish_mrs(state: TicketState, deps: Any) -> dict:
    """The Reviewer approved the local branches: rebase onto the latest default branch (D125),
    push, open the MRs, post the review summary and the approval on each. A rebase that changed
    the code under review sends it back to the review; failing checks back to the coder."""
    key = state["key"]
    ws = deps.workspaces
    todo = [r for r in ordered(state) if not has_mr(state, r)
            and state["repos"][r].get("status") != "merged"]  # fmt: skip
    if not todo:
        return {"last_event": {"type": "mrs_opened"}}
    before = {r: _patch(review_diff(deps, state, r)) for r in todo}
    back, leases, repos = sync_before_push(state, deps, "publish_mrs", todo)
    if back is not None:
        return back
    after = {r: _patch(review_diff(deps, {**state, "repos": repos}, r)) for r in todo}
    changed = [r for r in todo if after[r] != before[r]]
    if changed:  # not the code the Reviewer approved any more
        deps.notifier.progress(key, "Bringing the branches up to date with the latest code "
                               f"changed {', '.join(changed)}, so the Reviewer looks at "
                               "it again before the merge requests open.", "coder")  # fmt: skip
        return {"repos": repos, "override": state.get("override"),
                "last_event": {"type": "stale_review", "repos": changed}}  # fmt: skip
    for repo in todo:  # same change on a newer base: the approval carries over to the new commit
        repos[repo]["reviewed_sha"] = ws.head(ws.worktree_path(key, repo))
    repos, _ = _open_all(state, deps, repos, leases, todo, "approved")
    final = {**state, "repos": repos}
    round_ = state.get("review_round", 0)
    for repo in todo:
        project = deps.config.repo(repo).gitlab_project
        iid = repos[repo]["mr_iid"]
        deps.gitlab.add_note(project, iid, review_note(final, repo),
                             make_marker(key, "review", round_, "summary-local"))  # fmt: skip
        deps.gitlab.approve(project, iid, sha=repos[repo].get("reviewed_sha"))
    mrs = [{"repo": r, "role": repos[r]["role"], "url": repos[r]["mr_url"]} for r in ordered(state)]
    override = state.get("override")
    left = [] if override else [one_line(n, f) for n, f in numbered(final)]
    deps.notifier.repo_progress(key, repos, board=board_id(state))
    deps.notifier.mrs_opened(
        key, mrs, round=f"qa{state.get('qa_round', 0)}" if state.get("qa_round") else "initial")
    deps.notifier.approved(key, mrs, override, round=round_, left=left)
    deps.store.upsert_ticket(key, waiting_for=None)  # In Dev until merged (D127)
    return {"repos": repos, "last_event": {"type": "approved"}}

