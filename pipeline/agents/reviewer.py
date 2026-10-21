"""Reviewer Agent (Phase 5): one read-only run per repo (cwd = its worktree) with that repo's
`REVIEW.md`, plus one cross-repo contract check (cwd = the ticket dir) for multi-repo tickets.

Inputs land in `.agent/` (diff from GitLab, task, contract, verify result, ticket + spec; from
round 2 the previous findings in scope, the coder's resolutions and the human decision). A run
that fails is retried once (2.7); the caller turns a second failure into Needs Human.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pipeline.agents.coder import copy_spec_inputs
from pipeline.analysis.express_routes import changed_files_from_diff, extract_routes
from pipeline.schemas import AgentRun, ReviewResult, ReviewResults
from pipeline.workspace import AGENT_DIR

if TYPE_CHECKING:
    from pipeline.deps import Deps

ROUND_FILES = ("previous_review.json", "coder_resolutions.json", "human_decision.json")
LIVE_FILE = "acceptance_summary.md"  # P10: the live acceptance results, short
SINCE_FILE = "round_diff.patch"  # what the fixes changed since the last review (round 2+)
REPO_INPUTS = ("diff.patch", "verify.json", LIVE_FILE, SINCE_FILE, *ROUND_FILES)
CONTRACT_INPUTS = (SINCE_FILE, *ROUND_FILES)


def _dump(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n"


def _write_round_files(agent: Path, round_: int, previous: list[dict], resolutions: list[dict],
                       decision: dict | None) -> None:  # fmt: skip
    """Only after people chose what to fix (usually round 2+; round 1 too when a person's
    change request was fixed before the first review)."""
    if decision is None:
        return
    (agent / "previous_review.json").write_text(_dump({"findings": previous}))
    (agent / "coder_resolutions.json").write_text(_dump({"resolutions": resolutions}))
    (agent / "human_decision.json").write_text(_dump(decision or {}))


def review_check(round_: int, since: str = "") -> Any:
    """The runner's checks on a review result; problems go back to the same session once.
    (1) A `changes_requested` with nothing to fix loops: the pipeline would turn its summary into
    a finding of its own, round after round (DEMO-11). (2) From round 2 a new non-blocker finding
    must be about code the fixes changed (`round_diff.patch`): the reviewer reads the whole MR in
    round 1, and findings it only now noticed in untouched code kept the ticket in review for four
    rounds. No exception for severity (user: round 1 reports everything; later rounds only
    check the earlier findings and what their fixes changed)."""
    changed = set(changed_files_from_diff(since)) if since else set()

    def check(result: ReviewResult) -> list[str]:
        problems: list[str] = []
        unmet = any(c.status == "not_met" for c in result.acceptance_criteria_check)
        open_ = any(p.outcome == "still_open" for p in result.previous)
        if result.verdict == "changes_requested" and not (result.findings or unmet or open_):
            problems.append("the verdict is changes_requested but there is no finding, no unmet "
                            "criterion and no still-open finding: list each problem as a finding "
                            "(file and line), or change the verdict to approve")  # fmt: skip
        if round_ > 1 and changed:
            for f in result.findings:
                new = f.id.startswith(f"R{round_}-")
                if new and f.file and f.file not in changed:
                    problems.append(
                        f"{f.id} is about {f.file}, which the fixes since the last review did not "
                        "change: from round 2 report only problems in the code the fixes changed "
                        "(`.agent/round_diff.patch`). Remove it from `findings`"
                    )
        return problems

    return check


def inline_md(parts: list[tuple[str, Path]]) -> str:
    """Inputs in the prompt so the reviewer starts reviewing instead of reading (D129)."""
    out = ["", "", "These inputs are already below: **don't read them again**."]
    for title, path in parts:
        if path.is_file():
            out += ["", f"## Input: {title}", "", path.read_text(errors="replace").strip()]
    return "\n".join(out)


def routes_md(worktree: Path, diff: str) -> str:
    """Routes the diff's files register, with full mount paths (the Phase 7 scanner)."""
    try:
        routes = extract_routes(worktree, changed_files_from_diff(diff))
    except Exception:  # a scanner problem must never stop a review
        return ""
    if not routes:
        return ""
    lines = [f"- {r.method} {r.path} ({r.file}:{r.line})"
             + (f" — {r.validator}" if r.validator else "") for r in routes]  # fmt: skip
    return (
        "\n\n## Input: routes the changed files register (full paths, from the pipeline)\n\n"
        + "\n".join(lines)
    )


