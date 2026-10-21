"""Tester Agent (P10): writes the live acceptance cases for a ticket, as data.

One read-only run in the ticket workspace (cwd = the ticket dir, like the contract check), in
the **reviewer** pool, so the agent that proves the criteria isn't the one that wrote the code.
Its inputs are inline (D129): the criteria per repo in the ticket's words, the contract, each
repo's source diff (tests left out), the spec excerpt, and the test setup (identities, template
values, tables waiting for a migration, externals, repo notes). The result
(`AcceptancePlan`) is checked with `cases.plan_problems`; problems go back to the same session
once. After a run, `revise_cases` sends the *invalid* cases back (same session) with what the
pipeline saw.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pipeline.agents.reviewer import inline_md
from pipeline.schemas import AcceptancePlan, AgentRun, CaseResult
from pipeline.testing.cases import plan_problems
from pipeline.verify import _TEST_PATH, LOCK_FILES
from pipeline.workspace import AGENT_DIR

if TYPE_CHECKING:
    from pipeline.deps import Deps

PlanRun = tuple[AcceptancePlan | None, AgentRun]
DIFF_BUDGET = 40_000  # characters of source diff per repo in the prompt
RUN_VALUES = ("partner_id", "user_uuid", "user_pk", "user_id", "tag", "client_id")


def source_diff(diff: str, budget: int = DIFF_BUDGET) -> str:
    """The diff without test files and lock files, cut at `budget` characters (whole files)."""
    files: list[str] = []
    current: list[str] = []
    for line in diff.splitlines():
        if line.startswith("diff --git ") and current:
            files.append("\n".join(current))
            current = []
        current.append(line)
    if current:
        files.append("\n".join(current))
    out, used, left = [], 0, []
    for f in files:
        head = f.splitlines()[0] if f else ""
        path = head.rsplit(" b/", 1)[-1] if " b/" in head else ""
        if _TEST_PATH.search(path) or path.rsplit("/", 1)[-1] in LOCK_FILES:
            continue
        if used + len(f) > budget:
            left.append(path)
            continue
        used += len(f)
        out.append(f)
    text = "\n".join(out)
    if left:
        text += "\n\n(Not shown, read them in the repo folder if you need them: " + \
            ", ".join(f"`{p}`" for p in left) + ")"  # fmt: skip
    return text


def criteria_md(required: dict[str, list[tuple[str, str]]]) -> str:
    lines = []
    for repo, items in required.items():
        lines.append(f"### {repo}")
        lines += [f"- **{cid}**: {text}" if text else f"- **{cid}**" for cid, text in items]
        lines.append("")
    return "\n".join(lines).strip() or "(no criteria)"


def setup_md(deps: Any, repos: list[str], blocked_tables: dict[str, str],
             schema: str = "") -> str:  # fmt: skip
    acc = deps.config.acceptance
    lines = ["### Identities (`as`)", ""]
    for name, ident in acc.identities.items():
        what = ident.description or ident.kind
        if ident.kind == "seeded_session":
            perms = ident.session.get("obj_perms") or []
            what += f" (permissions: {', '.join(map(str, perms)) or 'none'})"
        lines.append(f"- `{name}` → {ident.repo}: {what}")
    defaults = ", ".join(f"{r}: `{i}`" for r, i in acc.default_identity.items() if r in repos)
    partner = ("a **real partner with existing data** (`acceptance.run_values`): scope every check "
               "and list call by your saved ids or `{{run.tag}}`, never by the partner alone, and "
               "`track` every row a POST creates (the cleanup can't sweep this partner)"
               if "partner_id" in acc.run_values else
               "new to this run: nothing else lives under it")  # fmt: skip
    lines += ["- `none`: no auth", f"- `default` per repo: {defaults or 'none'}", "",
              "### Template values", "",
              "- " + ", ".join(f"`{{{{run.{v}}}}}`" for v in RUN_VALUES),
              f"- The partner (`{{{{run.partner_id}}}}`) is {partner}", "",
              "### Tables waiting for a migration on the dev database", ""]
    lines += [f"- `{t}`: {why}" for t, why in blocked_tables.items()] or ["- none"]
    lines += ["", "### External services", ""]
    if acc.externals:
        for name, ext in acc.externals.items():
            about = f": {ext.description}" if ext.description else ""
            lines.append(f"- `{name}` ({ext.mode}){about}")
    else:
        lines.append("- none configured: a criterion that needs one goes in `blocked`")
    if acc.notes:
        lines += ["", "### Notes about the repos", ""] + [f"- {n}" for n in acc.notes]
    if schema:
        lines += ["", schema]
    return "\n".join(lines)


def _inputs(deps: Deps, key: str, repos: list[str], required: dict[str, list[tuple[str, str]]],
            blocked_tables: dict[str, str], schema: str = "") -> tuple[Path, str]:  # fmt: skip
    ws = deps.workspaces
    assert ws is not None
    ticket = ws.ticket_dir(key)
    agent = ticket / AGENT_DIR
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "acceptance_plan.json").unlink(missing_ok=True)
    (agent / "acceptance_criteria.md").write_text(criteria_md(required) + "\n")
    (agent / "acceptance_setup.md").write_text(setup_md(deps, repos, blocked_tables, schema) + "\n")
    diffs = []
    for repo in repos:
        cfg = deps.config.repo(repo)
        diff = ws.diff_vs_default(ws.worktree_path(key, repo), cfg.default_branch)
        path = agent / f"acceptance_diff_{repo}.patch"
        path.write_text(source_diff(diff))
        diffs.append((f"The source diff of {repo} (`.agent/{path.name}`)", path))
    prompt = (f"## This run\n\n- Ticket: **{key}**\n- Repos under test: "
              + ", ".join(f"**{r}** ({deps.config.repo(r).role}, folder `./{r}`)" for r in repos))
    prompt += inline_md([
        ("The criteria to prove (`.agent/acceptance_criteria.md`)",
         agent / "acceptance_criteria.md"),
        ("The test setup (`.agent/acceptance_setup.md`)", agent / "acceptance_setup.md"),
        ("The contract (`.agent/contract.json`)", agent / "contract.json"),
        *diffs,
        ("The ticket (`.agent/ticket.md`)", agent / "ticket.md"),
        ("Spec sections (`.agent/spec_excerpt.md`)", agent / "spec_excerpt.md"),
    ])  # fmt: skip
    return ticket, prompt


def write_cases(deps: Deps, key: str, repos: list[str],
                required: dict[str, list[tuple[str, str]]], blocked_tables: dict[str, str], *,
                schema: str = "", slot: str | None = None, provider_override: str | None = None,
                first_id: int = 1) -> PlanRun:  # fmt: skip
    """The tester's cases for `repos` (never raises for agent failures). `first_id`: the number
    its first case gets (`A<first_id>`), when only some criteria are written again."""
    ticket, prompt = _inputs(deps, key, repos, required, blocked_tables, schema)
    if first_id > 1:
        prompt += (f"\n\nOnly the criteria listed above need cases now (the others have theirs). "
                   f"Number your cases from A{first_id}.")  # fmt: skip
    ids = {r: [cid for cid, _ in items] for r, items in required.items()}
    return deps.runner.run(
        key, "tester", ticket, prompt, AcceptancePlan, provider_override=provider_override,
        slot=slot, prompt_vars={"KEY": key, "TICKET_DIR": str(ticket)},
        check=lambda plan: plan_problems(plan, ids, repos, deps.config),
        check_error="invalid_cases", continue_session=True,
    )  # fmt: skip


def revise_cases(deps: Deps, key: str, repos: list[str],
                 required: dict[str, list[tuple[str, str]]], plan: AcceptancePlan,
                 invalid: list[CaseResult], *, slot: str | None = None,
                 provider_override: str | None = None) -> PlanRun:  # fmt: skip
    """The cases the pipeline found invalid, back to the tester (same session) with what it saw."""
    blocked = {}  # the full prompt is only a fallback (no session to continue): setup as it was
    ticket, prompt = _inputs(deps, key, repos, required, blocked)
    items = "\n".join(f"- **{r.case_id}** ({r.criterion or 'contract'}): {r.reason}"
                      for r in invalid)  # fmt: skip
    seen = json.dumps([r.model_dump(mode="json", include={"case_id", "evidence"})
                       for r in invalid], indent=1, default=str)[:20_000]  # fmt: skip
    follow_up = (
        "## The pipeline ran your cases: these are invalid\n\n"
        "They broke a rule or don't match the database or the code (not a bug in the code under "
        f"test):\n\n{items}\n\nWhat each step sent and got back:\n\n```json\n{seen}\n```\n\n"
        "Fix only these cases (same ids) and write the whole `.agent/acceptance_plan.json` again."
    )
    ids = {r: [cid for cid, _ in its] for r, its in required.items()}
    (ticket / AGENT_DIR / "acceptance_plan.json").write_text(plan.model_dump_json(indent=1))
    return deps.runner.run(
        key, "tester", ticket, f"{prompt}\n\n{follow_up}", AcceptancePlan,
        provider_override=provider_override, slot=slot,
        prompt_vars={"KEY": key, "TICKET_DIR": str(ticket)},
        check=lambda p: plan_problems(p, ids, repos, deps.config),
        check_error="invalid_cases", continue_session=True, follow_up=follow_up,
    )  # fmt: skip
