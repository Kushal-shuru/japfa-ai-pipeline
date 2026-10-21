"""`pipeline gc` (Phase 8 1.7) and `pipeline demo-reset` (4.1).

- `gc`: remove the workspaces (git worktrees) of tickets that are finished, stopped or no
  longer known; optionally the node_modules cache.
- `demo_reset`: put demo tickets back to the start, everywhere: close their AI merge requests
  and delete their `ai/<KEY>-…` branches (GitLab and the base clones), delete the bot's Jira
  comments (by marker) and deployment-note attachments, move Jira back to To Do, and forget
  the ticket locally (checkpoints, tracking rows including `commands` and `chat_threads`,
  worktrees). Never touches a default branch (§9.1): code that a rehearsal merged into
  `develop` stays there.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from typing import Any

from pipeline.integrations.base import IntegrationError


def ai_branch_prefix(key: str) -> str:
    return f"ai/{key}-"


def is_ai_branch(key: str, branch: str | None) -> bool:
    return bool(branch) and (branch == f"ai/{key}" or str(branch).startswith(ai_branch_prefix(key)))


# ---------- gc ----------


def gc(deps: Any, cache: bool = False, dry_run: bool = False) -> list[str]:
    """Workspaces of finished / stopped / unknown tickets (and the node_modules cache with
    `cache`). Returns what was (or, with `dry_run`, would be) removed."""
    removed: list[str] = []
    root = deps.home / "workspaces"
    for tdir in sorted(root.iterdir()) if root.is_dir() else []:
        if not tdir.is_dir():
            continue
        row = deps.store.get_ticket(tdir.name)
        done = row is None or bool(row.get("finished_at")) or row.get("stage") == "stopped"
        if not done:
            continue
        removed.append(f"workspace {tdir.name}")
        if not dry_run:
            try:
                deps.workspaces.remove_ticket_workspace(tdir.name)
            except IntegrationError:
                shutil.rmtree(tdir, ignore_errors=True)
    if cache and (deps.home / "deps_cache").is_dir():
        removed.append("node_modules cache")
        if not dry_run:
            shutil.rmtree(deps.home / "deps_cache", ignore_errors=True)
    return removed


# ---------- demo-reset ----------


@dataclass
class ResetResult:
    key: str
    closed_mrs: list[str] = field(default_factory=list)
    deleted_branches: list[str] = field(default_factory=list)
    jira_comments: int = 0
    jira_attachments: int = 0
    jira_status: str = ""
    problems: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"{self.key}: closed {len(self.closed_mrs)} merge request(s), deleted "
            f"{len(self.deleted_branches)} branch(es), removed {self.jira_comments} bot "
            f"comment(s) and {self.jira_attachments} attachment(s); Jira: "
            f"{self.jira_status or 'unchanged'}."
        ]
        out += [f"  problem: {p}" for p in self.problems]
        return out


def local_reset(deps: Any, key: str, checkpointer: Any) -> list[str]:
    """`reset KEY`: forget the ticket on this machine only (checkpoints, tracking rows with the
    `commands` log kept, worktrees); GitLab and Jira are left alone. Returns the problems.
    P10: a live test run's leftover rows on the dev DB are deleted first (the ledger lives in the
    worktree folder); migrations the ticket had applied on dev are named (undo them by hand)."""
    problems = live_test_leftovers(deps, key)
    checkpointer.delete_thread(key)
    deps.store.delete_ticket(key)
    try:
        deps.workspaces.remove_ticket_workspace(key)
    except IntegrationError as err:
        return [*problems, f"The worktrees were not fully removed: {err}"]
    return problems


def live_test_leftovers(deps: Any, key: str) -> list[str]:
    """Before a ticket's folder goes: delete its live test rows still in the ledger, and remind
    about its migrations on the dev DB (the pipeline never undoes them: undo can drop data)."""
    from pipeline.testing.migrations import NOTE
    from pipeline.testing.service import AcceptanceService

    out: list[str] = []
    try:
        report = AcceptanceService(deps).cleanup_leftovers(key)
    except Exception as err:  # the DB is away: say so, keep going
        report = {"error": str(err)}
    if report and report.get("error"):
        out.append(f"Live test rows of {key} may still be on the dev database (cleanup failed: "
                   f"{report['error']}); the ledger is in the ticket folder until it's removed.")
    applied = sorted(deps.store.get_note(key, NOTE) or {})
    if applied:
        out.append(f"{key}'s migrations were applied on the dev database: {', '.join(applied)}. "
                   "If the ticket won't be merged, undo them there (`npx sequelize-cli "
                   "db:migrate:undo`, newest first).")  # fmt: skip
    return out


def demo_reset(deps: Any, key: str, checkpointer: Any = None) -> ResetResult:
    """Everything in the module docstring for one ticket; continues past failures (listed in
    `problems`) so a half-reset ticket can be reset again."""
    res = ResetResult(key)

    def attempt(what: str, fn: Any) -> tuple[bool, Any]:
        try:
            return True, fn()
        except IntegrationError as err:
            res.problems.append(f"{what}: {err}")
            return False, None

    prefix = ai_branch_prefix(key)
    for repo in deps.config.repos:
        project = repo.gitlab_project
        _, mrs = attempt(
            f"list merge requests in {repo.name}", lambda p=project: deps.gitlab.list_mrs(p, prefix)
        )
        mrs = [m for m in mrs or [] if is_ai_branch(key, m["source_branch"])]
        for mr in mrs:
            if mr["state"] == "opened":
                ok, _ = attempt(
                    f"close {repo.name}!{mr['iid']}",
                    lambda p=project, i=mr["iid"]: deps.gitlab.close_mr(p, i),
                )
                if ok:
                    res.closed_mrs.append(f"{repo.name}!{mr['iid']}")
        branches = {m["source_branch"] for m in mrs}
        branches |= {
            row["branch"]
            for row in deps.store.mrs_for(key)  # known without an MR too
            if row["repo"] == repo.name and is_ai_branch(key, row.get("branch"))
        }
        for branch in sorted(branches):
            if branch == repo.default_branch:  # never a default branch (§9.1)
                continue
            ok, _ = attempt(
                f"delete {repo.name}:{branch}",
                lambda p=project, b=branch: deps.gitlab.delete_branch(p, b),
            )
            if ok:
                res.deleted_branches.append(f"{repo.name}:{branch}")

    _, n = attempt("delete bot comments", lambda: deps.jira.delete_bot_comments(key))
    res.jira_comments = n or 0
    _, n = attempt(
        "delete attachments", lambda: deps.jira.delete_attachments(key, f"{key}-deployment")
    )
    res.jira_attachments = n or 0
    todo = deps.config.jira.statuses.todo
    ok, _ = attempt(f"move to {todo}", lambda: deps.jira.transition(key, todo))
    res.jira_status = todo if ok else ""

    if checkpointer is not None:
        checkpointer.delete_thread(key)
    deps.store.delete_ticket(key)
    deps.store.delete_commands(key)
    attempt("remove worktrees", lambda: deps.workspaces.remove_ticket_workspace(key))
    for repo in deps.config.repos:
        attempt(
            f"delete local branches in {repo.name}",
            lambda r=repo.name: deps.workspaces.delete_local_branches(r, prefix),
        )
    return res
