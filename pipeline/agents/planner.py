"""Planner Agent: ticket → per-repo tasks + interface contract (Phase 2 §3)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pipeline.agents.roles import GUIDANCE_FILE, guidance_md
from pipeline.analysis.repo_map import repo_map
from pipeline.analysis.spec_excerpt import excerpt, table_definitions
from pipeline.config import ROLE_RANK
from pipeline.logging import get_logger
from pipeline.schemas import (
    AcceptanceCriterion,
    AgentRun,
    FileChange,
    ImplementationPlan,
    InterfaceContract,
    RepoTask,
    TicketContext,
)
from pipeline.workspace import AGENT_DIR, branch_name

if TYPE_CHECKING:
    from pipeline.deps import Deps

MODES = ("initial", "revise", "replan", "qa_fix", "change")
MODE_FILES = ("previous_plan.json", "feedback.json", "deviations.json", "bugs.json", "change.json")
SPEC_EXCERPT = "spec_excerpt.md"
REPO_MAP = "repo_map.md"
_ENDPOINT_LINE = re.compile(r"\b(?:GET|POST|PUT|PATCH|DELETE)\s+/[\w\-/:{}.]+")


# ---------- inputs ----------


def _dump(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def write_inputs(ctx: TicketContext, mode: str, deps: Deps,
                 extra: dict | None) -> tuple[Path, list[dict], str]:  # fmt: skip
    """Worktrees + `.agent/` inputs for the planner. Returns the ticket dir (the planner's cwd),
    the repos list and the repo map. Those two go into the prompt only (the planner never opens
    them as files, so none are written)."""
    ws = deps.workspaces
    assert ws is not None, "Deps.workspaces is required for planning"
    extra = extra or {}
    branch = extra.get("branch") or branch_name(ctx.key, ctx.summary)  # QA rounds: …-qa<n>
    repos = []
    for name in ctx.repos:
        path = ws.create_worktree(ctx.key, name, branch)
        cfg = deps.config.repo(name)
        repos.append(
            {"name": name, "role": cfg.role, "description": cfg.description, "path": f"./{name}",
             "absolute_path": str(path), "base_branch": cfg.default_branch}
        )  # fmt: skip
    agent = ws.write_ticket_files(ctx)
    ticket_text = f"{ctx.summary}\n{ctx.description_md}"
    (agent / SPEC_EXCERPT).write_text(excerpt(ctx.spec_md, ctx.summary, ticket_text))
    words = "\n".join([ctx.summary, *_ENDPOINT_LINE.findall(ctx.description_md)])
    maps = [repo_map(Path(r["absolute_path"]), r["name"], r["role"], words) for r in repos]
    repo_map_md = "# Repo map\n\n" + "\n".join(maps)
    # stale inputs from an earlier run must not leak (repos.json / repo_map.md: older versions)
    for name in ("repos.json", REPO_MAP, *MODE_FILES, GUIDANCE_FILE):
        (agent / name).unlink(missing_ok=True)

    guidance = guidance_md(extra.get("guidance"))
    if guidance:
        (agent / GUIDANCE_FILE).write_text(guidance)
    if mode != "initial":
        previous = extra.get("previous_plan")
        if previous is None:
            raise ValueError(f"mode {mode!r} needs extra['previous_plan']")
        if isinstance(previous, ImplementationPlan):
            previous = previous.model_dump(mode="json")
        (agent / "previous_plan.json").write_text(_dump(previous))
        # the planner edits this file in place instead of writing the whole plan again
        (agent / "plan.json").write_text(_dump(previous))
    if mode == "revise":
        (agent / "feedback.json").write_text(_dump({"feedback": list(extra.get("feedback", []))}))
    elif mode == "replan":
        (agent / "deviations.json").write_text(
            _dump({"deviations": list(extra.get("deviations", []))})
        )
    elif mode == "qa_fix":
        (agent / "bugs.json").write_text(_dump({"bugs": list(extra.get("bugs", []))}))
    elif mode == "change":
        (agent / "change.json").write_text(_dump(extra.get("change") or {}))
    return ws.ticket_dir(ctx.key), repos, repo_map_md


SINGLE_SESSION_NOTE = """\
- **One agent codes every repo of this ticket in one session** (service first, then the gateway), so
  the contract is a short list, not a specification both sides must parse. Write the endpoints
  compactly: no JSON Schema (`"type": "object"`, `"properties"`, `"required": […]` inside `body`
  and the like). `request_schema` is `{"params": {"id": "uuid"}, "query": {"page": "integer"},
  "body": {"fields": {"status": "active|inactive"}, "required": ["status"]}}` (field → type or the
  allowed values; `required` only for a body with required fields; `{}` for a part that isn't
  used); `response_schema` is `{"status": "success", "data": {"config_id": "uuid", …}}` (the
  fields and their types). `errors`: every `{status, code, when}` as before. `error_mapping`: only
  entries where the client sees something other than the service's answer, plus the gateway's own
  errors; `note` stays `""`. Method, path, auth, ids, `is_new`, `calls`, `implements`,
  `consumes` as before."""


def planner_prompt(ctx: TicketContext, mode: str, guidance: bool = False,
                   ticket_dir: Path | None = None, repos: list[dict] | None = None,
                   repo_map_md: str | None = None, single: bool = False) -> str:  # fmt: skip
    """The run's facts, then the prepared inputs inline (ticket, spec excerpt, repo map, each
    repo's CLAUDE.md) so the planner starts planning instead of searching (D128)."""
    files = {
        "initial": "",
        "revise": "`.agent/previous_plan.json`, `.agent/feedback.json`",
        "replan": "`.agent/previous_plan.json`, `.agent/deviations.json`",
        "qa_fix": "`.agent/previous_plan.json`, `.agent/bugs.json`",
        "change": "`.agent/previous_plan.json`, `.agent/change.json`",
    }[mode]
    lines = [
        "## This run",
        "",
        f"- Ticket: **{ctx.key}** — {ctx.summary}",
        f"- Mode: **{mode}**",
        f"- Repos: {', '.join(ctx.repos)}",
    ]
    for r in repos or []:
        about = f": {r['description']}" if r.get("description") else ""
        lines.append(f"  - **{r['name']}** ({r['role']}, folder `{r['path']}`){about}")
    if files:
        lines.append(f"- Mode inputs: {files}")
    if single:
        lines.append(SINGLE_SESSION_NOTE)
    if guidance:
        lines.append(
            "- **Human guidance** in `.agent/guidance.md` (given after the last run stopped): "
            "read it first and follow it."
        )
    if ticket_dir is None:
        return "\n".join(lines)
    agent = ticket_dir / AGENT_DIR
    inline = [("The ticket (`.agent/ticket.md`)", agent / "ticket.md"),
              ("Spec excerpt (`.agent/spec_excerpt.md`)", agent / SPEC_EXCERPT)]  # fmt: skip
    if repo_map_md is None and (agent / REPO_MAP).is_file():  # a map left as a file
        repo_map_md = (agent / REPO_MAP).read_text()
    inline += [(f"Standards of {r} (`{r}/CLAUDE.md`)", ticket_dir / r / "CLAUDE.md")
               for r in ctx.repos]  # fmt: skip
    # P9 (time): the closest existing source files of each repo, which the planner otherwise
    # reads one per step (DEMO-10: 13 reads, ~17 s each, then the 10-minute limit)
    near = near_files(repo_map_md) if repo_map_md else {}
    capped: set[str] = set()  # only these source files are cut: the ticket and the spec are whole
    for repo, paths in near.items():
        inline += [(f"{repo}: `{p}`", ticket_dir / repo / p) for p in paths]
        capped |= {f"{repo}: `{p}`" for p in paths}
    lines += ["", "These inputs are already below: **don't read them again**."]
    if repo_map_md:
        lines += ["", "## Input: Repo map", "", repo_map_md.strip()]
    for title, path in inline:
        if path.is_file():
            text = path.read_text(errors="replace").strip()
            cut = text.splitlines()
            if len(cut) > MAX_NEAR_LINES and title in capped:
                text = (
                    "\n".join(cut[:MAX_NEAR_LINES])
                    + f"\n… ({len(cut) - MAX_NEAR_LINES} more lines)"
                )
            lines += ["", f"## Input: {title}", "", text]
    return "\n".join(lines)


MAX_NEAR_FILES = 6  # per repo
MAX_NEAR_LINES = 250  # per file
_NEAR_ITEM = re.compile(r"^- `([^`]+)`", re.M)
_NOT_NEAR = re.compile(
    r"(^|/)(test|tests|__tests__|spec|migrations|seeders|workers)/|\.spec\.", re.I
)


def near_files(repo_map_md: str) -> dict[str, list[str]]:
    """Per repo, the first source files of its "Files near this ticket" list in the repo map
    (closest first): no tests, migrations, seeders or workers."""
    out: dict[str, list[str]] = {}
    for block in re.split(r"^## ", repo_map_md, flags=re.M)[1:]:
        repo = block.split(" ", 1)[0].strip()
        section = block.split("**Files near this ticket**", 1)
        if len(section) < 2:
            continue
        body = section[1].split("**Routes", 1)[0]
        picked = [p for p in _NEAR_ITEM.findall(body) if not _NOT_NEAR.search(p)]
        if picked:
            out[repo] = picked[:MAX_NEAR_FILES]
    return out


def continuation_prompt(ctx: TicketContext, mode: str, guided: bool, ticket_dir: Path,
                        single: bool = False) -> str:  # fmt: skip
    """The follow-up for a continued planner session: what's asked now and only the inputs that
    can be new (the ticket, which may hold answers; guidance; the mode's own inputs)."""
    agent = ticket_dir / AGENT_DIR
    asked = {
        "initial": "Plan the ticket now (you asked questions before: the answers are in the "
                   "ticket or the guidance below)",
        "revise": "Revise your plan with people's feedback (`.agent/feedback.json`, below)",
        "replan": "Update your plan for what came up while coding (`.agent/deviations.json`)",
        "qa_fix": "Plan the fixes for the QA bugs (`.agent/bugs.json`); the code has changed "
                  "since you read it (merged), so re-read the files the fixes touch",
        "change": "Update your plan for a person's change request (`.agent/change.json`)",
    }[mode]  # fmt: skip
    lines = [planner_prompt(ctx, mode, guided, single=single), "",
             f"**Continue from your earlier work in this session.** {asked}. You already have "
             "the spec excerpt, the repo map, the standards and the code you read: don't read "
             "them again unless something below needs it. Follow the rules for this mode from "
             "the instructions at the start. `.agent/plan.json` must end up complete, in the "
             "same format; the note at the end of this message says how to update it."]  # fmt: skip
    files = [("The ticket now (`.agent/ticket.md`)", agent / "ticket.md")]
    if guided:
        files.append(("Human guidance (`.agent/guidance.md`)", agent / GUIDANCE_FILE))
    files += [(f"`.agent/{n}`", agent / n) for n in MODE_FILES if n != "previous_plan.json"]
    for title, path in files:
        if path.is_file():
            lines += ["", f"## Input: {title}", "", path.read_text(errors="replace").strip()]
    return "\n".join(lines)


# ---------- validation (3.2) ----------


def _norm(text: str) -> str:
    return re.sub(r"[\s*_`]+", " ", text).strip().lower()


# "AC 4 — …" / "**AC4:**" / "h3. AC-4" at the start of a line; not "AC 10–14" in a table.
_TICKET_AC = re.compile(
    r"^[\s#*_>\-]*(?:h\d\.\s*)?[*_]*AC[\s\-]?(\d{1,3})\b(?!\s*[–-]\s*\d)", re.I | re.M
)
_AC_ID = re.compile(r"AC[\s\-]?(\d{1,3})", re.I)


def ticket_ac_ids(text: str) -> list[str]:
    """Numbered acceptance criteria the ticket itself defines ("AC1", "AC2" …), in order."""
    out: list[str] = []
    for m in _TICKET_AC.finditer(text or ""):
        i = f"AC{int(m.group(1))}"
        if i not in out:
            out.append(i)
    return out


# what ends a criterion's text: a heading, a table, or a bold title line ("**B. Errors**",
# "**Error reference**"); the criteria's own lines are bullets ("* **Given** …")
_SECTION_END = re.compile(r"^\s*(?:#|\||\*\*[^*\n]+\*\*)")


def ticket_ac_split(text: str) -> tuple[dict[str, str], str]:
    """The ticket's numbered criteria in its own words ({"AC1": "AC 1 — … Given … Then …"}),
    and the ticket without them (context, rules, error tables stay). ({}, text) when the ticket
    numbers none. Each criterion runs to the next one, a heading, a table or a bold title."""
    texts: dict[str, str] = {}
    rest: list[str] = []
    cur: str | None = None
    buf: list[str] = []

    def flush() -> None:
        if cur and cur not in texts:
            body = "\n".join(buf).strip().replace("**", "")
            texts[cur] = body  # the ticket's words, whole (a cut criterion is a wrong criterion)

    for line in (text or "").splitlines():
        m = _TICKET_AC.match(line)
        if m:
            flush()
            cur, buf = f"AC{int(m.group(1))}", [line]
            continue
        if cur and _SECTION_END.match(line):
            flush()
            cur, buf = None, []
        if cur:
            buf.append(line)
        else:
            rest.append(line)
    flush()
    return (texts, "\n".join(rest)) if texts else ({}, text or "")


def complete_brief(plan: ImplementationPlan, ctx: TicketContext) -> ImplementationPlan:
    """The brief lists every criterion the ticket numbers, in the ticket's own words: ones the
    planner left out are added, blank texts filled. The planner only has to say which task each
    one belongs to (DEMO-12: a 32-criterion ticket, the planner's list stopped at AC20)."""
    own = ticket_ac_split(ctx.description_md)[0]
    have = {ac_id(c.id) or c.id for c in plan.brief.criteria}
    added = [AcceptanceCriterion(id=i, text=own.get(i, "")) for i in ticket_ac_ids(
        ctx.description_md) if i not in have]  # fmt: skip
    if not added and all(c.text.strip() for c in plan.brief.criteria):
        return plan
    criteria = [c if c.text.strip() else c.model_copy(update={"text": own.get(
        ac_id(c.id) or c.id, "")}) for c in plan.brief.criteria]  # fmt: skip
    criteria = sorted([*criteria, *added], key=lambda c: _ac_number(c.id))
    return plan.model_copy(update={"brief": plan.brief.model_copy(update={"criteria": criteria})})


def _ac_number(cid: str) -> int:
    m = _AC_ID.fullmatch(cid.strip())
    return int(m.group(1)) if m else 10**6


_GATEWAY_WORDS = ("ws1", "gateway", "closio")
_SERVICE_WORDS = ("ws2",)


def assign_uncovered(plan: ImplementationPlan, ctx: TicketContext) -> ImplementationPlan:
    """The last chance: every criterion of the brief is in at least one task. One no task names
    goes to the repo its text points at ("WS1" / gateway: the gateway task; "WS2": the service
    task), else to every task: each repo's review and tests then answer it for itself (not
    applicable where it isn't theirs). Nothing is ever left without an owner."""
    own = ticket_ac_split(ctx.description_md)[0]
    used = {ac_id(a) for t in plan.tasks for a in t.acceptance_criteria}
    tasks = [t.model_copy() for t in plan.tasks]
    for c in plan.brief.criteria:
        cid = ac_id(c.id) or c.id
        if cid in used or not tasks:
            continue
        text = f"{c.text} {own.get(cid, '')}".lower()
        gateway = [t for t in tasks if t.role == "gateway"]
        service = [t for t in tasks if t.role != "gateway"]
        wants_gw = any(w in text for w in _GATEWAY_WORDS)
        wants_svc = any(w in text for w in _SERVICE_WORDS)
        owners = (gateway if wants_gw and not wants_svc and gateway
                  else service if wants_svc and not wants_gw and service else tasks)
        for t in owners:
            t.acceptance_criteria = [*t.acceptance_criteria, cid]
    return plan.model_copy(update={"tasks": tasks})


def repo_contract(plan: ImplementationPlan, task: RepoTask) -> InterfaceContract:
    """The part of the contract one repo works with: the internal endpoints it provides or
    calls, and (a gateway) the public ones; all notes. The whole contract stays in the ticket's
    `.agent/` for the contract check and the deployment note."""
    c = plan.contract
    ids = set(task.implements) | set(task.consumes)
    internal = [e for e in c.internal_endpoints if e.id in ids or e.provider_repo == task.repo
                or task.repo in e.consumer_repos]  # fmt: skip
    public = [e for e in c.public_endpoints if e.id in ids or task.role == "gateway"]
    if not internal and not public:
        return c
    return InterfaceContract(internal_endpoints=internal, public_endpoints=public, notes=c.notes)


def ac_id(text: str) -> str | None:
    """ "AC 4" / "ac-4" / "AC4" → "AC4"; None when `text` is not just an AC id."""
    m = _AC_ID.fullmatch(text.strip())
    return f"AC{int(m.group(1))}" if m else None


def validate_plan(plan: ImplementationPlan, ctx: TicketContext, deps: Deps, mode: str) -> list[str]:
    """Deterministic checks the orchestrator enforces before accepting a plan."""
    problems: list[str] = []
    if plan.status == "needs_clarification":
        if not plan.open_questions:
            problems.append("status is needs_clarification but open_questions is empty")
        return problems
    if not plan.tasks:
        problems.append("status is ready but there are no tasks")

    ticket_repos = set(ctx.repos)
    task_repos = [t.repo for t in plan.tasks]
    for repo in sorted({r for r in task_repos if task_repos.count(r) > 1}):
        problems.append(f"more than one task for repo {repo!r}")
    for t in plan.tasks:
        if t.repo not in ticket_repos:
            problems.append(f"task repo {t.repo!r} is not one of the ticket's repos {ctx.repos}")
        elif t.role != deps.config.repo(t.repo).role:
            problems.append(
                f"task {t.repo!r} has role {t.role!r}; the repo's role is "
                f"{deps.config.repo(t.repo).role!r}"
            )
    orders = [t.order for t in plan.tasks]
    for o in sorted({o for o in orders if orders.count(o) > 1}):
        problems.append(f"order {o} is used by more than one task")
    ranked = sorted(plan.tasks, key=lambda t: t.order)
    for a, b in zip(ranked, ranked[1:], strict=False):
        if ROLE_RANK[a.role] > ROLE_RANK[b.role]:
            problems.append(
                f"order puts {a.repo} ({a.role}) before {b.repo} ({b.role}); "
                "use service → gateway → frontend"
            )

    internal = {e.id: e for e in plan.contract.internal_endpoints}
    public = {e.id: e for e in plan.contract.public_endpoints}
    ids = [e.id for e in plan.contract.internal_endpoints + plan.contract.public_endpoints]
    for i in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"endpoint id {i!r} is used more than once")
    for e in internal.values():
        if e.provider_repo not in ticket_repos:
            problems.append(f"endpoint {e.id}: provider_repo {e.provider_repo!r} not in the ticket")
        for c in e.consumer_repos:
            if c not in ticket_repos:
                problems.append(f"endpoint {e.id}: consumer {c!r} not in the ticket")
    for pe in public.values():
        for c in pe.calls:
            if c not in internal:
                problems.append(f"public endpoint {pe.id} calls unknown internal endpoint {c!r}")
        if not pe.error_mapping:
            problems.append(f"public endpoint {pe.id} has no error_mapping")
    for t in plan.tasks:
        for i in t.implements:
            if i in internal and internal[i].provider_repo != t.repo:
                problems.append(
                    f"task {t.repo} implements {i}, but its provider_repo is "
                    f"{internal[i].provider_repo}"
                )
            elif i in public and t.role != "gateway":
                problems.append(
                    f"task {t.repo} implements public endpoint {i}; only the gateway can"
                )
            elif i not in internal and i not in public:
                problems.append(f"task {t.repo} implements unknown endpoint id {i!r}")
        for i in t.consumes:
            if i not in internal and i not in public:
                problems.append(f"task {t.repo} consumes unknown endpoint id {i!r}")

    if mode == "change":  # P5 §4b: each change names the repo that makes it
        if not plan.changes_from_previous:
            problems.append("mode change: changes_from_previous is empty; list each concrete "
                            "change as '<repo>: <what to change>'")  # fmt: skip
        for entry in plan.changes_from_previous:
            repo = entry.partition(":")[0].strip()
            if repo not in ticket_repos:
                problems.append(f"mode change: {entry!r} must start with one of the ticket's "
                                f"repos followed by ':' ({', '.join(ctx.repos)})")  # fmt: skip
    problems += check_spec_tables(plan, ctx.spec_md)
    if mode == "qa_fix":  # tasks only for repos the bugs touch: no coverage checks
        return problems

    by_repo = {t.repo: t for t in plan.tasks}
    for e in internal.values():
        task = by_repo.get(e.provider_repo)
        if e.is_new and (task is None or e.id not in task.implements):
            problems.append(f"new endpoint {e.id} has no task in {e.provider_repo} implementing it")
    gateways = [t for t in plan.tasks if t.role == "gateway"]
    for pe in public.values():
        if not any(pe.id in t.implements for t in gateways):
            problems.append(f"public endpoint {pe.id} has no gateway task implementing it")

    problems += _check_brief(plan, ctx)
    for t in plan.tasks:
        if not t.changes:
            problems.append(f"task {t.repo} has no `changes` (file, new/edit, functions, what)")
    if not plan.flow.strip():
        problems.append("`flow` is empty: one line on how the request travels through the repos")

    risks = _norm(" ".join(plan.risks))
    for repo in ctx.repos:
        if repo not in by_repo and _norm(repo) not in risks:
            problems.append(f"repo {repo!r} has no task and no risk explaining why")
    return problems


