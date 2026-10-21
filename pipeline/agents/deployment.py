"""Deployment Agent (Phase 6 §2): one read-only run per ticket (cwd = the ticket workspace)
that writes the deployment note for the whole change set.

Inputs land in the ticket's `.agent/`: `devops_facts.json` (deterministic, `analysis/
devops_facts.py`), `deploy_repos.json`, `diffs/<repo>.patch`, `override.json` (accepted open
findings); `plan.json`, `contract.json`, `ticket.md`, `spec.md` are already there from planning.
Each repo's `DEPLOYMENT_TEMPLATE.md` and migration files are read from its folder. A failed run
is retried once; the caller turns a second failure into Needs Human.
"""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING, Any

from pipeline.agents.reviewer import inline_md
from pipeline.schemas import AgentRun, DeployResult, DevopsFacts
from pipeline.workspace import AGENT_DIR

if TYPE_CHECKING:
    from pipeline.deps import Deps

TEMPLATE_FILE = "DEPLOYMENT_TEMPLATE.md"
INPUTS = ("devops_facts.json", "deploy_repos.json", "override.json", "deployment.md",
          "deploy.json")  # fmt: skip


def _dump(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n"


def write_note(
    deps: Deps, key: str, repos: list[str], facts: list[DevopsFacts], diffs: dict[str, str], *,
    override: dict | None = None, slot: str | None = None, provider_override: str | None = None,
) -> tuple[DeployResult | None, AgentRun]:  # fmt: skip
    """`repos` in the default merge/deploy order (services → gateway → frontend)."""
    ws = deps.workspaces
    assert ws is not None
    ticket = ws.ticket_dir(key)
    agent = ticket / AGENT_DIR
    agent.mkdir(parents=True, exist_ok=True)
    for name in INPUTS:  # a regenerated note must not see the last run's output
        (agent / name).unlink(missing_ok=True)
    diff_dir = agent / "diffs"
    shutil.rmtree(diff_dir, ignore_errors=True)
    diff_dir.mkdir()
    for repo, diff in diffs.items():
        (diff_dir / f"{repo}.patch").write_text(diff)
    (agent / "devops_facts.json").write_text(_dump([f.model_dump() for f in facts]))
    listing = []
    for n, repo in enumerate(repos, 1):
        folder = ticket / repo
        listing.append({
            "name": repo, "role": deps.config.repo(repo).role, "path": f"./{repo}",
            "diff": f".agent/diffs/{repo}.patch", "default_order": n,
            "template": f"./{repo}/{TEMPLATE_FILE}" if (folder / TEMPLATE_FILE).exists() else None,
        })  # fmt: skip
    (agent / "deploy_repos.json").write_text(_dump(listing))
    if override:
        (agent / "override.json").write_text(_dump(override))
    variables = {"KEY": key, "TICKET_DIR": str(ticket)}
    prompt = "\n".join([
        "## This run", "", f"- Ticket: **{key}**",
        "- Repositories (default order): " + ", ".join(repos),
        "- Override: see `.agent/override.json`" if override else "- Override: none",
    ])  # fmt: skip
    # P9 (cost): the inputs it always needs are in the prompt (it spent 10 of 13 turns reading)
    parts = [("Facts (`.agent/devops_facts.json`)", agent / "devops_facts.json"),
             ("Repositories (`.agent/deploy_repos.json`)", agent / "deploy_repos.json"),
             ("The ticket (`.agent/ticket.md`)", agent / "ticket.md")]  # fmt: skip
    if override:
        parts.append(("Override (`.agent/override.json`)", agent / "override.json"))
    for f in facts:
        parts.append((f"{f.repo}: `{TEMPLATE_FILE}`", ticket / f.repo / TEMPLATE_FILE))
        parts += [(f"{f.repo}: `{m}`", ticket / f.repo / m) for m in f.migration_files]
    prompt += inline_md(parts)
    result, run = None, None
    for _ in range(2):
        result, run = deps.runner.run(
            key, "deployment", ticket, prompt, DeployResult, repo=None,
            provider_override=provider_override, slot=slot, prompt_vars=variables,
        )  # fmt: skip
        if result is not None or (run.error or "").startswith("paused"):
            break
    assert run is not None
    return result, run
