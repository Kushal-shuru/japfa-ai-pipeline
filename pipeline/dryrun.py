"""`pipeline run <KEY> --dry-run`: the whole flow (Phases 3–7) on fakes, in seconds, for free.

Fake Jira / GitLab / spec, scripted planner, coders and reviewers (FakeProvider: round 1 raises
findings on the gateway, one of them from the contract check against the service; round 2
approves; the fix coder answers whatever is in `.agent/findings.json`), local bare git remotes in a
temp dir, the real Verifier with a no-op command runner, console chat, in-memory checkpoints.
Checks that every step and branch connects; real agents are tested via `serve` + `say`
(narrows D50 for `run --dry-run` only).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from pipeline.acceptance import criterion_ref
from pipeline.agents.pool import AgentPool
from pipeline.agents.providers.fake_provider import FakeProvider
from pipeline.agents.runner import AgentRunner
from pipeline.chat.console_chat import ConsoleChat
from pipeline.config import PipelineConfig, Secrets, ensure_home
from pipeline.deps import Deps
from pipeline.integrations.fakes import FakeGitLab, FakeJira, FakeSpec
from pipeline.server import Services, build_services
from pipeline.store import Store
from pipeline.verify import Verifier
from pipeline.workspace import Workspaces, slugify

SPEC_URL = "https://docs.google.com/document/d/dry-run-spec-document/edit"
ACS = ["GET the dry-run endpoint returns 200 with {ok: true}", "Unknown id returns 404 NOT_FOUND"]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


_TASK_CRITERION = re.compile(r"^- ((?:AC|T)\d+)\b", re.M)


def complete_review(result: dict, cwd: Path) -> dict:
    """What a scripted review must still answer (P9), filled in the way a satisfied reviewer
    would: every criterion id in `.agent/task.md` it didn't answer → met; every finding in
    `.agent/previous_review.json` → still open if listed again, else fixed. A script that sets
    `previous` itself keeps it as it is."""
    data = json.loads(json.dumps(result))
    agent = cwd / ".agent"
    task = agent / "task.md"
    ids = _TASK_CRITERION.findall(task.read_text()) if task.is_file() else []
    checks = list(data.get("acceptance_criteria_check") or [])
    answered = {criterion_ref(str(c.get("criterion") or "")) for c in checks}
    checks += [{"criterion": i, "status": "met", "evidence": "scripted review"}
               for i in ids if i not in answered]  # fmt: skip
    data["acceptance_criteria_check"] = checks
    if "previous" not in data:
        path = agent / "previous_review.json"
        earlier = json.loads(path.read_text()).get("findings", []) if path.is_file() else []
        listed = {f.get("id") for f in data.get("findings") or []}
        data["previous"] = [{"finding_id": f["id"],
                             "outcome": "still_open" if f["id"] in listed else "fixed"}
                            for f in earlier]  # fmt: skip
    return data


def make_remote(root: Path, name: str, branch: str) -> Path:
    """Bare repo with one commit on `branch` (the repo's base branch)."""
    bare = root / "remotes" / f"{name}.git"
    seed = root / "seed" / name
    bare.mkdir(parents=True)
    seed.mkdir(parents=True)
    _git(bare, "init", "--bare", "-b", branch)
    _git(seed, "init", "-b", branch)
    (seed / "README.md").write_text(f"# {name} (dry run)\n")
    (seed / ".gitignore").write_text(".agent/\nnode_modules/\n")
    _git(seed, "add", "-A")
    _git(seed, "-c", "user.name=dry", "-c", "user.email=dry@run", "commit", "-m", "init")
    _git(seed, "remote", "add", "origin", str(bare))
    _git(seed, "push", "origin", branch)
    return bare


def demo_plan(key: str, config: PipelineConfig, repos: list[str]) -> dict:
    """A valid ImplementationPlan for any mix of service / gateway / frontend repos."""
    path = f"/api/dry-run/v1/{key.lower()}/:id"
    services = [r for r in repos if config.repo(r).role == "service"]
    gateway = next((r for r in repos if config.repo(r).role == "gateway"), None)
    internal = [
        {"id": f"{slugify(s)}.dry_run", "provider_repo": s,
         "consumer_repos": [gateway] if gateway else [], "method": "GET", "path": path,
         "description": "dry-run endpoint", "request_schema": {"params": {"id": "string"}},
         "response_schema": {"ok": "boolean"},
         "errors": [{"status": 404, "code": "NOT_FOUND", "when": "unknown id"}],
         "auth": "X-Client-Id / X-Client-Secret", "is_new": True}
        for s in services
    ]  # fmt: skip
    public = []
    if gateway:
        public.append(
            {"id": "gw.dry_run", "method": "GET", "path": f"/dashboard{path}",
             "auth": "logged-in user", "validation": {"params": {"id": "string"}},
             "calls": [e["id"] for e in internal],
             "error_mapping": [{"internal_status": 404, "internal_code": "NOT_FOUND",
                                "public_status": 597, "public_code": "NOT_FOUND"}],
             "response_schema": {"ok": "boolean"}}
        )  # fmt: skip
    tasks = []
    for n, repo in enumerate(config.repo_order(repos), 1):
        role = config.repo(repo).role
        tasks.append({
            "repo": repo, "role": role, "order": n, "summary": f"dry run: {role} part",
            "steps": ["write the dry-run file"], "files_hint": [],
            "changes": [{"file": f"src/dry-run-{role}.js", "change": "new",
                         "functions": ["dryRun"], "what": f"the {role} part of the dry run"}],
            "acceptance_criteria": ["AC1", "AC2"],
            "implements": [e["id"] for e in internal if e["provider_repo"] == repo]
            + (["gw.dry_run"] if repo == gateway else []),
            "consumes": [e["id"] for e in internal] if repo == gateway
            else (["gw.dry_run"] if role == "frontend" and gateway else []),
        })  # fmt: skip
    brief = {"criteria": [{"id": f"AC{n}", "text": ac} for n, ac in enumerate(ACS, 1)]}
    return {"status": "ready", "mode": "initial", "brief": brief, "tasks": tasks,
            "flow": f"client → gateway GET /dashboard{path} → service GET {path}",
            "contract": {"internal_endpoints": internal, "public_endpoints": public},
            "risks": ["dry run"]}  # fmt: skip


def _coder_script(fake: FakeProvider, key: str, repo: str, config: PipelineConfig) -> None:
    """A route per repo; the service also adds a migration and the gateway reads a new
    environment variable, so the deployment note has DevOps work to describe (P6)."""
    rel = f"src/dry-run-{key.lower()}.js"
    content = f"// dry run for {key}\nrouter.get('/api/dry-run/v1/{key.lower()}/:id')\n"
    files = {rel: content}
    cfg = config.repo(repo)
    if cfg.role == "service":
        folder = (cfg.paths or {}).get("migrations", "src/migrations")
        files[f"{folder}/20260928000000-dry-run-{key.lower()}.js"] = (
            "module.exports = { up: async () => {}, down: async () => {} };\n")  # fmt: skip
    elif cfg.role == "gateway":
        files[rel] = content + "const base = process.env.DRY_RUN_SERVICE_URL;\n"
    # the commit plan (D169): one small commit per piece of work, the route first
    plan = [{"message": f"feat({key}): add the dry-run route", "files": [rel]}]
    plan += [{"message": f"feat({key}): add the dry-run migration", "files": [f]}
             for f in sorted(files) if f != rel]  # fmt: skip
    files[".agent/commits.json"] = json.dumps(plan, indent=2) + "\n"
    fake.add(
        "coder",
        {"repo": repo, "status": "success", "summary": f"dry-run change in {repo}",
         "files_changed": sorted(f for f in files if not f.startswith(".agent/")),
         "tests": {"lint": "passed"}},
        repo=repo, files=files,
    )  # fmt: skip


def scripted_qa_notes(cwd: Path, _prompt: str) -> dict:
    """QA notes for the dry run (and the graph tests): three test cases per changed route
    (`.agent/routes.json`), placeholders only."""
    path = cwd / ".agent" / "routes.json"
    routes = json.loads(path.read_text()) if path.exists() else []
    routes = routes or [{"method": "GET", "path": "/api/dry-run/v1/items/:id", "validator": None}]
    out = ["Dry-run test notes: what changed, and how to check it. You need `{{QA_BASE_URL}}` and "
           "a `{{QA_TOKEN}}` for a test user."]  # fmt: skip
    n = 0
    for r in routes:
        out.append(f"### {r['method']} {r['path']}")
        url = "{{QA_BASE_URL}}" + r["path"].replace(":id", "<id>")
        for what, status in (("happy path", "200"), ("invalid id", "400"),
                             ("unknown id", "597")):  # fmt: skip
            n += 1
            out.append(f"#### Test {n}: {what}\n```bash\ncurl -X {r['method']} \"{url}\" "
                       f"-H \"Authorization: Bearer {{{{QA_TOKEN}}}}\"\n```\n"
                       f"**Expected:** `{status}`.")  # fmt: skip
    return {"comment_markdown": "\n\n".join(out)}


def scripted_deploy_note(cwd: Path, _prompt: str) -> dict:
    """The Deployment Agent of the dry run and the graph tests: a short note that names every
    fact in `.agent/devops_facts.json`, in the default order."""
    agent = cwd / ".agent"
    repos = json.loads((agent / "deploy_repos.json").read_text())
    facts = {f["repo"]: f for f in json.loads((agent / "devops_facts.json").read_text())}
    key = cwd.name
    lines = [f"# Deployment note — {key}", "", "## 0. Change set and order", ""]
    lines += [f"{r['default_order']}. {r['name']} ({r['role']})" for r in repos]
    for r in repos:
        f = facts.get(r["name"], {})
        env = f.get("new_env_vars", []) + f.get("undeclared_env_vars", []) + \
            f.get("build_time_env_vars", [])  # fmt: skip
        lines += ["", f"## {r['name']} ({r['role']})", "", "### 1. Environment variables",
                  ", ".join(f"`{v}`" for v in env) or "None.", "", "### 2. Database migrations",
                  ", ".join(f"`{m}`" for m in f.get("migration_files", [])) or "None.", "",
                  "### 3. Infrastructure",
                  ", ".join(f.get("infra_files_changed", []) + f.get("new_dependencies", []))
                  or "None."]  # fmt: skip
        for m in reversed(f.get("migration_files", [])):  # Rollback only with migrations (D123)
            name = m.rsplit("/", 1)[-1]
            undo = f"`npx sequelize-cli db:migrate:undo --name {name}` (runs its `down`)."
            lines += ["", "### 4. Rollback", undo]
    doc = "\n".join(lines) + "\n"
    risky = any(f.get("migration_files") for f in facts.values())
    return {"doc_markdown": doc, "deploy_order": [r["name"] for r in repos],
            "risk_level": "medium" if risky else "low", "needs_devops": False}  # fmt: skip


class AnyDocSpec(FakeSpec):
    """The dry run's spec: any link `implement` is given reads as the same small document."""

    def fetch(self, url: str) -> tuple[str, str]:
        return super().fetch(SPEC_URL if "docs.google.com/document/d/" in url else url)


def _dry_file(key: str) -> str:
    return f"src/dry-run-{key.lower()}.js"


def _review_scripts(fake: FakeProvider, key: str, config: PipelineConfig, repos: list[str]) -> None:
    """Round 1: a major + a minor finding on the gateway (the service approves) and one contract
    finding the service must change; every later round approves."""
    ordered = config.repo_order(repos)
    gateway = next((r for r in ordered if config.repo(r).role == "gateway"), ordered[-1])
    service = next((r for r in ordered if config.repo(r).role == "service"), ordered[0])
    rel = _dry_file(key)
    for n in range(config.limits.max_review_rounds):
        for repo in ordered:
            findings = []
            if n == 0 and repo == gateway:
                findings = [
                    {"id": f"R1-{repo}-F1", "repo": repo, "severity": "major",
                     "category": "validation", "file": rel, "line": 2,
                     "comment": "The route doesn't validate `id` before calling the service.\n\n"
                                "Why it matters: a malformed id reaches the service and comes "
                                "back as a 500 instead of a 400.",
                     "suggestion": "Validate `id` with `isValidUUID` first."},
                    {"id": f"R1-{repo}-F2", "repo": repo, "severity": "minor",
                     "category": "logging", "file": rel, "line": 1,
                     "comment": "The comment says 'dry run' but doesn't name the ticket's "
                                "feature.\n\nWhy it matters: it helps the next reader.",
                     "suggestion": None},
                ]  # fmt: skip
            # the criteria and earlier findings are answered by `complete_review` (P9)
            fake.add("reviewer", {
                "scope": repo, "verdict": "changes_requested" if findings else "approve",
                "summary": f"Dry-run review of {repo}.", "findings": findings,
            }, repo=repo)  # fmt: skip
        if len(ordered) > 1:
            contract = [] if n else [
                {"id": "R1-X-F1", "repo": service, "severity": "major",
                 "category": "error-mapping", "file": rel, "line": 2,
                 "comment": "The service answers an unknown id with 400; the contract says 404 "
                            "NOT_FOUND.\n\nWhy it matters: the gateway maps only 404 to its "
                            "public 597.",
                 "suggestion": "Return 404 NOT_FOUND for an unknown id."},
            ]  # fmt: skip
            fake.add("contract_check", {
                "scope": "contract", "verdict": "changes_requested" if contract else "approve",
                "summary": "Dry-run contract check.", "findings": contract,
            })  # fmt: skip


def _fix_scripts(fake: FakeProvider, key: str, config: PipelineConfig, repos: list[str]) -> None:
    """The fix coder: fixes every finding in `.agent/findings.json`."""

    def answer(cwd: Path, _prompt: str) -> dict:
        path = cwd / ".agent" / "findings.json"
        items = json.loads(path.read_text()) if path.exists() else []
        return {"repo": cwd.name, "status": "success",
                "summary": f"Fixed {len(items)} finding(s) in the dry run.",
                "files_changed": [_dry_file(key)], "tests": {"lint": "passed"},
                "resolutions": [{"finding_id": i["id"], "action": "fixed",
                                 "note": "done in the dry run"} for i in items]}  # fmt: skip

    for n in range(config.limits.max_review_rounds):
        for repo in config.repo_order(repos):
            fake.add(
                "coder_fix",
                answer,
                repo=repo,
                files={
                    _dry_file(key): f"// dry run for {key}, fix round {n + 1}\n"
                    f"router.get('/api/dry-run/v1/{key.lower()}/:id', validateId)\n"
                    + (
                        "const base = process.env.DRY_RUN_SERVICE_URL;\n"
                        if config.repo(repo).role == "gateway"
                        else ""
                    )
                },
            )


def _qa_round_defaults(fake: FakeProvider, key: str) -> None:
    """P7: once the queued runs are used up (a QA round), coders fix the bugs and reviewers
    approve."""

    rel = f"src/dry-run-{key.lower()}-qa.js"

    def coder(cwd: Path, _prompt: str) -> dict:
        return {"repo": cwd.name, "status": "success", "summary": "Fixed the QA bugs (dry run).",
                "files_changed": [rel], "tests": {"lint": "passed"}}  # fmt: skip

    def review(cwd: Path, _prompt: str) -> dict:
        return {"scope": cwd.name, "verdict": "approve", "summary": "Dry-run review: fine.",
                "findings": [], "acceptance_criteria_check": []}  # fmt: skip

    fake.set_default("coder", coder, files={rel: "// QA fixes (dry run)\n"})
    fake.set_default("reviewer", review)
    fake.set_default("contract_check", {"scope": "contract", "verdict": "approve",
                                        "summary": "Dry-run contract check: fine.",
                                        "findings": []})  # fmt: skip


DRY_RUN_BUGS = (  # bug ticket → (summary, description): QA files each bug as its own ticket
    ("Unknown id returns 500", "An unknown id returns 500 instead of 404."),
    ("Error says undefined", "The error message says 'undefined'."),
)


class DryRunMerger:
    """Stands in for the people who merge (dry run only): each tick merges the next approved MR
    of every ticket waiting for its merges, in merge order, in the fake GitLab."""

    def __init__(self, deps: Any, runtime: Any) -> None:
        self.deps = deps
        self.runtime = runtime

    def tick(self) -> list[tuple[str, str]]:
        from pipeline.graph.nodes.common import ordered

        merged = []
        for row in self.deps.store.list_tickets():
            if row.get("waiting_for") != "merge_all":
                continue
            info = self.runtime.snapshot(row["key"])
            if info.waiting != "merge_all" or info.busy:
                continue
            for repo in ordered(info.values):
                work = info.values["repos"][repo]
                project = self.deps.config.repo(repo).gitlab_project
                if self.deps.gitlab.mr_state(project, work["mr_iid"]) == "opened":
                    self.deps.gitlab.set_state(project, work["mr_iid"], "merged")
                    merged.append((row["key"], repo))
                    break
        return merged


def make_dry_run(key: str, config: PipelineConfig, secrets: Secrets, root: Path,
                 sync: bool = True) -> Services:  # fmt: skip
    """All-fake services for `key`. `sync=False` (`pipeline serve --dry-run`): worker threads
    for `pipeline cli --dry-run`."""
    # the scripted coders and reviewers are per repo: the dry run exercises that path
    config = config.model_copy(update={
        "agents": config.agents.model_copy(update={"single_session": False}),
        "chat_llm": config.chat_llm.model_copy(update={"provider": "none"}),  # templates
    })
    home = ensure_home(root / "home")
    secrets = secrets.model_copy(update={"pipeline_home": home})
    store = Store("sqlite://")
    store.init()
    remotes = {r.name: make_remote(root, r.name, r.default_branch) for r in config.repos}
    ws = Workspaces(home, config, remote_url=lambda r: str(remotes[r.name]))

    repos = config.repo_names
    jira = FakeJira(store)
    description = "Dry run of the pipeline.\n\n## Acceptance criteria\n\n" + "\n".join(
        f"- {ac}" for ac in ACS
    )
    jira.add_issue(key, f"Dry run {key}", description, repos=repos, spec=SPEC_URL)

    fake = FakeProvider("fake")
    fake.finish.update(reviewer=complete_review, contract_check=complete_review)
    for _ in range(config.limits.max_plan_revisions + 1):
        fake.add("planner", demo_plan(key, config, repos))
    for repo in config.repo_order(repos):
        _coder_script(fake, key, repo, config)
    fake.set_default("deployment", scripted_deploy_note)
    fake.set_default("qa_notes", scripted_qa_notes)
    _qa_round_defaults(fake, key)
    _review_scripts(fake, key, config, repos)
    _fix_scripts(fake, key, config, repos)
    providers: dict[str, Any] = {"claude": fake, "codex": fake}
    runner = AgentRunner(config, secrets, store, home, providers=providers)

    def no_op(cmd: str, cwd: Path, timeout_s: float) -> tuple[int, str]:
        return 0, f"dry run: {cmd}"

    deps = Deps(
        jira=jira, gitlab=FakeGitLab(), spec=AnyDocSpec({SPEC_URL: "## Dry run\n\nspec"}),
        store=store, config=config, secrets=secrets, runner=runner,
        pool=AgentPool(config, store),
        chat=ConsoleChat(home / "logs" / "chat.log", store=store) if sync else None,
        workspaces=ws, verifier=Verifier(config, ws, run_cmd=no_op), dry_run=True,
    )  # fmt: skip
    return build_services(deps, sync=sync, checkpoint_path=None)


def run_dry(services: Services, key: str) -> dict[str, Any]:
    """implement → approve the plan → review → fix all → review again → deployment note →
    merge one MR at a time → RFT → QA writes two bugs on Jira + Blocked → `fix all` → the same
    loop (QA round 1) → RFT → QA Passed → finished, or until it needs a human."""
    from pipeline.chat.commands import parse_command

    def say(text: str) -> Any:
        cmd = parse_command(text, actor_id="dry-run", actor_name="dry run", source="cli")
        return services.bus.handle(cmd)

    from pipeline.events import GitLabMergePoller, QaPoller

    say(f"implement {key}")
    answers = {"plan_approval": f"approve plan {key}", "review": f"review {key}",
               "fix": f"fix {key} all", "re_review": f"review {key}",
               "deploy_doc": f"deploy-doc {key}"}  # fmt: skip
    merger = DryRunMerger(services.deps, services.runtime)
    poller = GitLabMergePoller(services.deps, services.runtime)
    qa = QaPoller(services.deps, services.runtime)
    jira = services.deps.jira
    statuses = services.deps.config.jira.statuses
    prefix = services.deps.config.jira.project
    bug_keys = [f"{prefix}-9{n}" for n in range(1, len(DRY_RUN_BUGS) + 1)]
    for _ in range(60):
        info = services.runtime.snapshot(key)
        if info.waiting == "qa" and not info.busy:  # testing: bugs once, then it passes
            if not info.values.get("qa_round"):
                for bug_key, (summary, text) in zip(bug_keys, DRY_RUN_BUGS, strict=True):
                    jira.add_issue(bug_key, summary, text, status="To Do")
                jira.transition(key, statuses.blocked)
                say(f"fix {key} {' '.join(bug_keys)}")
            else:
                jira.transition(key, statuses.qa_passed)
                if not qa.poll():
                    break
            continue
        if info.waiting == "merge_all":  # people merge one MR at a time (P6)
            if not merger.tick():
                break
            poller.poll()
            continue
        if info.waiting not in answers:
            break
        say(answers[info.waiting])
    info = services.runtime.snapshot(key)
    return {
        "finished": info.finished,
        "waiting": info.waiting,
        "halted_reason": info.values.get("halted_reason"),
        "mrs": services.deps.store.mrs_for(key),
        "jira_status": services.deps.jira.current_status(key),
    }