_TABLE_WORD = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+){2,}\b")
# a column, not a table: `ar_ageing_deduction_config_id`, `…_created_at`
_COLUMN_END = ("id", "ids", "at", "by", "code", "codes", "name", "type", "key", "date", "status")


def check_spec_tables(plan: ImplementationPlan, spec_md: str) -> list[str]:
    """D170: a plan must not invent tables next to the ones the spec defines. A name the plan
    uses (steps, summaries, what new files do) that shares its prefix with a table the spec
    defines (`ar_ageing_deduction_…`) but appears nowhere in the spec is an invented table:
    DEMO-10 planned `ar_ageing_deduction_channels` / `…_groups` instead of the spec's one
    `ar_ageing_deduction_rules`. Silent when the spec defines no tables."""
    defined = table_definitions(spec_md)
    if not defined:
        return []
    spec_words = set(re.findall(r"[a-z0-9_]+", spec_md.replace("\\_", "_").lower()))
    prefixes = {tuple(n.split("_")[:-1]) for n in defined if n.count("_") >= 2}
    texts = [t.summary for t in plan.tasks] + [st for t in plan.tasks for st in t.steps]
    texts += [c.what for t in plan.tasks for c in t.changes if c.change == "new"]
    invented: list[str] = []
    for word in _TABLE_WORD.findall(" ".join(texts)):
        parts = tuple(word.split("_"))
        if (word in defined or word in spec_words or parts[-1] in _COLUMN_END
                or word in invented):  # fmt: skip
            continue
        if any(len(parts) > len(p) and parts[: len(p)] == p for p in prefixes):
            invented.append(word)
    if not invented:
        return []
    family = sorted({n for n in defined for w in invented
                     if any(w.startswith("_".join(p) + "_") and n.startswith("_".join(p) + "_")
                            for p in prefixes)})  # fmt: skip
    where = ", ".join(f"{n} (§{defined[n].number})" if defined[n].number else n for n in family)
    return [
        f"the plan uses table(s) {', '.join(invented)} that the spec does not define. For this "
        f"area the spec defines {where}: read their CREATE TABLE in .agent/spec.md and plan "
        "exactly those tables and columns, or ask in open_questions why they don't fit"
    ]


