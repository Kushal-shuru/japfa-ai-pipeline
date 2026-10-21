"""Coder Agent: one run per repo, inside that repo's worktree (CLAUDE.md §3.6, Phase 3 §5).

Inputs land in `<worktree>/.agent/` (task.md + contract.json from the planner; ticket.md +
spec.md copied from the ticket dir so the coder never needs to read or write outside its repo,
§9.8; verify_failures.md / findings.json when relevant). The coder keeps a commit plan in
`.agent/commits.json` (one small commit per module or mini feature, D169) and the orchestrator
makes those commits, verifies, pushes and opens the MR.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline.agents.roles import GUIDANCE_FILE, guidance_md
from pipeline.schemas import AgentRun, CoderResult, CoderResults, ImplementationPlan, RepoTask
from pipeline.workspace import AGENT_DIR

if TYPE_CHECKING:
    from pipeline.config import RepoConfig
    from pipeline.deps import Deps

COMMITS_FILE = "commits.json"  # the coder's commit plan (D169); an output, cleared each run
INPUT_FILES = ("verify_failures.md", "findings.json", "human_decision.json", "conflicts.json",
               "changes.md", GUIDANCE_FILE, COMMITS_FILE)  # fmt: skip
CHANGES_FILE = "changes.md"  # a redo after a plan change: only these changes (D130)


def changes_md(repo: str, changes: list[str]) -> str:
    items = "\n".join(f"- {c}" for c in changes) or (
        "- (the updated plan lists no change for this repository by name: bring the code in line "
        "with the updated task and contract, changing only what differs)"
    )
    return (f"# Changes to apply in {repo}\n\nThe task is **already implemented** on this branch "
            "(earlier commits, already in a merge request). The plan was updated after that. "
            f"Apply **only** these changes:\n\n{items}\n\nLeave everything else as it is: don't "
            "rewrite, reformat or re-check code these changes don't touch.\n")  # fmt: skip


FULL_SUITE_WORDS = ("coverage",)  # extra checks that re-run the whole test suite


def commands_md(repo: RepoConfig) -> str:
    """The checks the coder runs itself. P9 (cost): the whole test suite and the coverage gate
    are not among them: the pipeline runs every configured check after the hand-in (`verify`) and
    sends a failure back with its output, so the coder ran them twice before (DEMO-9: `npm test`
    six times in one run, each turn re-reading the whole context)."""
    c = repo.commands
    rows = [("install (only if node_modules is missing or package.json changed)", c.install)]
    if c.lint and "eslint" in c.lint:  # fix what ESLint can fix by itself, then check once
        rows.append(("lint autofix (run first, then `lint`)", c.lint.rstrip() + " --fix"))
    rows.append(("lint", c.lint))
    if c.build:
        rows.append(("build", c.build))
    quick = [cmd for cmd in c.extra if not any(w in cmd for w in FULL_SUITE_WORDS)]
    rows += [(cmd.removeprefix("npm run "), cmd) for cmd in quick]
    lines = [f"- **{name}**: `{cmd}`" for name, cmd in rows]
    full = [x for x in (c.test, *c.extra) if x and x not in quick]
    if c.test:
        lines.append(
            "- **test**: run **only the test files you added or changed** (the standards file says "
            "how to run one file), as often as you need while fixing. Don't run "
            + " or ".join(f"`{x}`" for x in full)
            + ": the pipeline runs the whole suite and every check above after you hand in, and "
            "sends any failure back to you with its output. Record `test` as `passed` when your "
            "test files pass."
        )
    else:
        lines.append("- This repo has **no test runner**: do not add one (see the standards file).")
    if repo.shared_paths:
        lines += ["", SHARED_MD.format(paths=", ".join(f"`{p}`" for p in repo.shared_paths))]
    return "\n".join(lines)


# P9 (DEMO-10: the coder changed the shared validateSchema middleware twice to satisfy one
# acceptance criterion); verify enforces it too (`shared_code` check)
SHARED_MD = (
    "**Shared code is off-limits:** never edit or delete an existing file under {paths} (every "
    "endpoint uses them); adding a new file there is fine. This is enforced: the pipeline undoes "
    "any change to them (and your edit tools refuse them), so don't try. If an acceptance "
    "criterion or a finding can only be met by changing one, don't change it and don't work "
    "around it: do the rest, and report it starting with `shared code:` (file, what it does "
    "today, what the criterion wants): in `open_questions` in a coding run, as a `disputed` "
    "resolution's note in a fix run. A person decides."
)


def prepare_inputs(
    deps: Deps,
    key: str,
    repo: str,
    verify_failures: str | None = None,
    findings: list[dict] | None = None,
    guidance: list[dict] | None = None,
    human_decision: dict | None = None,
    redo: list[str] | None = None,
) -> Path:
    ws = deps.workspaces
    assert ws is not None, "Deps.workspaces is required for coding"
    worktree = ws.worktree_path(key, repo)
    ws.ensure_env(key, repo)  # tests / commit hooks need it (D69)
    agent = worktree / AGENT_DIR
    agent.mkdir(parents=True, exist_ok=True)
    ticket_agent = ws.ticket_dir(key) / AGENT_DIR
    copy_spec_inputs(ticket_agent, agent, repo)
    for name in INPUT_FILES:  # stale inputs from an earlier run must not leak in
        (agent / name).unlink(missing_ok=True)
    if verify_failures:
        (agent / "verify_failures.md").write_text(verify_failures.rstrip() + "\n")
    if findings is not None:
        (agent / "findings.json").write_text(json.dumps(findings, indent=2) + "\n")
    if human_decision is not None:
        (agent / "human_decision.json").write_text(json.dumps(human_decision, indent=2) + "\n")
    if redo is not None:
        (agent / CHANGES_FILE).write_text(changes_md(repo, redo))
    text = guidance_md(guidance)
    if text:
        (agent / GUIDANCE_FILE).write_text(text)
    return worktree


def commit_plan(worktree: Path) -> list[tuple[str, list[str]]]:
    """`.agent/commits.json` → [(message, files)], in order (D169). Accepts a list or
    `{"commits": [...]}`; entries without a message or files are skipped; unreadable → []
    (the orchestrator then makes one commit, as before)."""
    path = worktree / AGENT_DIR / COMMITS_FILE
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("commits")
    out: list[tuple[str, list[str]]] = []
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict):
            continue
        message = str(entry.get("message") or "").strip()
        files = [str(f).strip() for f in entry.get("files") or [] if str(f).strip()]
        if message and files:
            out.append((message, files))
    return out


COMMIT_TYPE_RE = re.compile(r"^[a-z]+(\([^)]*\))?!?: \S")


def commit_message(message: str, kind: str, key: str) -> str:
    """One line, `<type>(KEY): <what>` (`feat` / `fix` added when the coder gave no type),
    whole (a long subject is better than one cut with an ellipsis)."""
    line = (message.strip().splitlines() or [""])[0].strip().rstrip(".")
    if not COMMIT_TYPE_RE.match(line):
        line = f"{kind}({key}): {line[:1].lower() + line[1:] if line else 'coder changes'}"
    return line


FILES_BUDGET = 60_000  # characters of existing code put in the prompt (D129)
FILE_LIMIT = 20_000


def repo_spec_excerpt(ticket_dir: Path, repo: str) -> str:
    """The spec sections this repo's task relies on (cost trim, D171): what the plan cites for
    it, its endpoints' sections, the tables it names, its error sections. '' = use the ticket's
    excerpt (no plan yet, or nothing matched)."""
    from pipeline.agents.planner import repo_contract
    from pipeline.analysis.spec_excerpt import defined_tables, error_codes, task_excerpt

    agent = ticket_dir / AGENT_DIR
    plan_path, spec_path = agent / "plan.json", agent / "spec.md"
    if not plan_path.is_file() or not spec_path.is_file():
        return ""
    try:
        plan = ImplementationPlan.model_validate_json(plan_path.read_text())
    except ValueError:
        return ""
    task = next((t for t in plan.tasks if t.repo == repo), None)
    if task is None:
        return ""
    own = repo_contract(plan, task)
    cited = "\n".join([*plan.contract.notes, task.summary, *task.steps,
                       *(c.what for c in task.changes)])  # fmt: skip
    endpoints = [(e.method, e.path) for e in [*own.internal_endpoints, *own.public_endpoints]]
    codes = {e.code for ep in own.internal_endpoints for e in ep.errors}
    codes |= {str(v) for ep in own.public_endpoints for m in ep.error_mapping
              for k, v in m.items() if k.endswith("code") and v}  # fmt: skip
    # the resource's table definitions the ticket's excerpt holds (a task may name the model, not
    # the table) and the ticket's own error codes (its error reference) stay in
    excerpt_path, ticket_path = agent / "spec_excerpt.md", agent / "ticket.md"
    if excerpt_path.is_file():
        cited += "\n" + " ".join(sorted(defined_tables(excerpt_path.read_text())))
    if ticket_path.is_file():
        codes |= error_codes(ticket_path.read_text())
    return task_excerpt(spec_path.read_text(), cited, endpoints, codes)


def link_or_copy(src: Path, dst: Path) -> None:
    """`dst` as a hard link to `src` (same content, no extra data); a copy when linking fails
    (another filesystem). An existing `dst` is replaced."""
    dst.unlink(missing_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copyfile(src, dst)


def copy_spec_inputs(ticket_agent: Path, agent: Path, repo: str) -> None:
    """ticket.md and spec.md as copies (an agent reads only inside its own repo, §9.8), and the
    spec excerpt for this repo's task, else the ticket's."""
    if (ticket_agent / "ticket.md").exists():
        shutil.copyfile(ticket_agent / "ticket.md", agent / "ticket.md")
    if (ticket_agent / "spec.md").exists():  # the whole spec (up to hundreds of KB): a hard link
        link_or_copy(ticket_agent / "spec.md", agent / "spec.md")  # shares the data, no second copy
    own = repo_spec_excerpt(ticket_agent.parent, repo)
    if own:
        (agent / "spec_excerpt.md").write_text(own)
    elif (ticket_agent / "spec_excerpt.md").exists():
        shutil.copyfile(ticket_agent / "spec_excerpt.md", agent / "spec_excerpt.md")


def _task(ticket_dir: Path, repo: str) -> RepoTask | None:
    path = ticket_dir / AGENT_DIR / "plan.json"
    try:
        plan = ImplementationPlan.model_validate_json(path.read_text())
    except (OSError, ValueError):
        return None
    return next((t for t in plan.tasks if t.repo == repo), None)


def start_files(worktree: Path, task: RepoTask | None) -> list[str]:
    """Existing files the task names (files to edit, then files to copy from), in that order."""
    if task is None:
        return []
    names = [c.file for c in task.changes if c.change == "edit"] + list(task.files_hint)
    out: list[str] = []
    for n in names:
        n = n.strip().lstrip("./")
        if n and n not in out and (worktree / n).is_file():
            out.append(n)
    return out


SIBLING_LIMIT = 14_000  # characters per sibling test file in the prompt
SIBLING_BUDGET = 40_000  # all sibling tests + helpers together
_TEST_DIRS = {"test", "tests", "__tests__", "spec"}
_SKIP_DIRS = {"node_modules", ".git", ".agent", "coverage", "dist", "build"}
_NOISE = {"spec", "test", "tests", "js", "ts", "api", "unit", "index"}
_REQUIRE = re.compile(r"""(?:require\(|from\s+)['"](\.{1,2}/[^'"]+)['"]""")


def _tokens(name: str) -> set[str]:
    """Lower-case words of a file name: `ageingDeductionConfig.api.spec.js` →
    {ageing, deduction, config}."""
    words = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", name.rsplit("/", 1)[-1])
    return {w.lower() for w in words} - _NOISE


def test_files(worktree: Path) -> list[str]:
    """Every test file of the repo (under a test directory, node_modules left out)."""
    out: list[str] = []
    for top in sorted(_TEST_DIRS):
        root = worktree / top
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            rel = p.relative_to(worktree)
            skipped = _SKIP_DIRS & set(rel.parts)
            if p.is_file() and not skipped and p.suffix in {".js", ".ts", ".mjs"}:
                out.append(rel.as_posix())
    return out


def sibling_tests(worktree: Path, task: RepoTask | None, skip: set[str]) -> list[str]:
    """The existing tests closest to the test files the task adds (same words in the name, same
    folder first), at most two, then the test helpers they require (at most three): the
    structure, mocks and helpers a new spec copies. Coders otherwise find them one read at a
    time (DEMO-11: ~12 reads before the first write). `skip`: files already in the prompt."""
    if task is None:
        return []
    new = [c.file for c in task.changes if c.change == "new" and _is_test(c.file)]
    if not new:
        return []
    existing = [f for f in test_files(worktree) if f not in skip and f not in new]
    scored: list[tuple[float, str]] = []
    for f in existing:
        score = 0.0
        for n in new:
            common = len(_tokens(n) & _tokens(f))
            same = f.rsplit("/", 1)[0] == n.rsplit("/", 1)[0]
            near = f.rsplit("/", 2)[0] == n.rsplit("/", 2)[0]
            score = max(score, common + (1.0 if same else 0.5 if near else 0.0) * (common > 0))
        if score > 0:
            scored.append((score, f))
    scored.sort(key=lambda s: (-s[0], len(s[1]), s[1]))
    chosen = [f for _, f in scored[:2]]
    helpers: list[str] = []
    for f in chosen:
        base = (worktree / f).parent
        for ref in _REQUIRE.findall((worktree / f).read_text(errors="replace")):
            for cand in (base / ref, base / f"{ref}.js", base / ref / "index.js"):
                try:
                    rel = cand.resolve().relative_to(worktree.resolve()).as_posix()
                except ValueError:
                    continue
                taken = rel in chosen or rel in helpers or rel in skip
                if cand.is_file() and _TEST_DIRS & set(rel.split("/")[:1]) and not taken:
                    helpers.append(rel)
                    break
    return chosen + helpers[:3]


def _is_test(path: str) -> bool:
    return bool(_TEST_DIRS & set(path.split("/")[:1])) or bool(
        re.search(r"\.(spec|test)\.[cm]?[jt]s$", path))


def inline_inputs(worktree: Path, ticket_dir: Path, repo: str, fix: bool) -> str:
    """The coder's inputs in the prompt, so it starts coding instead of reading (D129): task,
    this repo's part of the contract, the ticket without the criteria `task.md` already has, the
    spec sections for this task, and (not in fix runs) the existing files its task names, within
    `FILES_BUDGET`. Not the repo's `CLAUDE.md`: Claude Code / Codex load it themselves (D171)."""
    agent = worktree / AGENT_DIR
    parts: list[tuple[str, Path]] = []
    if (agent / CHANGES_FILE).is_file():  # a redo: the changes come first (D130)
        parts.append(("Only these changes (`.agent/changes.md`)", agent / CHANGES_FILE))
    parts += [("Your task (`.agent/task.md`)", agent / "task.md"),
                                     ("This repo's part of the contract (`.agent/contract.json`)",
                                      agent / "contract.json")]  # fmt: skip
    if fix:
        parts.append(("The findings to fix (`.agent/findings.json`)", agent / "findings.json"))
    else:
        parts.append(("Spec sections for your task (`.agent/spec_excerpt.md`)",
                      agent / "spec_excerpt.md"))  # fmt: skip
    out = ["", "These inputs are already below: **don't read them again**."]
    for title, path in parts:
        if path.is_file():
            out += ["", f"## Input: {title}", "", path.read_text(errors="replace").strip()]
    if not fix and (agent / "ticket.md").is_file():  # the criteria are in the task already
        from pipeline.agents.planner import ticket_ac_split

        ticket = (agent / "ticket.md").read_text(errors="replace")
        texts, without = ticket_ac_split(ticket)
        title = ("The ticket without its numbered acceptance criteria (they are in your task)"
                 if texts else "The ticket")  # fmt: skip
        out += ["", f"## Input: {title} (`.agent/ticket.md`)", "", without.strip()]
    if fix:
        return "\n".join(out)
    used, shown, rest, shown_names = 0, [], [], []
    for rel in start_files(worktree, _task(ticket_dir, repo)):
        text = (worktree / rel).read_text(errors="replace")
        if len(text) > FILE_LIMIT or used + len(text) > FILES_BUDGET:
            rest.append(rel)
            continue
        used += len(text)
        shown_names.append(rel)
        shown.append(f"### `{rel}`\n\n```\n{text.rstrip()}\n```")
    if shown:
        out += ["", "## Input: existing files your task names (current content)", "", *shown]
    sib, used_s = [], 0
    for rel in sibling_tests(worktree, _task(ticket_dir, repo), set(shown_names) | set(rest)):
        text = (worktree / rel).read_text(errors="replace")
        if used_s + min(len(text), SIBLING_LIMIT) > SIBLING_BUDGET:
            continue
        cut = len(text) > SIBLING_LIMIT
        used_s += min(len(text), SIBLING_LIMIT)
        sib.append(f"### `{rel}`\n\n```\n{text[:SIBLING_LIMIT].rstrip()}"
                   f"{chr(10) + '… (cut)' if cut else ''}\n```")  # fmt: skip
    if sib:
        out += ["", "## Input: the closest existing tests and their helpers (copy their structure "
                "and mocks; don't search for other specs)", "", *sib]
    if rest:
        out += ["", "Also named by your task (read them if you need them): "
                + ", ".join(f"`{r}`" for r in rest)]  # fmt: skip
    return "\n".join(out)


def coder_prompt(
    key: str, repo: str, retry: bool, fix: bool, leftovers: list[str] | None = None,
    guidance: bool = False, redo: bool = False,
) -> str:  # fmt: skip
    lines = ["## This run", "", f"- Ticket: **{key}**", f"- Repo: **{repo}** (this worktree)"]
    if redo:
        lines.append(
            "- **Redo after a plan change**: your task is already implemented on this branch. "
            "Apply only the changes in `.agent/changes.md` (below), then run the checks and "
            "commit. Don't redo, rewrite or re-verify anything else; the full task is for "
            "reference only."
        )
    if leftovers:
        shown = ", ".join(f"`{p}`" for p in leftovers[:10])
        more = f" (+{len(leftovers) - 10} more)" if len(leftovers) > 10 else ""
        lines.append(
            f"- **Earlier attempt left uncommitted changes** ({shown}{more}). Check them with "
            "`git status` / `git diff`, keep what is right, finish the task and list them in "
            "`.agent/commits.json`."
        )
    if guidance:
        lines.append(
            "- **Human guidance** in `.agent/guidance.md` (given after an earlier attempt "
            "stopped): read it first and follow it."
        )
    if fix:
        lines.append(
            "- Mode: **fix** the findings in `.agent/findings.json` (each has a `number`, the "
            "`#n` people use, and the `human_note` people wrote about it). People's decision and "
            "general notes are in `.agent/human_decision.json`: follow their notes"
        )
    if retry:
        lines.append(
            "- **Retry**: the orchestrator's checks failed on your last attempt. Read "
            "`.agent/verify_failures.md` and fix those problems first; your earlier commits are "
            "still on the branch."
        )
    return "\n".join(lines)


def code_repo(
    deps: Deps,
    key: str,
    repo: str,
    *,
    fix: bool = False,
    slot: str | None = None,
    provider_override: str | None = None,
    verify_failures: str | None = None,
    findings: list[dict] | None = None,
    guidance: list[dict] | None = None,
    human_decision: dict | None = None,
    redo: list[str] | None = None,
) -> tuple[CoderResult | None, AgentRun]:
    """Run the coder (or coder_fix) for one repo. Never raises for agent failures. `redo`: the
    repo's changes after a plan change (its code already exists); only those are applied."""
    cfg = deps.config.repo(repo)
    assert deps.workspaces is not None
    worktree = prepare_inputs(deps, key, repo, verify_failures, findings, guidance, human_decision,
                              None if fix else redo)  # fmt: skip
    leftovers = deps.workspaces.changed_paths(worktree)["vs_head"]
    deps.workspaces.set_identity(repo, deps.config.git.coder_author)
    variables = {
        "KEY": key,
        "REPO": repo,
        "ROLE": cfg.role,
        "COMMANDS": commands_md(cfg),
        "TICKET_DIR": str(deps.workspaces.ticket_dir(key)),
    }
    role = "coder_fix" if fix else "coder"
    prompt = coder_prompt(key, repo, bool(verify_failures), fix, leftovers, bool(guidance),
                          redo is not None and not fix)  # fmt: skip
    prompt += inline_inputs(worktree, deps.workspaces.ticket_dir(key), repo, fix)
    # P9 (time, cost): a repo keeps one coder session per branch: check retries, review fixes,
    # re-coding after a plan change and retries after guidance continue it (it knows the code it
    # wrote); a QA round's new branch starts fresh
    result, run = deps.runner.run(
        key, role, worktree, prompt, CoderResult,
        repo=repo, provider_override=provider_override, slot=slot, prompt_vars=variables,
        continue_session=True, session_scope=deps.workspaces.current_branch(worktree),
    )  # fmt: skip
    if result is not None and result.repo != repo:
        result = result.model_copy(update={"repo": repo})
    return result, run


def resolve_conflicts(
    deps: Deps,
    key: str,
    repo: str,
    onto: str,
    files: list[str],
    *,
    slot: str | None = None,
    provider_override: str | None = None,
) -> tuple[CoderResult | None, AgentRun]:
    """A rebase onto `onto` stopped with conflicts in `files` (D125): the coder resolves them
    and stages the files; the orchestrator continues the rebase. Never raises for agent
    failures."""
    cfg = deps.config.repo(repo)
    assert deps.workspaces is not None
    worktree = prepare_inputs(deps, key, repo)
    (worktree / AGENT_DIR / "conflicts.json").write_text(
        json.dumps({"onto": onto, "files": files}, indent=2) + "\n"
    )
    variables = {"KEY": key, "REPO": repo, "ROLE": cfg.role, "COMMANDS": commands_md(cfg),
                 "TICKET_DIR": str(deps.workspaces.ticket_dir(key))}  # fmt: skip
    prompt = "\n".join([
        "## This run", "", f"- Ticket: **{key}**", f"- Repo: **{repo}** (this worktree)",
        f"- A `git rebase {onto}` stopped with conflicts in: "
        + ", ".join(f"`{f}`" for f in files),
    ])  # fmt: skip
    result, run = deps.runner.run(
        key, "coder_rebase", worktree, prompt, CoderResult,
        repo=repo, provider_override=provider_override, slot=slot, prompt_vars=variables,
    )  # fmt: skip
    if result is not None and result.repo != repo:
        result = result.model_copy(update={"repo": repo})
    return result, run


# ---------- one session for the whole ticket (agents.single_session) ----------


def _plan_of(ticket_dir: Path) -> ImplementationPlan | None:
    try:
        return ImplementationPlan.model_validate_json(
            (ticket_dir / AGENT_DIR / "plan.json").read_text())
    except (OSError, ValueError):
        return None


def ticket_spec_excerpt(ticket_dir: Path) -> str:
    """The spec sections the whole plan relies on, once (the union of what each repo's task
    cites, its endpoints, tables and error codes); the planner's excerpt when nothing matches."""
    from pipeline.analysis.spec_excerpt import defined_tables, error_codes, task_excerpt

    agent = ticket_dir / AGENT_DIR
    plan, spec = _plan_of(ticket_dir), agent / "spec.md"
    fallback = (agent / "spec_excerpt.md").read_text(errors="replace") \
        if (agent / "spec_excerpt.md").is_file() else ""  # fmt: skip
    if plan is None or not spec.is_file():
        return fallback
    c = plan.contract
    cited = "\n".join([*c.notes, *(t.summary for t in plan.tasks),
                       *(s for t in plan.tasks for s in t.steps),
                       *(ch.what for t in plan.tasks for ch in t.changes)])  # fmt: skip
    if fallback:  # the resource's tables the planner's excerpt holds
        cited += "\n" + " ".join(sorted(defined_tables(fallback)))
    endpoints = [(e.method, e.path) for e in [*c.internal_endpoints, *c.public_endpoints]]
    codes = {e.code for ep in c.internal_endpoints for e in ep.errors}
    codes |= {str(v) for ep in c.public_endpoints for m in ep.error_mapping
              for k, v in m.items() if k.endswith("code") and v}  # fmt: skip
    if (agent / "ticket.md").is_file():
        codes |= error_codes((agent / "ticket.md").read_text())
    return task_excerpt(spec.read_text(), cited, endpoints, codes) or fallback


def ticket_commit_plan(agent: Path, repos: list[str]) -> dict[str, list[tuple[str, list[str]]]]:
    """`.agent/commits.json` of a ticket-level coder: `[{repo, message, files}]` → per repo, in
    order. Paths are inside the repo; a folder prefix the coder added is stripped. An entry
    without a repo goes to the only repo when there is one."""
    path = agent / COMMITS_FILE
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    if isinstance(data, dict):
        data = data.get("commits")
    out: dict[str, list[tuple[str, list[str]]]] = {}
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict):
            continue
        repo = str(entry.get("repo") or (repos[0] if len(repos) == 1 else "")).strip()
        message = str(entry.get("message") or "").strip()
        files = [str(f).strip().removeprefix(f"{repo}/") for f in entry.get("files") or []
                 if str(f).strip()]  # fmt: skip
        if repo in repos and message and files:
            out.setdefault(repo, []).append((message, files))
    return out