def live_summary_md(result: Any, proven: dict[str, str] | None = None,
                    texts: dict[str, str] | None = None) -> str:  # fmt: skip
    """The repo's live acceptance results for the reviewer: first the criteria they already
    proved on this code (the reviewer doesn't verify those again; the pipeline counts them as
    met), then one line per case (no bodies)."""
    lines = [f"# Live acceptance tests ({result.mode}): the apps ran with this code against the "
             "dev database", ""]  # fmt: skip
    if proven:
        lines += ["## Already proven by real calls: don't verify these again", ""]
        for cid, evidence in proven.items():
            first = ((texts or {}).get(cid) or "").strip().splitlines()
            lines.append(f"- **{cid}**" + (f" ({first[0][:80]})" if first else "")
                         + f": {evidence}")  # fmt: skip
        lines += ["", "## Every case", ""]
    for r in result.results:
        who = r.case_id + (f" ({r.criterion})" if r.criterion else " (contract)")
        why = f" — {r.reason}" if r.reason and r.status != "passed" else ""
        lines.append(f"- **{r.status}** {who}: {r.title}{why}")
    if result.pending_migrations:
        lines += ["", "Waiting for migrations on the dev database: "
                  + ", ".join(result.pending_migrations)]  # fmt: skip
    return "\n".join(lines) + "\n"


def round_prompt(key: str, where: str, round_: int, scope: str, earlier: bool = False) -> str:
    lines = ["## This run", "", f"- Ticket: **{key}**", f"- Reviewing: **{where}**",
             f"- Round: **{round_}** (new finding ids: `R{round_}-{scope}-F<k>`)"]  # fmt: skip
    if earlier:
        lines.append(
            "- Findings from earlier: read `.agent/previous_review.json`, "
            "`.agent/coder_resolutions.json` and `.agent/human_decision.json` first."
        )
    return "\n".join(lines)


def review_repo(
    deps: Deps, key: str, repo: str, round_: int, diff: str, *, verify: Any = None,
    previous: list[dict] | None = None, resolutions: list[dict] | None = None,
    decision: dict | None = None, slot: str | None = None, provider_override: str | None = None,
    acceptance: Any = None, proven: dict[str, str] | None = None,
    criteria_texts: dict[str, str] | None = None, since: str = "",
) -> tuple[ReviewResult | None, AgentRun]:  # fmt: skip
    ws = deps.workspaces
    assert ws is not None
    cfg = deps.config.repo(repo)
    worktree = ws.worktree_path(key, repo)
    agent = worktree / AGENT_DIR
    agent.mkdir(parents=True, exist_ok=True)
    ticket_agent = ws.ticket_dir(key) / AGENT_DIR
    copy_spec_inputs(ticket_agent, agent, repo)
    for name in REPO_INPUTS:  # stale inputs from an earlier round must not leak in
        (agent / name).unlink(missing_ok=True)
    (agent / "diff.patch").write_text(diff)
    if since:
        (agent / SINCE_FILE).write_text(since)
    if verify is not None:
        data = verify.model_dump(mode="json") if hasattr(verify, "model_dump") else verify
        (agent / "verify.json").write_text(_dump(data))
    if acceptance is not None and acceptance.results:
        (agent / LIVE_FILE).write_text(live_summary_md(acceptance, proven, criteria_texts))
    _write_round_files(agent, round_, previous or [], resolutions or [], decision)
    variables = {"KEY": key, "REPO": repo, "ROLE": cfg.role, "ROUND": str(round_),
                 "TICKET_DIR": str(ws.ticket_dir(key))}  # fmt: skip
    prompt = round_prompt(key, f"{repo} ({cfg.role})", round_, repo, decision is not None)
    prompt += inline_md([("The MR diff (`.agent/diff.patch`)", agent / "diff.patch"),
                         ("What the fixes changed since the last review "
                          "(`.agent/round_diff.patch`)", agent / SINCE_FILE),
                         ("This repo's task (`.agent/task.md`)", agent / "task.md"),
                         ("The contract (`.agent/contract.json`)", agent / "contract.json"),
                         ("The checks (`.agent/verify.json`)", agent / "verify.json"),
                         ("The live acceptance tests (`.agent/acceptance_summary.md`)",
                          agent / LIVE_FILE),
                         ("The review checklist (`REVIEW.md`)", worktree / "REVIEW.md"),
                         *[(f"`.agent/{n}`", agent / n) for n in ROUND_FILES]])  # fmt: skip
    prompt += routes_md(worktree, diff)
    return _run_twice(deps, key, "reviewer", worktree, prompt, repo, variables, slot,
                      provider_override, review_check(round_, since))  # fmt: skip