def _check_brief(plan: ImplementationPlan, ctx: TicketContext) -> list[str]:
    """The brief lists every AC the ticket numbers; every brief AC is in some task; tasks
    refer only to ACs that exist (D128)."""
    problems: list[str] = []
    ids = [ac_id(c.id) or c.id for c in plan.brief.criteria]
    for i in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"brief: AC id {i} is used more than once")
    if not ids and (ctx.acceptance_criteria or ticket_ac_ids(ctx.description_md)):
        problems.append("brief.criteria is empty: list the ticket's acceptance criteria")
    own = ticket_ac_split(ctx.description_md)[0]  # the ticket's own words, copied in later
    blank = [i for c, i in zip(plan.brief.criteria, ids, strict=True)
             if not c.text.strip() and i not in own]  # fmt: skip
    if blank:
        problems.append(f"brief: {', '.join(blank)} has no text; the ticket does not number it, "
                        "so write the criterion yourself")  # fmt: skip
    bad = [c.id for c in plan.brief.criteria if ac_id(c.id) is None]
    if bad:
        problems.append(f"brief: ids must be AC1, AC2 …, not {', '.join(map(repr, bad))}")
    numbered = ticket_ac_ids(ctx.description_md)
    missing = [i for i in numbered if i not in ids]
    if missing:
        problems.append(
            f"brief.criteria misses {', '.join(missing)} from the ticket (read the whole "
            "ticket, including every AC under its sub-headings)"
        )
    if not numbered:  # bullets / numbered list / prose: the parser's count is a lower bound
        pre = [_norm(p) for p in plan.brief.preconditions]
        parsed = [
            a for a in ctx.acceptance_criteria
            if not any(_norm(a)[:60] in p or p[:60] in _norm(a) for p in pre if p)
        ]  # fmt: skip
        if len(ids) < len(parsed):
            problems.append(f"brief.criteria has {len(ids)} criteria but the ticket lists "
                            f"{len(parsed)}: include every one (preconditions go in "
                            "brief.preconditions)")  # fmt: skip
    used: set[str] = set()
    for t in plan.tasks:
        for entry in t.acceptance_criteria:
            i = ac_id(entry)
            if i is None:
                continue
            if i not in ids:
                problems.append(f"task {t.repo} names {i}, which is not in brief.criteria")
            used.add(i)
    texts = {ac_id(c.id) or c.id: c.text for c in plan.brief.criteria}
    texts.update(ticket_ac_split(ctx.description_md)[0])
    for i in ids:
        if i not in used:
            what = " ".join(texts.get(i, "").split())[:140]
            problems.append(f'{i} is in no task\'s acceptance_criteria: add "{i}" to the '
                            f"acceptance_criteria of the task whose repo owns it"
                            + (f" ({what})" if what else ""))  # fmt: skip
    return problems