def standards_md(worktree: Path) -> str:
    for name in ("CLAUDE.md", "AGENTS.md"):
        if (worktree / name).is_file():
            return (worktree / name).read_text(errors="replace").strip()
    return ""


def repo_inputs_md(worktree: Path, ticket_dir: Path, repo: str, fix: bool) -> list[str]:
    """The existing files the repo's task names and the closest tests, as prompt blocks with
    the repo folder in each path (the same budgets as a per-repo coder)."""
    if fix:
        return []
    task = _task(ticket_dir, repo)
    used, shown, rest, names = 0, [], [], []
    for rel in start_files(worktree, task):
        text = (worktree / rel).read_text(errors="replace")
        if len(text) > FILE_LIMIT or used + len(text) > FILES_BUDGET:
            rest.append(rel)
            continue
        used += len(text)
        names.append(rel)
        shown.append(f"### `{repo}/{rel}`\n\n```\n{text.rstrip()}\n```")
    out = []
    if shown:
        out += [f"## Input: existing files of `{repo}` your task names (current content)",
                "", *shown, ""]  # fmt: skip
    if rest:
        out += ["Also named by your task (read them if you need them): "
                + ", ".join(f"`{repo}/{r}`" for r in rest), ""]  # fmt: skip
    sib, used_s = [], 0
    for rel in sibling_tests(worktree, task, set(names) | set(rest)):
        text = (worktree / rel).read_text(errors="replace")
        if used_s + min(len(text), SIBLING_LIMIT) > SIBLING_BUDGET:
            continue
        used_s += min(len(text), SIBLING_LIMIT)
        cut = "\n… (cut)" if len(text) > SIBLING_LIMIT else ""
        sib.append(f"### `{repo}/{rel}`\n\n```\n{text[:SIBLING_LIMIT].rstrip()}{cut}\n```")
    if sib:
        out += [f"## Input: the closest existing tests of `{repo}` and their helpers (copy their "
                "structure and mocks; don't search for other specs)", "", *sib, ""]  # fmt: skip
    return out


