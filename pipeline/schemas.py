"""All pipeline contracts. Mirrors CLAUDE.md §7 exactly; change both together (and log it)."""
# ruff: noqa: E501
# fmt: off

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

RepoRole = Literal["gateway", "service", "frontend"]
RepoKind = Literal["backend", "frontend"]
Provider = Literal["claude", "codex", "fake"]

class TicketContext(BaseModel):
    key: str
    summary: str
    description_md: str
    acceptance_criteria: list[str]
    repos: list[str]
    spec_urls: list[str]
    spec_md: str
    spec_hash: str
    epic_key: str | None = None

# ---------- planning ----------
class ErrorCase(BaseModel):
    status: int
    code: str
    when: str

class InternalEndpoint(BaseModel):       # exposed by a service, called by the gateway
    id: str                              # e.g. "orders.create"
    provider_repo: str
    consumer_repos: list[str]
    method: Literal["GET","POST","PUT","PATCH","DELETE"]
    path: str                            # e.g. /internal/v1/orders
    description: str
    request_schema: dict                 # JSON Schema: {params, query, body}
    response_schema: dict
    errors: list[ErrorCase] = []
    auth: str                            # e.g. "X-Internal-Token header"
    is_new: bool

class PublicEndpoint(BaseModel):         # exposed by the gateway
    id: str
    method: Literal["GET","POST","PUT","PATCH","DELETE"]
    path: str
    auth: str
    validation: dict                     # JSON Schema enforced by the gateway validator
    calls: list[str]                     # InternalEndpoint ids, in call order
    error_mapping: list[dict]            # {internal status/code → public status/code}
    response_schema: dict

class InterfaceContract(BaseModel):
    internal_endpoints: list[InternalEndpoint] = []
    public_endpoints: list[PublicEndpoint] = []
    notes: list[str] = []

class FileChange(BaseModel):            # one file a task adds or edits (the short plan comment, D128)
    file: str
    change: Literal["new", "edit"]
    functions: list[str] = []
    what: str                            # one line: what it does for the ticket

class RepoTask(BaseModel):
    repo: str
    role: RepoRole
    order: int
    summary: str
    steps: list[str]
    changes: list[FileChange] = []
    files_hint: list[str] = []
    acceptance_criteria: list[str]       # ids from the brief ("AC4"); own criteria as text
    implements: list[str] = []           # endpoint ids provided
    consumes: list[str] = []             # endpoint ids called

class AcceptanceCriterion(BaseModel):
    id: str                              # "AC1": the ticket's own numbering when it has one
    text: str                            # Given / When / Then in one or two lines

class TicketBrief(BaseModel):            # the planner's reading of the whole ticket (D128)
    preconditions: list[str] = []        # e.g. a "Shared Given" for all ACs
    criteria: list[AcceptanceCriterion] = []

class ExternalCall(BaseModel):          # [P10] a call the ticket's code makes to a service outside the ticket's repos
    service: str                         # `externals` name in config, e.g. "partner_public"
    repo: str                            # the repo that makes the call
    method: Literal["GET","POST","PUT","PATCH","DELETE"]
    path: str                            # e.g. /users/{id}
    response_from: Literal["recorded", "existing_code", "spec", "unknown"] = "unknown"   # where the expected response shape comes from
    note: str = ""

class ImplementationPlan(BaseModel):
    status: Literal["ready", "needs_clarification"]
    mode: Literal["initial", "revise", "replan", "qa_fix", "change"]   # revise = human feedback at plan approval; change [P5]
    brief: TicketBrief = TicketBrief()
    flow: str = ""                       # one line: how the request travels through the repos
    tasks: list[RepoTask]
    contract: InterfaceContract
    open_questions: list[str] = []
    risks: list[str] = []
    changes_from_previous: list[str] = []                   # revise / replan / qa_fix
    external_calls: list[ExternalCall] = []                 # [P10] so missing fixtures show before coding

# ---------- coding ----------
class ApiChange(BaseModel):
    method: Literal["GET","POST","PUT","PATCH","DELETE"]
    path: str
    note: str = ""

class UiChange(BaseModel):
    route: str
    component: str
    note: str = ""

class FindingResolution(BaseModel):
    finding_id: str
    action: Literal["fixed", "disputed", "not_applicable"]
    note: str

class CoderResult(BaseModel):
    repo: str
    status: Literal["success", "failed", "needs_clarification"]
    summary: str
    files_changed: list[str] = []
    tests: dict[str, Literal["passed","failed","skipped"]] = {}
    new_env_vars: list[str] = []
    migrations: list[str] = []
    new_dependencies: list[str] = []
    api_changes: list[ApiChange] = []
    ui_changes: list[UiChange] = []
    contract_deviations: list[str] = []
    open_questions: list[str] = []
    resolutions: list[FindingResolution] = []