# ---------- outputs (3.3) ----------


def task_criteria(task: RepoTask, plan: ImplementationPlan,
                  ticket_texts: dict[str, str] | None = None) -> list[tuple[str, str]]:  # fmt: skip
    """The task's acceptance criteria as (id, text): brief ids ("AC4") keep theirs, the task's
    own criteria are numbered T1, T2 … (P9: the reviewer answers each id, task.md shows them).
    The ticket's own wording wins over the planner's one-line version when there is one."""
    texts = {ac_id(c.id) or c.id: c.text for c in plan.brief.criteria}
    texts.update(ticket_texts or {})
    out: list[tuple[str, str]] = []
    own = 0
    for a in task.acceptance_criteria:
        i = ac_id(a)
        if i is None:
            own += 1
            out.append((f"T{own}", a))
        else:
            out.append((i, texts.get(i, "")))
    return out


def render_task_md(task: RepoTask, plan: ImplementationPlan, key: str,
                   ticket_texts: dict[str, str] | None = None) -> str:  # fmt: skip
    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {i}" for i in items) or "- (none)"

    steps = "\n".join(f"{n}. {s}" for n, s in enumerate(task.steps, 1)) or "(none)"
    notes = plan.contract.notes
    acs = [(f"{i}: " + text.replace("\n", "\n  ")) if text else i
           for i, text in task_criteria(task, plan, ticket_texts)]  # fmt: skip
    pre = plan.brief.preconditions
    files = [change_line(c) for c in task.changes]
    listed = {c.file for c in task.changes}
    files += [f"`{f}`" for f in task.files_hint if f not in listed]
    return (
        f"# {key} — task for {task.repo} ({task.role}, order {task.order})\n\n"
        f"{task.summary}\n\n## Steps\n\n{steps}\n\n"
        f"## Acceptance criteria\n\n{bullets(acs)}\n\n"
        + (f"Preconditions for all of them:\n\n{bullets(pre)}\n\n" if pre else "")
        + f"## Files\n\n{bullets(files)}\n\n"
        f"## Contract\n\nImplements: {', '.join(task.implements) or '—'}  \n"
        f"Consumes: {', '.join(task.consumes) or '—'}  \n"
        "This repo's part of the contract: `.agent/contract.json` — follow it exactly; report "
        "deviations.\n" + (f"\n## Contract notes\n\n{bullets(notes)}\n" if notes else "")
    )