def ticket_follow_up(key: str, repos: list[str], *, failures: dict[str, str],
                     findings: dict[str, list[dict]], decision: dict | None,
                     redo: dict[str, list[str]], guidance: list[dict] | None,
                     leftovers: dict[str, list[str]]) -> str:  # fmt: skip
    """What a continued ticket session is told: only what is new (the plan, contract, ticket and
    spec are already in its conversation)."""
    lines = [f"## This run\n\n- Ticket: **{key}**; the repos (folders here): "
             + ", ".join(f"**{r}**" for r in repos)]
    for repo in findings:
        lines.append(f"- Fix the review findings of **{repo}** (below); their `human_note` is "
                     "what people wrote about each.")
    if decision:
        lines += ["- People's decision and general notes (follow their notes):",
                  "", "```json", json.dumps(decision, indent=1, default=str), "```"]
    for repo, items in findings.items():
        lines += ["", f"## Input: the findings to fix in `{repo}`", "", "```json",
                  json.dumps(items, indent=1, default=str), "```"]  # fmt: skip
    for repo, md in failures.items():
        lines += ["", f"## Input: what the automated checks found wrong in `{repo}`", "",
                  md.strip(), "", "Fix those problems first; your earlier work is still there."]
    for repo, changes in redo.items():
        lines += ["", f"## Input: redo in `{repo}` after a plan change", "",
                  changes_md(repo, changes)]  # fmt: skip
    for repo, files in leftovers.items():
        shown = ", ".join(f"`{p}`" for p in files[:10])
        lines.append(f"- Uncommitted changes in `{repo}` from an earlier attempt ({shown}): check "
                     "them, keep what is right and list them in `.agent/commits.json`.")
    text = guidance_md(guidance)
    if text:
        lines += ["", "## Input: human guidance (follow it)", "", text.strip()]
    return "\n".join(lines)