class CoderResults(BaseModel):           # [single_session] one session coded several repos
    results: list[CoderResult]           # exactly one per repo it was asked to code

class VerifyResult(BaseModel):
    repo: str
    ok: bool
    checks: dict[str, Literal["passed","failed","skipped"]]
    output_tail: dict[str, str] = {}
    problems: list[str] = []
    head_sha: str | None = None          # [P9] the commit the checks ran on (set by the orchestrator)

# ---------- review ----------
class Finding(BaseModel):
    id: str                              # "R<round>-<repo>-F<n>" or "R<round>-X-F<n>" (contract)
    repo: str                            # repo that must change
    severity: Literal["blocker","major","minor","nit","human"]   # human = a person's change request [P5]
    category: str
    file: str | None = None
    line: int | None = None              # new-file line present in the diff
    comment: str
    suggestion: str | None = None

class CriterionCheck(BaseModel):         # [P9] one acceptance criterion of the repo's task
    criterion: str                       # the id from task.md: "AC4", or "T1" for the task's own criteria
    status: Literal["met", "not_met", "unknown", "not_applicable"]
    evidence: str = ""                   # file:line or test name; required for met
    note: str = ""                       # why not met / unknown / not applicable
    evidence_kind: Literal["real", "simulated", "traced", ""] = ""   # [P10] real = a live acceptance test proved it

    @model_validator(mode="before")
    @classmethod
    def _old_shape(cls, data: Any) -> Any:   # {"criterion", "met": bool} before P9
        if isinstance(data, dict) and "status" not in data and "met" in data:
            met = data.get("met")
            data = {**data, "status": "met" if met is True else "not_met" if met is False else "unknown"}
        return data

class PreviousCheck(BaseModel):          # [P9] the reviewer's answer on a finding people chose to fix
    finding_id: str
    outcome: Literal["fixed", "still_open", "withdrawn"]   # withdrawn = the coder's dispute convinced the reviewer
    note: str = ""

class ReviewResult(BaseModel):
    scope: str                           # repo name, or "contract"
    verdict: Literal["approve", "changes_requested"]
    summary: str
    findings: list[Finding] = []
    acceptance_criteria_check: list[CriterionCheck] = []
    previous: list[PreviousCheck] = []   # [P9] one per finding in previous_review.json; unanswered = still open

class ReviewResults(BaseModel):          # [single_session] one session reviewed several repos
    results: list[ReviewResult]          # one per repo (scope = the repo name) + one scope "contract"

# ---------- deployment / QA ----------
class DevopsFacts(BaseModel):
    repo: str
    new_env_vars: list[str]
    removed_env_vars: list[str]
    build_time_env_vars: list[str]
    undeclared_env_vars: list[str]
    migration_files: list[str]
    infra_files_changed: list[str]
    new_dependencies: list[str]
    notes: list[str]

class DeployResult(BaseModel):
    doc_markdown: str
    deploy_order: list[str]
    risk_level: Literal["low","medium","high"]
    needs_devops: bool                   # set by orchestrator from facts

class QaNotesResult(BaseModel):
    comment_markdown: str

# ---------- environment & live acceptance [P10] ----------
class ServiceNeed(BaseModel):            # one thing a ticket's repos need before they can be tested live
    repo: str | None = None              # None = the machine (a tool), not one repo
    kind: Literal["tool", "env", "database", "redis", "app", "external"]
    name: str                            # e.g. "redis-server", "PG_HOST_WS2TARGETSALES", "partner_public"
    status: Literal["ok", "missing", "failed", "warning"]
    detail: str = ""                     # plain words: what was found / what to do

class EnvReport(BaseModel):              # the preflight (`env_check`) before coding
    ok: bool
    needs: list[ServiceNeed] = []
    problems: list[str] = []             # what stops the ticket, in plain words
    checked_at: str = ""

class SeedRow(BaseModel):                # a row the test inserts before its calls (deleted afterwards)
    table: str
    alias: str = ""                      # refer to its columns later as {{seed.<alias>.<column>}}
    values: dict

class StepExpect(BaseModel):
    status: int | list[int] | None = None
    json_checks: list[dict] = Field(default=[], alias="json")   # {"path": "data.id", "equals"|"exists"|"contains"|"type": …}
    model_config = {"populate_by_name": True}

