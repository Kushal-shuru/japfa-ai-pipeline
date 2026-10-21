"""`code_next_repo` (every pending repo at once, D129) and `verify` (orchestrator checks, one
repo per pass, lowest order first)."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pipeline.agents.coder import (
    code_repo,
    code_ticket,
    commit_message,
    commit_plan,
    ticket_commit_plan,
)
from pipeline.chat.summaries import error_words, join_words, plural
from pipeline.environment import LEAD, machine_problem
from pipeline.graph.nodes.change import split_changes
from pipeline.graph.nodes.common import (
    after_run,
    agent_slot,
    board_id,
    bullets,
    is_local,
    ordered,
    pending_repos,
)
from pipeline.graph.nodes.fixing import fix_inputs
from pipeline.graph.state import TicketState
from pipeline.graph.wrapper import halt
from pipeline.integrations.base import IntegrationError
from pipeline.logging import get_logger
from pipeline.verify import WARNING, failures_md, shared
from pipeline.workspace import AGENT_DIR

# plans updated after coding started: a repo that already has an MR is redone, not recoded
REDO_MODES = frozenset({"change", "replan", "revise"})


READY = ('mr_open', 'changes_requested', 'approved')  # D207: reviewed locally, no MR yet


def redo_changes(state: Any, repo: str) -> list[str] | None:
    """The plan's changes for `repo` when its code already exists in an MR (D130); None =
    a normal coding run. Without a line naming the repo, all of the plan's changes."""
    plan = state.get("plan")
    work = (state.get("repos") or {}).get(repo) or {}
    # the code exists once it is in an MR, or (local review, D207) once it has been handed to review
    exists = work.get("mr_iid") or (is_local(state) and work.get("status") in READY)
    if plan is None or plan.mode not in REDO_MODES or not exists:
        return None
    mine = [w for r, w in split_changes(plan.changes_from_previous, ordered(state)) if r == repo]
    return mine or list(plan.changes_from_previous)


def protect_shared(deps: Any, key: str, repo: str, result: Any) -> Any:
    """The repo's `shared_paths` (ws1: the authentication middlewares, `validateSchema`) are never
    changed by a coder, whichever provider ran it: an edit or deletion of an existing protected
    file is undone here, before anything is committed, and said in `open_questions`. The coder is
    told the same in its prompt (and Claude coders can't edit them at all); verify's
    `shared_code` check stays as a last line."""
    cfg = deps.config.repo(repo)
    if not cfg.shared_paths:
        return result
    ws = deps.workspaces
    path = ws.worktree_path(key, repo)
    try:
        changed = ws.changed_paths(path, cfg.default_branch)["vs_default"]
        touched = [p for p in changed if shared(p, cfg.shared_paths)]
        restored = ws.restore_from_default(path, cfg.default_branch, touched)
    except IntegrationError as err:  # verify's check still catches it
        get_logger(ticket=key, repo=repo).warning("shared_restore_failed", error=str(err))
        return result
    if not restored:
        return result
    get_logger(ticket=key, repo=repo).warning("shared_code_restored", files=restored)
    note = ("shared code: I undid changes to protected " + ", ".join(f"`{f}`" for f in restored)
            + "; the pipeline never lets a coder change these files. If the task needs a change "
            "there, a person decides.")  # fmt: skip
    return result.model_copy(update={"open_questions": [*result.open_questions, note]})


def _code_one(state: TicketState, deps: Any, repo: str) -> tuple[Any, Any, Any]:
    """One coder run in its own slot: (result, run, slot). Commits leftovers on success."""
    key = state["key"]
    previous = state["repos"][repo].get("verify_result")
    failures = failures_md(previous) if previous is not None and not previous.ok else None
    findings, decision = fix_inputs(state, repo)
    install = getattr(deps.verifier, "install", None)
    if install is not None:  # before holding a slot: the coder needn't wait on npm (D129)
        install(key, repo)
    with agent_slot(state, deps, "coder") as slot:
        result, run = code_repo(
            deps, key, repo, slot=slot.name, provider_override=state.get("provider_override"),
            verify_failures=failures, guidance=state.get("guidance"), fix=findings is not None,
            findings=findings, human_decision=decision, redo=redo_changes(state, repo),
        )  # fmt: skip
    if result is not None and result.status == "success":
        ws = deps.workspaces
        result = _commit_repo(deps, key, repo, result, findings is not None,
                              commit_plan(ws.worktree_path(key, repo)))  # fmt: skip
    return result, run, slot