def contract_check(
    deps: Deps, key: str, round_: int, diffs: dict[str, str], *,
    previous: list[dict] | None = None, resolutions: list[dict] | None = None,
    decision: dict | None = None, slot: str | None = None, provider_override: str | None = None,
    since: dict[str, str] | None = None,
) -> tuple[ReviewResult | None, AgentRun]:  # fmt: skip
    ws = deps.workspaces
    assert ws is not None
    ticket = ws.ticket_dir(key)
    agent = ticket / AGENT_DIR
    agent.mkdir(parents=True, exist_ok=True)
    for name in CONTRACT_INPUTS:
        (agent / name).unlink(missing_ok=True)
    diff_dir = agent / "diffs"
    shutil.rmtree(diff_dir, ignore_errors=True)
    diff_dir.mkdir()
    for repo, diff in diffs.items():
        (diff_dir / f"{repo}.patch").write_text(diff)
    repos = [{"name": r, "role": deps.config.repo(r).role, "path": f"./{r}",
              "diff": f".agent/diffs/{r}.patch"} for r in diffs]  # fmt: skip
    (agent / "review_repos.json").write_text(_dump(repos))
    since_all = "\n".join(d for d in (since or {}).values() if d)
    if since_all:
        (agent / SINCE_FILE).write_text(since_all)
    _write_round_files(agent, round_, previous or [], resolutions or [], decision)
    variables = {"KEY": key, "ROUND": str(round_), "TICKET_DIR": str(ticket)}
    prompt = round_prompt(key, "the interface contract across " + ", ".join(diffs), round_, "X",
                          decision is not None)  # fmt: skip
    prompt += inline_md([("The contract (`.agent/contract.json`)", agent / "contract.json"),
                         ("The ticket (`.agent/ticket.md`)", agent / "ticket.md"),
                         ("Spec excerpt (`.agent/spec_excerpt.md`)", agent / "spec_excerpt.md"),
                         ("What the fixes changed since the last review "
                          "(`.agent/round_diff.patch`)", agent / SINCE_FILE),
                         *[(f"Diff of {r} (`.agent/diffs/{r}.patch`)", diff_dir / f"{r}.patch")
                           for r in diffs],
                         *[(f"`.agent/{n}`", agent / n) for n in ROUND_FILES]])  # fmt: skip
    return _run_twice(deps, key, "contract_check", ticket, prompt, None, variables, slot,
                      provider_override, review_check(round_, since_all))  # fmt: skip


def _run_twice(deps: Deps, key: str, role: str, cwd: Path, prompt: str, repo: str | None,
               variables: dict[str, str], slot: str | None,
               provider_override: str | None,
               check: Any = None) -> tuple[ReviewResult | None, AgentRun]:  # fmt: skip
    """One retry when a review run fails (2.7)."""
    result, run = None, None
    for _ in range(2):
        result, run = deps.runner.run(
            key, role, cwd, prompt, ReviewResult, repo=repo, provider_override=provider_override,
            slot=slot, prompt_vars=variables, check=check, check_error="invalid_review",
        )  # fmt: skip
        if result is not None or (run.error or "").startswith("paused"):
            break
    assert run is not None
    return result, run


# ---------- one session for the whole change set (agents.single_session) ----------