def render_ticket_plan_md(plan: ImplementationPlan, key: str, repos: list[str] | None = None,
                          ticket_texts: dict[str, str] | None = None) -> str:  # fmt: skip
    """The plan for a ticket-level coder or reviewer (single_session): the flow, the shared
    preconditions and contract notes once, then each repo's task (steps, criteria in the
    ticket's words, files, endpoints), in coding order. `repos`: only these tasks."""

    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {i}" for i in items) or "- (none)"

    out = [f"# {key} — the plan"]
    if plan.flow.strip():
        out.append(f"How a request travels: {plan.flow.strip()}")
    if plan.brief.preconditions:
        out.append("Preconditions for every criterion:\n\n" + bullets(plan.brief.preconditions))
    for task in sorted(plan.tasks, key=lambda t: t.order):
        if repos is not None and task.repo not in repos:
            continue
        steps = "\n".join(f"{n}. {s}" for n, s in enumerate(task.steps, 1)) or "(none)"
        acs = [(f"{i}: " + text.replace("\n", "\n  ")) if text else i
               for i, text in task_criteria(task, plan, ticket_texts)]  # fmt: skip
        files = [change_line(c) for c in task.changes]
        listed = {c.file for c in task.changes}
        files += [f"`{f}`" for f in task.files_hint if f not in listed]
        out.append(
            f"## {task.order}. `{task.repo}` ({task.role})\n\n{task.summary}\n\n"
            f"Steps:\n\n{steps}\n\nAcceptance criteria:\n\n{bullets(acs)}\n\n"
            f"Files (paths inside `{task.repo}/`):\n\n{bullets(files)}\n\n"
            f"Implements: {', '.join(task.implements) or '—'}. "
            f"Consumes: {', '.join(task.consumes) or '—'}."
        )
    if plan.contract.notes:
        out.append("Notes:\n\n" + bullets(plan.contract.notes))
    return "\n\n".join(out) + "\n"


