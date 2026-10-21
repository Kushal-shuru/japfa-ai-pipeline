"""Rebase before every push (D125): the first MRs (`open_mrs`), the review fixes (`push_fixes`)
and the QA fixes (the same two nodes).

For each repo about to be pushed:

1. fetch `origin/<default>` and the MR branch; commits people pushed to the MR branch are taken in
   first (rebase onto `origin/<branch>`), then the branch is rebased onto the latest default
   branch;
2. conflicts → the coder (`coder_rebase`) resolves and stages them, the orchestrator continues the
   rebase (per conflicting commit, at most `MAX_CONFLICT_STEPS`); unresolvable → abort the rebase
   and Needs Human;
3. if the branch moved (or the last checks failed) the orchestrator's checks run again (§9.5);
   failing → the repo goes back to the coder through the normal coding loop, with one attempt
   left;
4. the push that follows uses `--force-with-lease` pinned to the remote sha seen in 1, so a
   commit pushed in the meantime by someone else is never overwritten.

Nothing is pushed until every repo of the call is synced and verified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pipeline.agents.coder import resolve_conflicts
from pipeline.chat.summaries import error_words, join_words
from pipeline.graph.nodes.common import agent_slot, bullets
from pipeline.graph.state import RepoWork, TicketState
from pipeline.graph.wrapper import halt
from pipeline.verify import WARNING

MAX_CONFLICT_STEPS = 10
MARKERS = ("<<<<<<< ", ">>>>>>> ")


@dataclass
class Synced:
    lease: str = ""
    moved: bool = False
    resolved: list[str] = field(default_factory=list)
    problem: str | None = None


def _markers_left(path: Any, files: list[str]) -> list[str]:
    left = []
    for rel in files:
        f = path / rel
        if f.is_file() and any(line.startswith(MARKERS) for line in
                               f.read_text(errors="replace").splitlines()):  # fmt: skip
            left.append(rel)
    return left


def _resolve(state: TicketState, deps: Any, repo: str, onto: str,
             files: list[str]) -> str | None:  # fmt: skip
    """One conflicting commit: the coder resolves and stages. Returns a problem or None."""
    key = state["key"]
    ws = deps.workspaces
    path = ws.worktree_path(key, repo)
    with agent_slot(state, deps, "coder_rebase") as slot:
        deps.notifier.progress(
            key, f"Bringing {repo} up to date with the latest code before pushing: "
            f"{join_words([f'`{f}`' for f in files])} conflicted, so I'm resolving "
            f"{'it' if len(files) == 1 else 'them'}.", "coder",
        )  # fmt: skip
        result, run = resolve_conflicts(
            deps, key, repo, onto, files, slot=slot.name,
            provider_override=state.get("provider_override"),
        )  # fmt: skip
    if result is None:
        return f"Resolving the conflicts in {repo} failed: {error_words(run.error)}"
    if result.status != "success":
        why = bullets(result.open_questions) or result.summary
        return f"I couldn't resolve the conflicts in {repo} on my own:\n\n{why}"
    unstaged = ws.conflicted_files(path)
    left = _markers_left(path, files)
    if unstaged or left:
        return (f"The conflicts in {repo} are still not resolved "
                f"({join_words([f'`{f}`' for f in sorted(set(unstaged + left))])}).")  # fmt: skip
    return None


def sync_repo(state: TicketState, deps: Any, repo: str) -> Synced:
    key = state["key"]
    ws = deps.workspaces
    work = state["repos"][repo]
    default = deps.config.repo(repo).default_branch
    branch = work["branch"]
    path = ws.worktree_path(key, repo)
    out = Synced(lease=ws.fetch_for_sync(path, branch, default))
    before = ws.head(path)
    targets = ([f"origin/{branch}"] if out.lease else []) + [f"origin/{default}"]
    for onto in targets:
        if not ws.needs_rebase(path, onto):
            continue
        conflicts = ws.rebase_onto(path, onto)
        steps = 0
        while conflicts:
            steps += 1
            problem = (f"Rebasing {repo} onto the latest code kept conflicting "
                       f"({steps - 1} commits resolved).") if steps > MAX_CONFLICT_STEPS \
                else _resolve(state, deps, repo, onto, conflicts)  # fmt: skip
            if problem:
                ws.abort_rebase(path)
                out.problem = problem + (
                    "\n\nA developer can rebase the branch by hand and push it, then tell me to "
                    "try again. Or tell me how the conflicting changes should be combined."
                )
                return out
            out.resolved += conflicts
            conflicts = ws.continue_rebase(path)
    out.moved = ws.head(path) != before
    return out


def sync_before_push(state: TicketState, deps: Any, node: str,
                     repos: list[str]) -> tuple[dict | None, dict[str, str], dict]:  # fmt: skip
    """Sync + (re)verify every repo in `repos`. Returns (update to return instead of pushing, or
    None; leases per repo; the repos dict with fresh verify results)."""
    key = state["key"]
    new_repos: dict[str, RepoWork] = {r: dict(w) for r, w in state["repos"].items()}  # type: ignore[misc]
    leases: dict[str, str] = {}
    back: list[str] = []
    for repo in repos:
        synced = sync_repo(state, deps, repo)
        if synced.problem:
            return {"repos": new_repos, **halt(node, synced.problem)}, leases, new_repos
        leases[repo] = synced.lease
        work = new_repos[repo]
        last = work.get("verify_result")
        if not (synced.moved or synced.resolved or last is None or not last.ok):
            continue
        default = deps.config.repo(repo).default_branch
        if synced.moved:
            what = (f"resolved the conflicts in {join_words([f'`{f}`' for f in synced.resolved])}"
                    if synced.resolved else "no conflicts")  # fmt: skip
            deps.notifier.progress(key, f"Rebased {repo} onto the latest {default} ({what}); "
                                   "running the automated checks again.", "coder")  # fmt: skip
        result = deps.verifier.verify(key, repo, state.get("plan"))
        work["verify_result"] = result
        if result.ok:
            continue
        problems = bullets([p for p in result.problems if not p.startswith(WARNING)])
        limit = deps.config.limits.max_verify_attempts
        work["status"] = "pending"
        work["verify_attempts"] = max(limit - 1, 0)
        deps.notifier.verify_failed(key, repo, f"After updating {repo} to the latest {default} "
                                    f"the automated checks fail:\n\n{problems}", 1, 2)  # fmt: skip
        back.append(repo)
    if back:
        return ({"repos": new_repos, "last_event": {"type": "resync_failed", "repos": back}},
                leases, new_repos)  # fmt: skip
    return None, leases, new_repos