def _commit_repo(deps: Any, key: str, repo: str, result: Any, fixing: bool,
                 entries: list[tuple[str, list[str]]]) -> Any:  # fmt: skip
    """A coder's work in one repo as small commits (D169): one per entry of its commit plan
    (one per module or mini feature), then one for what the plan didn't name. Made here: Codex
    can't write .git, and repo hooks (ws2 runs the whole suite on every commit) would make each
    one slow; verify runs every check before the push. An edit of a protected file is undone
    first."""
    result = protect_shared(deps, key, repo, result)
    ws = deps.workspaces
    path = ws.worktree_path(key, repo)
    kind = "fix" if fixing else "feat"
    author = deps.config.git.coder_author
    try:
        ws.commit_plan(path, entries, author, lambda m: commit_message(m, kind, key))
    except IntegrationError as err:  # small commits are a nicety: never stop the ticket
        get_logger(ticket=key, repo=repo).warning("commit_plan_failed", error=str(err))
    # what the plan didn't name: one more commit with the coder's summary as its message
    what = re.split(r"(?<=[.!?])\s", result.summary.strip(), maxsplit=1)[0]
    ws.commit_all(path, commit_message(what, kind, key), author)
    return result


def _code_batch(state: TicketState, deps: Any, batch: list[str]) -> dict[str, tuple[Any, Any, Any]]:
    """agents.single_session: ONE coder session codes (or fixes) every repo of the batch, in plan
    order, in the ticket's folder. Same shape as `code_next_repo`'s per-repo outcomes: a result
    per repo (None = the session didn't report it), the shared run, the slot."""
    key = state["key"]
    install = getattr(deps.verifier, "install", None)
    failures: dict[str, str] = {}
    findings: dict[str, list[dict]] = {}
    redo: dict[str, list[str]] = {}
    decision = None
    for repo in batch:
        if install is not None:  # before holding a slot: the coder needn't wait on npm (D129)
            install(key, repo)
        previous = state["repos"][repo].get("verify_result")
        if previous is not None and not previous.ok:
            failures[repo] = failures_md(previous)
        items, decided = fix_inputs(state, repo)
        if items is not None:
            findings[repo], decision = items, decided
        again = redo_changes(state, repo)
        if again is not None and items is None:
            redo[repo] = again
    with agent_slot(state, deps, "coder_ticket") as slot:
        results, run = code_ticket(
            deps, key, batch, fix=bool(findings), slot=slot.name,
            provider_override=state.get("provider_override"), failures=failures,
            findings=findings, guidance=state.get("guidance"), human_decision=decision, redo=redo,
        )  # fmt: skip
    agent = deps.workspaces.ticket_dir(key) / AGENT_DIR
    plans = ticket_commit_plan(agent, batch) if results is not None else {}
    out: dict[str, tuple[Any, Any, Any]] = {}
    for repo in batch:
        result = (results or {}).get(repo)
        if result is not None and result.status == "success":
            result = _commit_repo(deps, key, repo, result, repo in findings, plans.get(repo, []))
        out[repo] = (result, run, slot)
    return out


def code_next_repo(state: TicketState, deps: Any) -> dict:
    """Code every pending repo that isn't waiting for its checks, in parallel: each coder sees
    only its own repo and the contract (§3.6), so the gateway needn't wait for the service
    (D129). `verify` then checks them one by one; a repo whose checks fail comes back here."""
    key = state["key"]
    pending = pending_repos(state)
    if not pending:
        return {"last_event": {"type": "all_coded"}}
    batch = [r for r in pending if not state["repos"][r].get("awaiting_verify")]
    if not batch:  # coded earlier in this batch: verify it next
        return {"last_event": {"type": "coded", "repo": pending[0]}}

    first = batch[0]
    retry = state["repos"][first].get("verify_result") is not None
    fixing = fix_inputs(state, first)[0] is not None
    note = ("Fixing what the automated checks found" if retry
            else "Fixing the review findings" if fixing else "In progress")  # fmt: skip
    if len(batch) == 1:
        position = ordered(state).index(first) + 1
        text = (f"Fixing {first} after the automated checks failed." if retry
                else f"Fixing the review findings in {first}." if fixing
                else f"Coding {first} ({position} of {len(state['repos'])}).")  # fmt: skip
    elif deps.config.agents.single_session:
        text = (f"Fixing the review findings in {join_words(batch)}." if fixing
                else f"Coding {join_words(batch)}.")  # fmt: skip
    else:
        text = (f"Fixing the review findings in {join_words(batch)} at the same time."
                if fixing else f"Coding {join_words(batch)} at the same time.")  # fmt: skip
    deps.notifier.repo_progress(key, state["repos"], first if len(batch) == 1 else None, note,
                                text=text, board=board_id(state))  # fmt: skip

    if deps.config.agents.single_session:
        outcomes = _code_batch(state, deps, batch)
    elif len(batch) == 1:
        outcomes = {first: _code_one(state, deps, first)}
    else:
        with ThreadPoolExecutor(max_workers=len(batch), thread_name_prefix=f"{key}-coder") as ex:
            futures = {r: ex.submit(_code_one, state, deps, r) for r in batch}
            outcomes = {r: f.result() for r, f in futures.items()}

    update = after_run(state, deps, outcomes[first][2])
    repos = dict(state["repos"])
    problem: dict | None = None
    for repo in batch:  # coding order: the first problem decides what happens next
        result, run, _slot = outcomes[repo]
        if result is None:
            problem = problem or halt("code_next_repo", f"The coding step for {repo} failed: "
                                      f"{error_words(run.error)}")  # fmt: skip
            continue
        work = {**repos[repo], "coder_result": result}
        if result.status == "success":
            work["awaiting_verify"] = True
        repos[repo] = work
        if problem is None and result.status != "success":
            problem = _problem(state, deps, repo, result)
    update["repos"] = repos
    if problem is not None:
        return {**update, **problem}
    return {**update, "last_event": {"type": "coded", "repo": batch[0]}}