def ticket_prompt(deps: Deps, key: str, repos: list[str], *, fix: bool, failures: dict[str, str],
                  findings: dict[str, list[dict]], decision: dict | None,
                  redo: dict[str, list[str]], guidance: list[dict] | None,
                  leftovers: dict[str, list[str]]) -> str:  # fmt: skip
    """The first prompt of a ticket session: the plan once, the contract once, the ticket once,
    the spec sections once, each repo's standards and starting files, and the checks per repo."""
    from pipeline.agents.planner import render_ticket_plan_md, ticket_ac_split

    ws = deps.workspaces
    assert ws is not None
    ticket_dir = ws.ticket_dir(key)
    agent = ticket_dir / AGENT_DIR
    plan = _plan_of(ticket_dir)
    ticket = (agent / "ticket.md").read_text(errors="replace") \
        if (agent / "ticket.md").is_file() else ""  # fmt: skip
    texts, without = ticket_ac_split(ticket)
    out = [ticket_follow_up(key, repos, failures=failures, findings=findings, decision=decision,
                            redo=redo, guidance=guidance, leftovers=leftovers), "",
           "These inputs are already below: **don't read them again**.", ""]  # fmt: skip
    if plan is not None:
        out += ["## Input: the plan", "", render_ticket_plan_md(plan, key, repos, texts), "",
                "## Input: the interface between the repos (authoritative)", "", "```json",
                plan.contract.model_dump_json(indent=1), "```", ""]  # fmt: skip
    excerpt = ticket_spec_excerpt(ticket_dir)
    if excerpt:
        out += ["## Input: spec sections the plan relies on", "", excerpt.strip(), ""]
    if ticket and not fix:
        title = ("the ticket without its numbered acceptance criteria (they are in the plan)"
                 if texts else "the ticket")  # fmt: skip
        out += [f"## Input: {title}", "", without.strip(), ""]
    for repo in repos:
        cfg = deps.config.repo(repo)
        worktree = ws.worktree_path(key, repo)
        std = standards_md(worktree)
        if std:
            out += [f"## Input: standards of `{repo}` ({cfg.role}; its `CLAUDE.md`)", "", std, ""]
        out += repo_inputs_md(worktree, ticket_dir, repo, fix)
        out += [f"## Checks for `{repo}` (run them from inside the folder: `cd {repo} && …`)",
                "", commands_md(cfg), ""]  # fmt: skip
    return "\n".join(out)