class DbCheck(BaseModel):                # a read-only query after the calls (runs in a READ ONLY transaction)
    sql: str                             # SELECT …; {{…}} values become parameters, never text
    equals: Any = None                   # the first column of the first row
    at_least: int | None = None
    wait_s: float = 0                    # async work (queues): retry until true or this many seconds pass

class Lookup(BaseModel):                 # a read-only query run before the case: its value is data the dev DB must have
    name: str                            # used later as {{lookup.<name>}}
    sql: str                             # SELECT …; first column of the first row; no row = the case is blocked, not failed
    why: str = ""                        # what the dev DB lacks when it finds nothing (shown to people)

class AcceptanceStep(BaseModel):         # one HTTP call
    repo: str                            # the repo whose local instance is called
    method: Literal["GET","POST","PUT","PATCH","DELETE"]
    path: str                            # the public path, without the repo's APP_PATH_PREFIX
    identity: str = Field(default="default", alias="as")   # an `acceptance.identities` name; "none" = no auth
    query: dict = {}
    body: Any = None
    headers: dict[str, str] = {}
    save: dict[str, str] = {}            # name → response path ("data.id"), used later as {{saved.<name>}}
    track: list[dict] = []               # rows the call creates: {"table": …, "where": {column: value}} (for cleanup)
    setup: bool = False                  # prepares data, proves nothing: if it fails the case is invalid, never the coder's
    expect: StepExpect = StepExpect()
    model_config = {"populate_by_name": True}

class AcceptanceCase(BaseModel):
    id: str                              # "C1" (contract) / "A1" (tester; T1… are task criteria)
    criterion: str = ""                  # "AC4"; "" = a contract case
    repo: str                            # the repo the case proves
    source: Literal["contract", "tester"]
    title: str
    use: list[str] = []                  # names of `AcceptancePlan.fixtures` whose seed rows are added before `seed`
    lookups: list[Lookup] = []
    seed: list[SeedRow] = []
    steps: list[AcceptanceStep]
    db: list[DbCheck] = []
    tables: list[str] = []               # every table it seeds, writes or reads (migration wait, cleanup)
    externals: list[str] = []            # `externals` names it needs fixtures for

class AcceptancePlan(BaseModel):         # the tester agent's result
    fixtures: dict[str, list[SeedRow]] = {}   # named seed sets many cases share (written once)
    cases: list[AcceptanceCase] = []
    blocked: list[dict] = []             # {"criterion": "AC9", "reason": "…"}: can't be tested locally, and why
    notes: list[str] = []

class CaseResult(BaseModel):
    case_id: str
    criterion: str = ""
    repo: str
    title: str = ""
    status: Literal["passed", "failed", "blocked", "invalid"]   # invalid = the case itself is wrong (back to the tester)
    reason: str = ""
    evidence: list[dict] = []            # per step: request, response (masked), checks
    evidence_kind: Literal["real", "simulated"] = "real"

class AcceptanceResult(BaseModel):
    repo: str | None = None              # None = the whole ticket
    ok: bool                             # nothing failed or invalid (blocked cases don't count)
    mode: Literal["required", "advisory"] = "required"
    head_shas: dict[str, str] = {}       # repo → the commit tested
    results: list[CaseResult] = []
    pending_migrations: list[str] = []   # the ticket's migrations not applied on the dev DB yet
    cleanup: dict = {}                   # {"inserted": n, "deleted": n, "left": n, "tables": {...}, "error": …}
    duration_s: float = 0

# ---------- commands & agents ----------
Verb = Literal["implement","approve_plan","revise_plan","review","fix","accept","deploy_doc",
               "devops_done","bug","fix_bugs","status","stop","retry","help",
               "guide","change","pause","unpause","report","test"]  # guide [P4b], change [P5], pause/unpause/report [P8], test [P10]

class Command(BaseModel):
    verb: Verb
    key: str | None = None
    args: dict = {}                      # provider, slot, scope, finding_ids, feedback, text
    actor_id: str
    actor_name: str
    source: Literal["cli", "jira"]
    raw: str
    interpreted: bool = False            # produced by the intent agent from free text
    confirmed: bool = False

class CommandResult(BaseModel):
    accepted: bool
    message_md: str                      # what the bot replies
    needs_confirmation: bool = False

class AgentRun(BaseModel):
    role: str
    repo: str | None
    slot: str | None
    run_no: int
    provider: Provider
    auth_mode: Literal["subscription", "api_key", "none"]
    model: str | None
    ok: bool
    duration_s: float
    turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    transcript_path: str
    error: str | None = None             # timeout, invalid_result, read_only_violation, provider_limit, cost_cap, run_cap