def write_plan_files(ctx: TicketContext, plan: ImplementationPlan, deps: Deps) -> None:
    ws = deps.workspaces
    assert ws is not None
    agent = ws.ticket_dir(ctx.key) / AGENT_DIR
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "plan.json").write_text(plan.model_dump_json(indent=2) + "\n")
    (agent / "contract.json").write_text(plan.contract.model_dump_json(indent=2) + "\n")
    ticket_texts, _ = ticket_ac_split(ctx.description_md)
    if deps.config.agents.single_session:
        return  # one coder / reviewer session reads plan.json and contract.json in this folder
    for task in plan.tasks:  # each repo: the ticket's own criteria, its part of the contract
        task_md = render_task_md(task, plan, ctx.key, ticket_texts)
        ws.write_repo_file(ctx.key, task.repo, "task.md", task_md)
        own = repo_contract(plan, task).model_dump_json(indent=2) + "\n"
        ws.write_repo_file(ctx.key, task.repo, "contract.json", own)


# ---------- rendering (3.4) ----------


def _cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_plan_md(plan: ImplementationPlan) -> str:
    out: list[str] = []
    status = "ready" if plan.status == "ready" else "needs answers before it can be finished"
    out.append(f"## Implementation plan ({plan.mode}): {status}")
    if plan.open_questions:
        out += ["", "### Open questions", ""] + [f"- {q}" for q in plan.open_questions]
    if plan.changes_from_previous:
        out += ["", "### Changes from previous plan", ""]
        out += [f"- {c}" for c in plan.changes_from_previous]
    for t in sorted(plan.tasks, key=lambda t: t.order):
        out += ["", f"### {t.order}. {t.repo} ({t.role})", "", t.summary, ""]
        out += [f"{n}. {s}" for n, s in enumerate(t.steps, 1)]
        if t.acceptance_criteria:
            out += ["", "**Acceptance criteria**", ""]
            out += [f"- {a}" for a in t.acceptance_criteria]
        refs = []
        if t.implements:
            refs.append(f"implements {', '.join(t.implements)}")
        if t.consumes:
            refs.append(f"consumes {', '.join(t.consumes)}")
        if refs:
            out += ["", f"_Contract: {'; '.join(refs)}_"]
    c = plan.contract
    if c.internal_endpoints:
        out += ["", "### Internal endpoints", "",
                "| Id | Method | Path | Provider → consumers | New | Errors |",
                "|---|---|---|---|---|---|"]  # fmt: skip
        for e in c.internal_endpoints:
            errors = ", ".join(f"{x.status} {x.code}" for x in e.errors) or "—"
            out.append(
                f"| {_cell(e.id)} | {e.method} | {_cell(e.path)} | "
                f"{_cell(e.provider_repo)} → {_cell(', '.join(e.consumer_repos) or '—')} | "
                f"{'yes' if e.is_new else 'no'} | {_cell(errors)} |"
            )
    if c.public_endpoints:
        out += ["", "### Public endpoints", "",
                "| Id | Method | Path | Auth | Calls | Error mapping |",
                "|---|---|---|---|---|---|"]  # fmt: skip
        for pe in c.public_endpoints:
            mapping = ", ".join(_mapping(m) for m in pe.error_mapping) or "—"
            out.append(
                f"| {_cell(pe.id)} | {pe.method} | {_cell(pe.path)} | {_cell(pe.auth)} | "
                f"{_cell(', '.join(pe.calls) or '—')} | {_cell(mapping)} |"
            )
    if c.notes:
        out += ["", "### Contract notes", ""] + [f"- {n}" for n in c.notes]
    if plan.external_calls:  # P10: live tests need an answer for each
        out += ["", "### External calls", ""] + [
            f"- {x.repo} → {x.service}: {x.method} {x.path} (response from: {x.response_from})"
            for x in plan.external_calls]  # fmt: skip
    if plan.risks:
        out += ["", "### Risks", ""] + [f"- {r}" for r in plan.risks]
    return "\n".join(out) + "\n"