def _problem(state: TicketState, deps: Any, repo: str, result: Any) -> dict:
    """What a coder that didn't succeed means: replan, questions or a failure."""
    key = state["key"]
    if result.status == "needs_clarification":
        if result.contract_deviations:
            limit = deps.config.limits.max_replans
            if state.get("replans", 0) >= limit:
                reason = (f"The code for {repo} still doesn't fit the agreed interface between "
                          f"the services after updating the plan {plural(limit, 'time')}:\n\n"
                          + bullets(result.contract_deviations))  # fmt: skip
                return halt("code_next_repo", reason)
            deps.notifier.progress(
                key,
                f"While coding {repo}, it turned out the agreed interface between the "
                "services needs a change, so I'm updating the plan:\n\n"
                + bullets(result.contract_deviations),
                "coder",
            )
            return {"last_event": {"type": "deviation", "repo": repo}}
        questions = bullets(result.open_questions) or result.summary
        return halt("code_next_repo", f"I have questions about {repo} before I "
                    f"can finish it:\n\n{questions}")  # fmt: skip
    return halt("code_next_repo", f"I couldn't finish the code for {repo}: {result.summary}")


def output_tails_md(result: Any, lines: int = 6) -> str:
    """The last lines of each failed check, so people see why (e.g. `ECONNREFUSED …:6379`)."""
    out = ""
    for name, status in result.checks.items():
        tail = [ln for ln in (result.output_tail.get(name) or "").splitlines() if ln.strip()]
        if status == "failed" and tail:
            out += f"\n\nLast lines of `{name}`:\n```\n" + "\n".join(tail[-lines:]) + "\n```"
    return out


def verify_md(repo: str, result: Any) -> str:
    """ "demo-ws2: lint passed · test failed" (developer details)."""
    checks = " · ".join(f"`{k}` {v}" for k, v in result.checks.items())
    return f"{repo}: {checks}"


def verify(state: TicketState, deps: Any) -> dict:
    key = state["key"]
    pending = pending_repos(state)
    if not pending:
        return {"last_event": {"type": "all_coded"}}
    # a repo just coded, or one whose checks failed and a person said `retry` / `guide`
    # (verify runs again without re-coding); coding order first
    failed = [r for r in pending if (v := state["repos"][r].get("verify_result")) is not None
              and not v.ok]  # fmt: skip
    ready = [r for r in pending if state["repos"][r].get("awaiting_verify") or r in failed]
    repo = ready[0] if ready else pending[0]
    work = dict(state["repos"][repo])
    result = deps.verifier.verify(key, repo, state.get("plan"))
    attempts = work.get("verify_attempts", 0) + 1
    work.update(verify_result=result, verify_attempts=attempts, awaiting_verify=False)
    warnings = [p.removeprefix(WARNING) for p in result.problems if p.startswith(WARNING)]
    if result.ok:
        work["status"] = "coded"
        passed = [k for k, v in result.checks.items() if v == "passed"]
        text = (f"{repo} is done and passed its automated checks"
                + (f" ({', '.join(passed)})." if passed else "."))  # fmt: skip
        if warnings:
            text += "\n\nWarnings:\n\n" + bullets(warnings)
        deps.notifier.repo_progress(key, {**state["repos"], repo: work}, text=text,
                                    board=board_id(state))  # fmt: skip
        return {"repos": {**state["repos"], repo: work}, "last_event": {"type": "verified",
                                                                       "repo": repo}}  # fmt: skip
    problems = bullets([p for p in result.problems if not p.startswith(WARNING)])
    problems += output_tails_md(result)
    limit = deps.config.limits.max_verify_attempts
    repos = {**state["repos"], repo: work}
    cause = machine_problem("\n".join(result.output_tail.get(name) or "" for name, status
                                      in result.checks.items() if status == "failed"))  # fmt: skip
    if cause:  # the machine, not the code (K29): no coder attempt spent on it
        work["verify_attempts"] = attempts - 1
        reason = f"{LEAD} {cause}\n\nThe automated checks for {repo} failed:\n\n{problems}"
        return {"repos": repos, **halt("verify", reason, repo=repo)}
    if attempts < limit:
        deps.notifier.repo_progress(key, repos, repo, "The automated checks failed; fixing it",
                                    board=board_id(state))  # fmt: skip
        deps.notifier.verify_failed(
            key, repo, f"{verify_md(repo, result)}\n\n{problems}", attempts, limit,
        )  # fmt: skip
        return {"repos": repos, "last_event": {"type": "verify_failed", "repo": repo}}
    reason = (f"The automated checks for {repo} still failed after "
              f"{plural(attempts, 'try', 'tries')}:\n\n{problems}")  # fmt: skip
    return {"repos": repos, **halt("verify", reason)}