def review_ticket(
    deps: Deps, key: str, round_: int, diffs: dict[str, str], reviewing: list[str], *,
    contract: bool, verify: dict[str, Any], previous: list[dict], resolutions: list[dict],
    decision: dict | None, since: dict[str, str], acceptance: dict[str, Any],
    proven: dict[str, dict[str, str]], criteria_texts: dict[str, dict[str, str]],
    slot: str | None = None, provider_override: str | None = None,
) -> tuple[dict[str, ReviewResult] | None, AgentRun]:  # fmt: skip
    """One reviewer session for every repo in `reviewing` and, when `contract`, the cross-repo
    contract check (agents.single_session). It reads the ticket's folder; everything it needs
    is in the prompt, nothing is copied into the repos. Returns the results by scope (a repo
    name, or "contract"). One retry when the run fails, like the per-repo reviews."""
    from pipeline.agents.coder import ticket_spec_excerpt
    from pipeline.agents.planner import render_ticket_plan_md, ticket_ac_split

    ws = deps.workspaces
    assert ws is not None
    ticket_dir = ws.ticket_dir(key)
    agent = ticket_dir / AGENT_DIR
    agent.mkdir(parents=True, exist_ok=True)
    plan = _plan(ticket_dir)
    ticket = (agent / "ticket.md").read_text(errors="replace") \
        if (agent / "ticket.md").is_file() else ""  # fmt: skip
    texts, without = ticket_ac_split(ticket)
    scopes = [*reviewing, *(["contract"] if contract else [])]
    out = [f"## This run\n\n- Ticket: **{key}**, round **{round_}** "
           f"(new finding ids: `R{round_}-<repo>-F<k>`, `R{round_}-X-F<k>` for the contract)",
           "- Review these repos, each with its own result (`scope` = the repo's name): "
           + ", ".join(f"**{r}** ({deps.config.repo(r).role}, folder `./{r}`)" for r in reviewing),
           "- " + ("Then the contract check, as `scope` `contract`." if contract
                   else "No contract check this round: report only the repos above."),
           f"- `results` must hold exactly one entry per scope: {', '.join(scopes)}.",
           ]  # fmt: skip
    others = [r for r in diffs if r not in reviewing]
    if others:
        out.append("- Not reviewed this round (already merged or unchanged): "
                   + ", ".join(others) + "; their diffs are below only for the contract check.")
    if decision is not None:
        out.append("- People chose what to fix: the earlier findings, what the coder says about "
                   "each, and the decision are below.")
    out += ["", "These inputs are already below: **don't read them again**.", ""]
    if plan is not None:
        out += ["## Input: the plan", "", render_ticket_plan_md(plan, key, None, texts), "",
                "## Input: the interface contract (authoritative)", "", "```json",
                plan.contract.model_dump_json(indent=1), "```", ""]  # fmt: skip
    if ticket:
        out += ["## Input: the ticket (the criteria are in the plan)", "", without.strip(), ""]
    excerpt = ticket_spec_excerpt(ticket_dir)
    if excerpt:
        out += ["## Input: spec sections the plan relies on", "", excerpt.strip(), ""]
    for repo in diffs:
        worktree = ws.worktree_path(key, repo)
        out += [f"## Input: the merge request diff of `{repo}`", "", diffs[repo].strip(), ""]
        if repo not in reviewing:
            continue
        check = verify.get(repo)
        if check is not None:
            data = check.model_dump(mode="json") if hasattr(check, "model_dump") else check
            out += [f"## Input: the automated checks of `{repo}`", "", "```json",
                    _dump(data).strip(), "```", ""]  # fmt: skip
        live = acceptance.get(repo)
        if live is not None and live.results:
            out += [f"## Input: the live acceptance tests of `{repo}`", "",
                    live_summary_md(live, proven.get(repo), criteria_texts.get(repo)).strip(), ""]
        if since.get(repo):
            out += [f"## Input: what the fixes changed in `{repo}` since the last review "
                    "(from round 2 new findings may only be about this)", "",
                    since[repo].strip(), ""]  # fmt: skip
        checklist = worktree / "REVIEW.md"
        if checklist.is_file():
            out += [f"## Input: the review checklist of `{repo}` (`REVIEW.md`)", "",
                    checklist.read_text(errors="replace").strip(), ""]  # fmt: skip
        routes = routes_md(worktree, diffs[repo]).strip()
        if routes:
            out += [routes, ""]
    if decision is not None:
        out += ["## Input: findings from the last round that people chose to fix "
                "(`previous_review`)", "", "```json", _dump({"findings": previous}).strip(), "```",
                "", "## Input: what the coder says it did (`coder_resolutions`)", "", "```json",
                _dump({"resolutions": resolutions}).strip(), "```", "",
                "## Input: people's decision (`human_decision`)", "", "```json",
                _dump(decision or {}).strip(), "```", ""]  # fmt: skip
    prompt = "\n".join(out)
    sinces = "\n".join(d for d in since.values() if d)

    def check(result: ReviewResults) -> list[str]:
        named = [r.scope for r in result.results]
        problems = [f"no result for scope `{s}`" for s in scopes if s not in named]
        problems += [f"a result for scope `{s}`, which was not asked for"
                     for s in sorted(set(named) - set(scopes))]  # fmt: skip
        problems += [f"two results for scope `{s}`" for s in sorted({s for s in named
                                                                     if named.count(s) > 1})]
        for res in result.results:
            if res.scope in scopes:
                one = since.get(res.scope, "") if res.scope != "contract" else sinces
                problems += [f"[{res.scope}] {p}" for p in review_check(round_, one)(res)]
        return problems

    variables = {"KEY": key, "ROUND": str(round_), "TICKET_DIR": str(ticket_dir)}
    result, run = None, None
    for _ in range(2):
        result, run = deps.runner.run(
            key, "reviewer_ticket", ticket_dir, prompt, ReviewResults,
            provider_override=provider_override, slot=slot, prompt_vars=variables,
            check=check, check_error="invalid_review", continue_session=False,
        )  # fmt: skip
        if result is not None or (run.error or "").startswith("paused"):
            break
    assert run is not None
    return ({r.scope: r for r in result.results} if result is not None else None), run


def _plan(ticket_dir: Path) -> Any:
    from pipeline.schemas import ImplementationPlan

    try:
        return ImplementationPlan.model_validate_json(
            (ticket_dir / AGENT_DIR / "plan.json").read_text())
    except (OSError, ValueError):
        return None