_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z(`\"'])")


def _plain(text: str) -> str:
    """`text` on one line, whole: nothing people read is ever cut (user, 2026-10-19)."""
    return " ".join(str(text).split())


def render_endpoint_table_md(plan: ImplementationPlan) -> str:
    """Compact contract table (id, method, path, internal new/existing or public); '' if none."""
    c = plan.contract
    if not (c.internal_endpoints or c.public_endpoints):
        return ""
    out = ["| Endpoint | Method | Path | Kind |", "|---|---|---|---|"]
    for e in c.internal_endpoints:
        kind = "internal, new" if e.is_new else "internal, existing"
        out.append(f"| {_cell(e.id)} | {e.method} | {_cell(e.path)} | {kind} |")
    for pe in c.public_endpoints:
        out.append(f"| {_cell(pe.id)} | {pe.method} | {_cell(pe.path)} | public |")
    return "\n".join(out)


def render_top_risks_md(plan: ImplementationPlan) -> str:
    if not plan.risks:
        return ""
    return "\n".join(["**Risks:**"] + [f"- {_plain(r)}" for r in plan.risks])


def change_line(c: FileChange) -> str:
    """ "`src/x.js` (new): `getX()` — reads …" for the plan comment and task.md."""
    fns = ", ".join(f"`{f}`" if f.endswith(")") else f"`{f}()`" for f in c.functions)
    return f"`{c.file}` ({c.change})" + (f": {fns}" if fns else "") + f" — {c.what}"


def render_plan_summary_md(plan: ImplementationPlan) -> str:
    """The short plan people read (Jira comment, D128): open questions first, how it works in
    one line, then per repo the files, their functions and what each does. No risks, endpoint
    table, ACs or steps: those stay in plan.json, task.md and the MRs. A plan that needs
    clarification shows the questions only."""
    kind = {"initial": "The plan", "revise": "The updated plan",
            "replan": "The updated plan", "qa_fix": "The plan for the QA fixes"}.get(
        plan.mode, "The plan")  # fmt: skip
    out = [f"**{kind}**" if plan.status == "ready" else "**Questions before I can plan**"]
    if plan.open_questions:
        out += ["", "**Please answer:**"]
        out += [f"{n}. {q}" for n, q in enumerate(plan.open_questions, 1)]
    if plan.status != "ready":  # unsure: only the questions; a half-made plan would mislead
        return "\n".join(out) + "\n"
    if plan.mode != "initial" and plan.changes_from_previous:
        out += ["", "**What changed:**"]
        out += [f"- {_plain(c)}" for c in plan.changes_from_previous]
    if plan.flow.strip():
        out += ["", f"**How it works:** {plan.flow.strip()}"]
    for t in sorted(plan.tasks, key=lambda t: t.order):
        out += ["", f"**{t.order}. {t.repo}** ({t.role})"]
        code = [c for c in t.changes if not _TEST_FILE.search(c.file)]
        tests = len(t.changes) - len(code)
        if code:
            out += [f"- {short_change_line(c)}" for c in code]
        else:
            out.append(f"- {_plain(t.summary)}")
        if tests:  # the coder still writes them (task.md lists them); people needn't read them
            out.append(f"- Tests: {tests} file{'s' if tests != 1 else ''}")
    return "\n".join(out) + "\n"


# test files: counted, not listed, in the plan people read (user, 2026-10-04: too much detail)
_TEST_FILE = re.compile(r"(^|/)(test|tests|__tests__|spec)/|\.(spec|test)\.[cm]?[jt]sx?$", re.I)


def short_change_line(c: FileChange) -> str:
    """ "`src/x.js` (new) — registers POST /x with validation": what the file does, whole, no
    function list."""
    return f"`{c.file}` ({c.change}) — {_plain(c.what).rstrip('.')}"


def _mapping(m: dict) -> str:
    src = " ".join(str(m.get(k)) for k in ("internal_status", "internal_code") if m.get(k))
    dst = " ".join(str(m.get(k)) for k in ("public_status", "public_code") if m.get(k))
    return f"{src or 'gateway'} → {dst}" if dst else json.dumps(m)


# ---------- entry point (3.1) ----------


def plan(
    ctx: TicketContext,
    mode: str,
    deps: Deps,
    extra: dict | None = None,
    provider_override: str | None = None,
    slot: str | None = None,
) -> tuple[ImplementationPlan | None, AgentRun]:
    """Run the planner; validate; one retry with the list of problems; write plan files."""
    if mode not in MODES:
        raise ValueError(f"unknown planner mode {mode!r}; valid: {MODES}")
    log = get_logger(ticket=ctx.key, node="plan")
    cwd, repos, repo_map_md = write_inputs(ctx, mode, deps, extra)
    guided = bool((extra or {}).get("guidance"))
    single = deps.config.agents.single_session
    prompt = planner_prompt(ctx, mode, guided, cwd, repos, repo_map_md, single)
    # P9 (time): a ticket keeps one planner session: answers to its questions, feedback, change
    # requests, replans and QA fixes continue it with only what's new (the runner falls back to
    # the full prompt when there is no earlier session to continue)
    follow_up = continuation_prompt(ctx, mode, guided, cwd, single)

    def complete(result: ImplementationPlan) -> ImplementationPlan:
        result = result.model_copy(update={"mode": mode})
        # a QA round plans only the bugs' repos, and questions make no plan: nothing to complete
        if mode == "qa_fix" or result.status != "ready":
            return result
        return complete_brief(result, ctx)

    def last_chance(result: ImplementationPlan) -> ImplementationPlan:
        result = complete(result)
        return result if mode == "qa_fix" or result.status != "ready" \
            else assign_uncovered(result, ctx)  # fmt: skip

    def check(result: ImplementationPlan) -> list[str]:
        problems = validate_plan(complete(result), ctx, deps, mode)
        if problems:
            log.warning("plan_invalid", problems=problems)
        return problems

    # Problems go back to the same session, which fixes plan.json in place (D128).
    result, run = deps.runner.run(
        ctx.key, "planner", cwd, prompt, ImplementationPlan,
        provider_override=provider_override, slot=slot, check=check, check_error="invalid_plan",
        continue_session=True, follow_up=follow_up, edit_in_place=mode != "initial",
        repair=last_chance,
    )  # fmt: skip
    if result is None:
        return None, run
    result = complete(result)
    write_plan_files(ctx, result, deps)
    log.info("plan_ready", status=result.status, tasks=len(result.tasks))
    return result, run