def code_ticket(
    deps: Deps,
    key: str,
    repos: list[str],
    *,
    fix: bool = False,
    slot: str | None = None,
    provider_override: str | None = None,
    failures: dict[str, str] | None = None,
    findings: dict[str, list[dict]] | None = None,
    guidance: list[dict] | None = None,
    human_decision: dict | None = None,
    redo: dict[str, list[str]] | None = None,
) -> tuple[dict[str, CoderResult] | None, AgentRun]:
    """One coder session for every repo in `repos`, in the ticket's folder (agents.single_session).
    It writes to the repos' worktrees only (nothing else is editable), reports one result per
    repo and one commit plan; its session is kept for the ticket, so check retries and review
    fixes continue it. Never raises for agent failures."""
    ws = deps.workspaces
    assert ws is not None, "Deps.workspaces is required for coding"
    ticket_dir = ws.ticket_dir(key)
    agent = ticket_dir / AGENT_DIR
    agent.mkdir(parents=True, exist_ok=True)
    (agent / COMMITS_FILE).unlink(missing_ok=True)
    failures, findings, redo = failures or {}, findings or {}, redo or {}
    leftovers: dict[str, list[str]] = {}
    for repo in repos:
        ws.ensure_env(key, repo)  # tests / commit hooks need it (D69)
        ws.set_identity(repo, deps.config.git.coder_author)
        left = ws.changed_paths(ws.worktree_path(key, repo))["vs_head"]
        if left:
            leftovers[repo] = left
    kw = {"fix": fix, "failures": failures, "findings": findings, "decision": human_decision,
          "redo": redo, "guidance": guidance, "leftovers": leftovers}  # fmt: skip
    prompt = ticket_prompt(deps, key, repos, **kw)
    follow_up = ticket_follow_up(key, repos, failures=failures, findings=findings,
                                 decision=human_decision, redo=redo, guidance=guidance,
                                 leftovers=leftovers)  # fmt: skip
    role = "coder_ticket_fix" if fix else "coder_ticket"
    # a ticket's repos share one branch name, so a run for the gateway alone (a failed check)
    # continues the session of the run that coded both
    branches = "+".join(sorted({str(ws.current_branch(ws.worktree_path(key, r))) for r in repos}))
    variables = {"KEY": key, "TICKET_DIR": str(ticket_dir)}
    protect = tuple(f"{r}/{p.lstrip('./')}" for r in repos
                    for p in deps.config.repo(r).shared_paths)  # fmt: skip

    def check(result: CoderResults) -> list[str]:
        named = [r.repo for r in result.results]
        problems = [f"no result for repo `{r}`" for r in repos if r not in named]
        problems += [f"a result for `{r}`, which this session was not asked to code"
                     for r in sorted(set(named) - set(repos))]  # fmt: skip
        problems += [f"two results for repo `{r}`" for r in sorted({r for r in named
                                                                    if named.count(r) > 1})]
        return problems

    result, run = deps.runner.run(
        key, role, ticket_dir, prompt, CoderResults, provider_override=provider_override,
        slot=slot, prompt_vars=variables, continue_session=True, follow_up=follow_up,
        session_scope=branches, check=check, check_error="invalid_results", protect=protect,
    )  # fmt: skip
    if result is None:
        return None, run
    return {r.repo: r for r in result.results}, run
