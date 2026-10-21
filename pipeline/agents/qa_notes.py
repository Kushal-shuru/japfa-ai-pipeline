"""QA notes (Phase 7 §2): copy-paste test cases for the gateway's public endpoints, written when
a ticket first reaches RFT.

The agent (deploy pool, read-only) runs in the gateway worktree (the first backend repo when the
ticket has no gateway) with the other repos readable. Its inputs in `.agent/`: `ticket.md`,
`contract.json` (from the planner), `routes.json` (the routes the gateway's MR changed, with full
paths and validators, `analysis/express_routes.py`) and `qa_context.json` (placeholders, notes
from the route cross-check). Result: `QaNotesResult.comment_markdown`.

Placeholders only (§9.6): the notes may not contain real hosts, IP addresses, `localhost`, tokens
or other secret values. A violation gets one fix-up run with the problems listed; still wrong →
the run fails (`unsafe_notes`) and nothing is posted.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pipeline.agents.reviewer import inline_md
from pipeline.schemas import AgentRun, QaNotesResult
from pipeline.verify import scan_secrets
from pipeline.workspace import AGENT_DIR

if TYPE_CHECKING:
    from pipeline.deps import Deps

MAX_ROUTE_FILES = 6  # route / controller files put in the prompt
TEST_HEADING = re.compile(r"^#{2,5}\s*Test\s+\d+\b", re.M | re.I)
_CODE = re.compile(r"```.*?```", re.S)
_IP = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_LOCAL = re.compile(r"\blocalhost\b|\b0\.0\.0\.0\b", re.I)
_URL = re.compile(r"\bhttps?://[^\s'\"`)]+", re.I)
# a real token after "Bearer": 12+ token characters (JWTs, opaque tokens). Plain words ("a bearer
# token for a logged-in user") are prose, not a leak (P9, DEMO-9: that phrase stopped the QA notes)
_BEARER = re.compile(r"\bBearer\s+(?!\{\{)[A-Za-z0-9\-._~+/]{12,}=*", re.I)
_SECRET_HEADER = re.compile(
    r"""(?i)\b([\w-]*(?:secret|token|api[_-]?key|password|cookie|session)[\w-]*)\s*:\s*"""
    r"""(?!\{\{)([^\s'"`]+)"""
)


def count_tests(md: str) -> int:
    return len(TEST_HEADING.findall(md))


def placeholder_problems(md: str) -> list[str]:
    """Real hosts, IPs, tokens or secret values in the notes (they must be placeholders)."""
    problems: list[str] = []
    for m in _IP.finditer(md):
        problems.append(f"IP address `{m.group(0)}`: use `{{{{QA_BASE_URL}}}}` instead")
    for m in _LOCAL.finditer(md):
        problems.append(f"`{m.group(0)}`: use `{{{{QA_BASE_URL}}}}` instead")
    for block in _CODE.findall(md):
        for m in _URL.finditer(block):
            problems.append(f"real URL `{m.group(0)}` in a command: use `{{{{QA_BASE_URL}}}}` "
                            "(or `{{<SERVICE>_BASE_URL}}` for developers)")  # fmt: skip
    for m in _BEARER.finditer(md):
        problems.append(f"token `{m.group(0)[:24]}…`: use `Bearer {{{{QA_TOKEN}}}}`")
    for m in _SECRET_HEADER.finditer(md):
        problems.append(f"value of `{m.group(1)}` must be a `{{{{…}}}}` placeholder")
    diff = "\n".join(f"+{line}" for line in md.splitlines())
    problems += [f"secret-looking value: {p}" for p in scan_secrets(diff)]
    return list(dict.fromkeys(problems))


def notes_repo(deps: Deps, repos: list[str]) -> str:
    """Where the notes are written from: the gateway, else the first backend repo."""
    for r in repos:
        if deps.config.repo(r).role == "gateway":
            return r
    return next((r for r in repos if deps.config.repo(r).kind == "backend"), repos[0])


def write_notes(
    deps: Deps,
    key: str,
    repos: list[str],
    routes: list[dict],
    route_notes: list[str],
    *,
    slot: str | None = None,
    provider_override: str | None = None,
) -> tuple[QaNotesResult | None, AgentRun]:
    ws = deps.workspaces
    assert ws is not None
    repo = notes_repo(deps, repos)
    cwd = ws.worktree_path(key, repo)
    agent = cwd / AGENT_DIR
    agent.mkdir(parents=True, exist_ok=True)
    ticket_agent = ws.ticket_dir(key) / AGENT_DIR
    for name in ("ticket.md", "contract.json"):
        if (ticket_agent / name).exists():
            shutil.copyfile(ticket_agent / name, agent / name)
    (agent / "routes.json").write_text(json.dumps(routes, indent=2) + "\n")
    qa = deps.config.qa
    others = {r: str(ws.worktree_path(key, r)) for r in repos if r != repo}
    (agent / "qa_context.json").write_text(json.dumps({
        "ticket": key, "notes_repo": repo, "other_repos": others,
        "placeholders": {"base_url": qa.base_url_placeholder, "token": qa.token_placeholder},
        "route_check_notes": route_notes,
    }, indent=2) + "\n")  # fmt: skip
    extra = tuple(Path(p) for p in others.values())
    prompt = (f"## This run\n\n- Ticket: **{key}**\n- Notes written from: **{repo}** "
              f"({deps.config.repo(repo).role})\n- Other repositories (read-only): "
              f"{', '.join(others) or 'none'}")  # fmt: skip
    variables = {"KEY": key, "REPO": repo, "ROLE": deps.config.repo(repo).role}
    # P9 (cost): the inputs and the changed route files are in the prompt (8 of 12 turns were
    # reads before)
    files = list(dict.fromkeys(str(r.get("file")) for r in routes if r.get("file")))
    prompt += inline_md(
        [(f"`.agent/{n}`", agent / n)
         for n in ("ticket.md", "contract.json", "routes.json", "qa_context.json")]
        + [(f"`{f}` ({repo})", cwd / f) for f in files[:MAX_ROUTE_FILES]]
    )  # fmt: skip
    result, run = deps.runner.run(key, "qa_notes", cwd, prompt, QaNotesResult, repo=repo,
                                  extra_dirs=extra, provider_override=provider_override,
                                  slot=slot, prompt_vars=variables)  # fmt: skip
    if result is None:
        return None, run
    problems = placeholder_problems(result.comment_markdown)
    if not problems:
        return result, run
    retry = (f"{prompt}\n\n## Fix your notes\n\nThe orchestrator rejected them: they must use "
             "placeholders only.\n\n" + "\n".join(f"- {p}" for p in problems)
             + "\n\nFix only these and write the whole result again. Your notes:\n\n"
             + result.comment_markdown)  # fmt: skip
    result, run = deps.runner.run(key, "qa_notes", cwd, retry, QaNotesResult, repo=repo,
                                  extra_dirs=extra, provider_override=provider_override or
                                  run.provider, slot=slot, prompt_vars=variables)  # fmt: skip
    if result is None:
        return None, run
    problems = placeholder_problems(result.comment_markdown)
    if problems:
        error = "unsafe_notes: " + "; ".join(problems)
        return None, run.model_copy(update={"ok": False, "error": error[:1000]})
    return result, run


def notes_inputs(deps: Any, state: Any) -> tuple[list[dict], list[str]]:
    """Routes the gateway's MR changed (full paths) + the cross-check notes (1.2)."""
    from pipeline.analysis.express_routes import (
        changed_files_from_diff,
        cross_check,
        extract_routes,
    )
    from pipeline.graph.nodes.review import mr_of

    key = state["key"]
    repos = list(state.get("repos") or {})
    repo = notes_repo(deps, repos)
    work = state["repos"][repo]
    if deps.config.repo(repo).role != "gateway" or not work.get("mr_iid"):
        return [], []
    changed = changed_files_from_diff(deps.gitlab.mr_diff(*mr_of(deps, state, repo)))
    routes = extract_routes(deps.workspaces.worktree_path(key, repo), changed)
    coder = work.get("coder_result")
    notes = cross_check(routes, state.get("plan"), list(coder.api_changes) if coder else [])
    return [r.dump() for r in routes], notes
