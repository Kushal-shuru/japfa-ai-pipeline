"""Config: secrets from `.env`, everything else from `pipeline.config.yaml` (CLAUDE.md §10)."""

from __future__ import annotations

import copy
import os
import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AgentProviderName = Literal["claude", "codex"]
AuthMode = Literal["subscription", "api_key"]
GateMode = Literal["manual", "auto"]
RepoRoleName = Literal["gateway", "service", "frontend"]

ROLE_RANK: dict[str, int] = {"service": 0, "gateway": 1, "frontend": 2}
POOLS = ("coder", "reviewer", "deploy")
AGENTS = ("coder", "reviewer", "devops")  # chat.agents keys (D111, D113)
GATES = ("plan_approval", "review", "fix", "re_review", "deploy_doc", "qa_fix")
# Agent roles → the `agents.roles` entry that configures them (CLAUDE.md §3.7).
AGENT_ROLES: dict[str, str | None] = {
    "planner": "planner",
    "coder": "coder",
    "coder_fix": "coder",
    "coder_rebase": "coder",  # resolves rebase conflicts before a push (D125)
    "coder_ticket": "coder",  # one session codes every repo of the ticket (single_session)
    "coder_ticket_fix": "coder",  # …and fixes review findings / failed checks in it
    "reviewer": "reviewer",
    "reviewer_ticket": "reviewer",  # one session reviews every repo + the contract
    "contract_check": "reviewer",
    "deployment": "deployment",
    "qa_notes": "qa_notes",
    "tester": "tester",  # writes the live acceptance cases (P10)
    "summary": "summary",  # short plain-language texts (merge request descriptions), D209
    "criteria": "criteria",  # copies the acceptance criteria out of the ticket description (D218)
}
API_KEY_ENV: dict[str, str] = {"claude": "anthropic_api_key", "codex": "openai_api_key"}
HOME_SUBDIRS = ("repos", "workspaces", "specs", "runs", "secrets", "logs")

DEFAULT_CONFIG_PATH = "pipeline.config.yaml"


class ConfigError(Exception):
    """Configuration is missing or invalid. The message is meant for humans."""


# ---------- secrets ----------


class Secrets(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.environ.get("PIPELINE_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    jira_base_url: str = ""
    jira_email: str = ""
    jira_api_token: SecretStr | None = None
    gitlab_base_url: str = "https://gitlab.com"
    gitlab_coder_token: SecretStr | None = None
    gitlab_reviewer_token: SecretStr | None = None
    google_oauth_client_path: Path | None = None
    google_token_path: Path | None = None
    pipeline_home: Path = Path("~/.ai-pipeline")
    claude_code_oauth_token: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None

    @property
    def home(self) -> Path:
        return self.pipeline_home.expanduser()

    def secret_values(self) -> list[str]:
        """All secret strings, for log masking."""
        values = []
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr) and value.get_secret_value():
                values.append(value.get_secret_value())
        return values


# ---------- pipeline config ----------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# statuses of the earlier, longer workflow (D127): accepted in old configs and ignored
LEGACY_STATUSES = ("code_review", "changes_requested", "ready_to_merge", "awaiting_devops",
                   "qa_failed")  # qa_failed: the board has no such status; QA moves it to Blocked


class JiraStatuses(_Strict):
    """The ticket's Jira workflow (D127): In Dev from the start until every MR is merged (plan,
    code, review, fixes, merge wait), Blocked only while it needs a person to unblock it, RFT
    once merged; Testing and QA - Passed are set by QA; QA moves a failed ticket to Blocked."""

    todo: str = "To Do"
    in_dev: str = "In Development"
    blocked: str = "Blocked"
    rft: str = "Ready for Test"
    testing: str = "Testing"
    qa_passed: str = "QA - Passed"

    @model_validator(mode="before")
    @classmethod
    def _legacy(cls, data: object) -> object:
        if isinstance(data, dict):
            data = {k: v for k, v in data.items() if k not in LEGACY_STATUSES}
            if "needs_human" in data:  # renamed
                data.setdefault("blocked", data["needs_human"])
                del data["needs_human"]
        return data

    def in_testing(self, status: str) -> bool:
        """RFT or Testing: QA has it (either may be the current status)."""
        return status.casefold() in (self.rft.casefold(), self.testing.casefold())


class JiraConfig(_Strict):
    project: str
    # optional since D202: `implement` asks for the tech spec link and the repositories; tickets
    # that still carry them in these Jira custom fields can use them
    spec_field: str = ""
    repos_field: str = ""
    statuses: JiraStatuses = JiraStatuses()


class RepoCommands(_Strict):
    install: str
    lint: str  # may contain {files} = changed files only (D21)
    test: str | None = None  # optional (D20)
    sanity: str | None = None  # optional (D20)
    build: str | None = None  # required for frontend repos
    extra: list[str] = []  # additional verify checks (D21)


class StackDb(_Strict):
    """[P10] The **names** of the env variables that hold the repo's database connection (the
    values are read from `stack.env_from` at test time, never stored)."""

    host: str
    port: str
    name: str
    user: str
    password: str
    ssl: str | None = None  # "true" → sslmode=require


class RepoStack(_Strict):
    """[P10] How to run this repo for the live acceptance tests. `env` values may use
    `{{run.client_id}}`, `{{run.client_secret}}`, `{{run.session_secret}}`, `{{run.tag}}`,
    `{{stack.dir}}` (the run's folder, e.g. for logs), `{{redis.host}}`, `{{redis.port}}`,
    `{{redis.password}}`, `{{url:<repo>}}` (another repo's
    local instance) and `{{mock:<external>}}` (the fixture server for an external service)."""

    start: str = "node src/app.js"
    # the developer clone's .env (dev DB credentials): read when the tests run and passed to the
    # app process only; never copied into a worktree (coder agents can read worktrees)
    env_from: str | None = None
    env: dict[str, str] = {}
    port_env: str = "PORT"
    health_path: str = "/api/v1/health-checker"
    path_prefix_env: str | None = None  # e.g. APP_PATH_PREFIX: its value goes before every path
    boot_timeout_s: float = 90
    db: StackDb | None = None


class RepoConfig(_Strict):
    name: str
    role: RepoRoleName
    kind: Literal["backend", "frontend"] | None = None
    description: str = ""
    gitlab_project: str
    default_branch: str = "develop"
    commands: RepoCommands
    paths: dict[str, str] = {}
    # shared code (P9, DEMO-10): existing files here are off-limits to coders (verify fails when
    # one changes; new files are fine). A directory ends with "/", else a glob (`src/app.js`).
    shared_paths: list[str] = []
    # `.env` for coder + verify runs (D69): this file (test values only, never production),
    # else the repo's `.env.example`. `~` and `$PIPELINE_HOME` are expanded.
    env_file: str | None = None
    stack: RepoStack | None = None  # [P10] live acceptance tests; None = this repo isn't run

    @model_validator(mode="after")
    def _derive_kind(self) -> RepoConfig:
        derived = "frontend" if self.role == "frontend" else "backend"
        if self.kind is None:
            self.kind = derived
        elif self.kind != derived:
            raise ValueError(f"repo {self.name!r}: kind {self.kind!r} does not match role")
        if self.role == "frontend" and not self.commands.build:
            raise ValueError(f"repo {self.name!r}: frontend repos need commands.build")
        return self


class ProviderAuth(_Strict):
    auth: AuthMode = "subscription"


Effort = Literal["low", "medium", "high", "xhigh", "max"]


class RoleConfig(_Strict):
    provider: AgentProviderName | None = None
    model: str | None = None
    effort: Effort | None = None  # Claude `--effort`; like `model`, only for the role's provider
    max_turns: int = 100
    timeout_min: float = 30


class AgentsConfig(_Strict):
    default_provider: AgentProviderName = "claude"
    fallback_provider: AgentProviderName | None = None
    providers: dict[AgentProviderName, ProviderAuth] = Field(
        default_factory=lambda: {"claude": ProviderAuth(), "codex": ProviderAuth()}
    )
    roles: dict[str, RoleConfig] = {}
    # One coder session codes (and fixes) every repo of a ticket in the ticket's folder, and one
    # reviewer session reviews every repo plus the contract: no per-repo copies of the ticket,
    # spec, task or contract, and each prompt's fixed part is paid once, not per repo. Off =
    # one coder / reviewer run per repo, in parallel (the earlier behaviour).
    single_session: bool = False

    @model_validator(mode="after")
    def _check_roles(self) -> AgentsConfig:
        known = {r for r in AGENT_ROLES.values() if r}
        unknown = sorted(set(self.roles) - known)
        if unknown:
            raise ValueError(f"unknown agent roles {unknown}; valid: {sorted(known)}")
        for role in AGENT_ROLES:
            provider = self.provider_for(role)
            if provider not in self.providers:
                raise ValueError(
                    f"role {role!r} resolves to provider {provider!r}, "
                    f"which is missing from agents.providers"
                )
        if self.fallback_provider and self.fallback_provider not in self.providers:
            raise ValueError(f"fallback_provider {self.fallback_provider!r} not in providers")
        return self

    def provider_for(self, role: str) -> AgentProviderName:
        if role not in AGENT_ROLES:
            raise KeyError(f"unknown agent role {role!r}")
        entry = AGENT_ROLES[role]
        cfg = self.roles.get(entry) if entry else None
        return (cfg.provider if cfg and cfg.provider else None) or self.default_provider

    def role_config(self, role: str) -> RoleConfig:
        entry = AGENT_ROLES.get(role)
        return (self.roles.get(entry) if entry else None) or RoleConfig()


class Slot(_Strict):
    name: str
    provider: AgentProviderName


class LimitsConfig(_Strict):
    """Safety nets, set well above what a real ticket needs (P9, user 2026-10-01: a big ticket
    must never be stopped by a limit; the stall and dispute rules end pointless loops first)."""

    max_review_rounds: int = 6
    max_qa_rounds: int = 5
    max_verify_attempts: int = 4
    max_replans: int = 3
    max_plan_revisions: int = 10
    max_acceptance_attempts: int = 3  # [P10] live test failures sent back to the coder


class ChatConfig(_Strict):
    default_fix_scope: str = "major+"
    # the notification policy (P9): minimal = plan, one merge-request card, decisions and
    # blockers in the thread; one Jira summary comment edited in place. normal = every step
    # (debugging)
    verbosity: Literal["minimal", "normal"] = "minimal"
    reminder_hours: float = 0  # >0: a manual gate waiting this long → one reminder (P8 1.5)
    local_port: int = 8765
    # the team inside the one bot (D111): agent → display name. `coder` is required; roles
    # without their own agent speak as the Coder (`devops`: deployment note + DevOps, D113)
    agents: dict[str, str] = Field(
        default_factory=lambda: {"coder": "Coder", "reviewer": "Reviewer", "devops": "DevOps"}
    )

    @model_validator(mode="before")
    @classmethod
    def _legacy(cls, data: object) -> object:
        """Settings of the removed Teams front-end (bot name, main-channel summaries, daily
        digest): accepted in old configs and ignored."""
        if isinstance(data, dict):
            data = {k: v for k, v in data.items()
                    if k not in ("bot_name", "channel_summaries", "digest_time")}
        return data

    @model_validator(mode="after")
    def _check_agents(self) -> ChatConfig:
        unknown = sorted(set(self.agents) - set(AGENTS))
        if unknown or "coder" not in self.agents:
            raise ValueError(f"chat.agents: needs `coder`; valid agents: {list(AGENTS)}"
                             + (f" (unknown: {unknown})" if unknown else ""))  # fmt: skip
        if any(not n.strip() for n in self.agents.values()):
            raise ValueError("chat.agents: names must not be empty")
        return self


ChatLLMProvider = Literal["agent", "fake", "none"]


class ChatLLMConfig(_Strict):
    """Short plain-language texts (merge request descriptions, summaries). D209: written by a
    small Claude / Codex agent run (the `summary` role in `agents.roles` picks provider, model and
    effort), not by a separate model API. `none` = templates only."""

    provider: ChatLLMProvider = "agent"
    timeout_s: float = 120.0  # an agent run, not an HTTP call
    summaries: bool = True  # plain-language texts (templates when False)
    send_code: bool = False  # code excerpts (e.g. failed-check output) may go into the prompt


class VerifyConfig(_Strict):
    """Repo commands (install, lint, tests, build) run with an environment allowlist (P9,
    `safe_env`); name any other variable a repo's scripts need here. The pipeline's own
    credentials are never passed, even when named."""

    env_passthrough: list[str] = []


class PollingConfig(_Strict):
    jira_seconds: int = 45
    gitlab_seconds: int = 45
    migration_seconds: int = 120  # [P10] tickets waiting for a migration on the dev DB


class QaConfig(_Strict):
    base_url_placeholder: str = "{{QA_BASE_URL}}"
    token_placeholder: str = "{{QA_TOKEN}}"
    execute_locally: bool = False


class IdentityConfig(_Strict):
    """[P10] Who a live test call is made as. `headers`: plain headers (a service's client id and
    secret); values may use the `{{run.*}}` templates. `seeded_session`: the pipeline writes a
    logged-in session into the local Redis, in the shape the gateway's login writes, and signs
    the session cookie with the stack's generated secret (no real login)."""

    kind: Literal["headers", "seeded_session"]
    repo: str
    description: str = ""
    headers: dict[str, str] = {}
    # seeded_session: the session's fields (user, roles, permissions, partner …); `{{run.*}}`
    # templates allowed
    session: dict[str, Any] = {}
    cookie_name_env: str = "SESSION_NAME"
    secret_env: str = "SESSION_SECRET"
    key_prefix_env: str = "REDIS_KEY_PREFIX"  # the app's Redis client puts it before every key
    session_key: str = "sess:{sid}"  # connect-redis' default
    ttl_s: int = 28800


class ExternalConfig(_Strict):
    """[P10] A service outside the ticket's repos. `fixtures`: answered by the local fixture
    server (no fixture → the request fails loudly and the case is blocked); `real_dev`: reads
    (GET/HEAD) go to the real dev service, writes are refused; `blocked`: always refused."""

    description: str = ""
    mode: Literal["fixtures", "real_dev", "blocked"] = "fixtures"
    fixtures: str | None = None  # default $PIPELINE_HOME/fixtures/<name>
    real_url_env: str | None = None  # the variable in a repo's env_from with the real base URL


RUN_VALUE_NAMES = ("partner_id", "user_uuid", "user_pk", "user_id")


class AcceptanceConfig(_Strict):
    """[P10] Live acceptance tests before the merge requests. `off`: not run; `advisory`: run and
    report, never block; `required`: a failing case goes back to the coder, then to a person."""

    mode: Literal["off", "advisory", "required"] = "off"
    # the only database hosts the tests may connect to (the shared dev DB); anything else stops
    # the run before it connects
    allowed_db_hosts: list[str] = []
    identities: dict[str, IdentityConfig] = {}
    default_identity: dict[str, str] = {}  # repo → identity for steps that don't name one
    # the partner and user the tests run as, instead of new random ones per run (user,
    # 2026-10-07): partner_id, user_uuid (UUIDs), user_pk, user_id. A real partner already has
    # data, so the cleanup then deletes only rows the run inserted or tracked (no partner sweep)
    run_values: dict[str, str] = {}
    # headers every call to a repo carries, whoever it's made as (also `none`): e.g. the
    # gateway's X-Secure-Transmission-Type, checked before authentication
    repo_headers: dict[str, dict[str, str]] = {}
    externals: dict[str, ExternalConfig] = {}
    # facts about the repos the tester must know (e.g. "the gateway turns every 404 from a service
    # into 597"), shown to it as they are
    notes: list[str] = []
    preflight_boot: bool = True  # the preflight starts each app once (default-branch code)
    request_timeout_s: float = 30
    # the Tester is told what the dev DB holds for the run's partner (occupied periods, the values
    # of status-like columns) so it picks free windows and real reference values; structure only
    data_probe: bool = True
    redis_tls: bool = True  # the local Redis speaks TLS (the apps' clients use rediss://)


ReviewMode = Literal["local", "gitlab"]


class GitConfig(_Strict):
    # local (default, D207): code, review and fix on the machine; the branches are pushed and the
    # merge requests opened only once the Reviewer approves. gitlab: open the merge requests
    # first; the Reviewer posts its findings there as threads. Env: REVIEW_MODE.
    review_mode: ReviewMode = "local"
    coder_author: str = "AI Coder <ai-coder@example.com>"
    deploy_author: str = "AI Deploy <ai-deploy@example.com>"
    allow_same_gitlab_identity: bool = False  # one account as coder and reviewer (D194)


SpecSource = Literal["google-doc", "local"]


class SpecConfig(_Strict):
    """Where `implement` gets the tech spec (env: TECH_SPEC_SOURCE, TECH_SPEC_DIR). `google-doc`:
    the person pastes a Google Doc link (the pipeline reads it with its Google login). `local`:
    the person picks a document from `local_dir` (default `tech_spec/` in the pipeline repo)."""

    source: SpecSource = "local"
    local_dir: str = "tech_spec"


class DemoConfig(_Strict):
    """`pipeline demo-reset` (P8 4.1): the tickets it resets when none are named."""

    tickets: list[str] = []


class PipelineConfig(_Strict):
    jira: JiraConfig
    repos: list[RepoConfig]
    agents: AgentsConfig = AgentsConfig()
    agent_pool: dict[str, list[Slot]]
    gates: dict[str, GateMode] = Field(
        default_factory=lambda: {
            "plan_approval": "manual",
            "review": "auto",  # P9: review → fix → re-review runs by itself (W15 lite)
            "fix": "auto",
            "re_review": "auto",
            "deploy_doc": "auto",
            "qa_fix": "manual",
        }
    )
    chat: ChatConfig = ChatConfig()
    chat_llm: ChatLLMConfig = ChatLLMConfig()
    limits: LimitsConfig = LimitsConfig()
    polling: PollingConfig = PollingConfig()
    verify: VerifyConfig = VerifyConfig()
    qa: QaConfig = QaConfig()
    git: GitConfig = GitConfig()
    demo: DemoConfig = DemoConfig()
    spec: SpecConfig = SpecConfig()
    acceptance: AcceptanceConfig = AcceptanceConfig()  # [P10]

    @model_validator(mode="before")
    @classmethod
    def _legacy_gates(cls, data: object) -> object:
        """`gates.start` (Jira pickup) is gone: tickets start from the terminal. Old configs
        that still name it are accepted and the entry is ignored."""
        if isinstance(data, dict) and isinstance(data.get("gates"), dict):
            data = {**data, "gates": {k: v for k, v in data["gates"].items() if k != "start"}}
        return data

    @model_validator(mode="after")
    def _check(self) -> PipelineConfig:
        if not self.repos:
            raise ValueError("repos: at least one repo is required")
        names = [r.name for r in self.repos]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"repos: duplicate names {dupes}")
        gateways = [r.name for r in self.repos if r.role == "gateway"]
        if len(gateways) > 1:
            raise ValueError(f"repos: at most one gateway allowed, found {gateways}")

        missing = [p for p in POOLS if p not in self.agent_pool]
        extra = sorted(set(self.agent_pool) - set(POOLS))
        if missing or extra:
            raise ValueError(
                f"agent_pool: needs exactly the pools {list(POOLS)}"
                + (f"; missing {missing}" if missing else "")
                + (f"; unknown {extra}" if extra else "")
            )
        slot_names: list[str] = []
        for pool, slots in self.agent_pool.items():
            if not slots:
                raise ValueError(f"agent_pool.{pool}: needs at least one slot")
            slot_names += [s.name for s in slots]
        dupes = sorted({n for n in slot_names if slot_names.count(n) > 1})
        if dupes:
            raise ValueError(f"agent_pool: duplicate slot names {dupes}")

        self._check_acceptance(names)

        missing_gates = [g for g in GATES if g not in self.gates]
        unknown_gates = sorted(set(self.gates) - set(GATES))
        if missing_gates or unknown_gates:
            raise ValueError(
                "gates: "
                + (f"missing {missing_gates} " if missing_gates else "")
                + (f"unknown {unknown_gates} " if unknown_gates else "")
                + f"(valid: {list(GATES)})"
            )
        return self

    def _check_acceptance(self, names: list[str]) -> None:
        import uuid

        acc = self.acceptance
        unknown = sorted(set(acc.run_values) - set(RUN_VALUE_NAMES))
        if unknown:
            raise ValueError(f"acceptance.run_values: unknown {unknown}; valid: "
                             f"{list(RUN_VALUE_NAMES)}")  # fmt: skip
        for name in ("partner_id", "user_uuid"):
            if name in acc.run_values:
                try:
                    uuid.UUID(str(acc.run_values[name]))
                except ValueError:
                    raise ValueError(f"acceptance.run_values.{name}: must be a UUID") from None
        for ident, cfg in acc.identities.items():
            if cfg.repo not in names:
                raise ValueError(f"acceptance.identities.{ident}: unknown repo {cfg.repo!r}")
            if ident == "none":
                raise ValueError("acceptance.identities: `none` is reserved (a call without auth)")
        for repo, ident in acc.default_identity.items():
            if repo not in names:
                raise ValueError(f"acceptance.default_identity: unknown repo {repo!r}")
            if ident != "none" and ident not in acc.identities:
                raise ValueError(f"acceptance.default_identity.{repo}: unknown identity {ident!r}")
        if acc.mode != "off" and any(r.stack and r.stack.db for r in self.repos) \
                and not acc.allowed_db_hosts:  # fmt: skip
            raise ValueError("acceptance.allowed_db_hosts: name the dev database host(s) the "
                             "live tests may use")  # fmt: skip

    # ---------- helpers ----------

    def repo(self, name: str) -> RepoConfig:
        for r in self.repos:
            if r.name == name:
                return r
        raise KeyError(f"unknown repo {name!r}; valid: {self.repo_names}")

    @property
    def repo_names(self) -> list[str]:
        return [r.name for r in self.repos]

    def repo_order(self, names: list[str]) -> list[str]:
        """Sort by role rank (service → gateway → frontend), then config order."""
        index = {r.name: i for i, r in enumerate(self.repos)}
        return sorted(names, key=lambda n: (ROLE_RANK[self.repo(n).role], index[n]))

    def resolve_provider(self, role: str, override: str | None = None) -> AgentProviderName:
        """Provider for a run: explicit override (command / slot) → role config → default."""
        if override is not None:
            if override not in ("claude", "codex"):
                raise ValueError(f"unknown provider {override!r}; valid: claude, codex")
            return override  # type: ignore[return-value]
        return self.agents.provider_for(role)

    def enabled_providers(self) -> set[str]:
        used = {self.agents.provider_for(r) for r in AGENT_ROLES}
        used |= {s.provider for slots in self.agent_pool.values() for s in slots}
        if self.agents.fallback_provider:
            used.add(self.agents.fallback_provider)
        return used


def _format_validation_error(err: ValidationError, source: str) -> str:
    lines = [f"Invalid config in {source}:"]
    for e in err.errors():
        loc = ".".join(str(p) for p in e["loc"]) or "(root)"
        msg = e["msg"].removeprefix("Value error, ")
        lines.append(f"  - {loc}: {msg}")
    return "\n".join(lines)


def check_against_secrets(cfg: PipelineConfig, secrets: Secrets) -> list[str]:
    """Problems between config and `.env` (e.g. api_key auth without the key)."""
    problems = []
    for provider in sorted(cfg.enabled_providers()):
        auth = cfg.agents.providers.get(provider)  # type: ignore[call-overload]
        if auth and auth.auth == "api_key" and not getattr(secrets, API_KEY_ENV[provider]):
            problems.append(
                f"agents.providers.{provider}.auth is api_key but "
                f"{API_KEY_ENV[provider].upper()} is not set in .env"
            )
    return problems


def parse_config(data: dict, source: str = "<config>") -> PipelineConfig:
    try:
        return PipelineConfig.model_validate(data)
    except ValidationError as err:
        raise ConfigError(_format_validation_error(err, source)) from None


# Values that belong to the environment, not to the config file (D193): the file keeps the
# structure, `.env` (or the process environment) supplies these. An env value wins over a
# value left in the YAML.
JIRA_ENV = {
    "project": "JIRA_PROJECT",
    "spec_field": "JIRA_SPEC_FIELD",
    "repos_field": "JIRA_REPOS_FIELD",
}
RUN_VALUES_ENV = {
    "partner_id": "ACCEPTANCE_PARTNER_ID",
    "user_uuid": "ACCEPTANCE_USER_UUID",
    "user_pk": "ACCEPTANCE_USER_PK",
    "user_id": "ACCEPTANCE_USER_ID",
}
TECH_SPEC_SOURCE_ENV = "TECH_SPEC_SOURCE"  # local | google-doc (spec.source)
TECH_SPEC_DIR_ENV = "TECH_SPEC_DIR"  # spec.local_dir
REVIEW_MODE_ENV = "REVIEW_MODE"  # local | gitlab (git.review_mode)
ALLOWED_DB_HOSTS_ENV = "ACCEPTANCE_ALLOWED_DB_HOSTS"  # comma-separated


def repo_env_name(repo: str, field: str) -> str:
    """`demo-ws1-closio`, `gitlab_project` → `REPO_DEMO_WS1_CLOSIO_GITLAB_PROJECT`."""
    return f"REPO_{re.sub(r'[^A-Z0-9]+', '_', repo.upper())}_{field.upper()}"


def apply_env(data: dict, env: Mapping[str, str], source: str = "<config>") -> dict:
    """A copy of the raw config with the environment's values filled in (D193). Raises a
    ConfigError naming the variable when a required one is set nowhere."""
    out = copy.deepcopy(data)
    jira = out.setdefault("jira", {})
    for key, name in JIRA_ENV.items():
        if env.get(name):
            jira[key] = env[name]
    missing = [name for key, name in JIRA_ENV.items()
               if key not in ("spec_field", "repos_field")
               and not jira.get(key)]  # fmt: skip
    for repo in out.get("repos") or []:
        name = repo.get("name", "")
        if value := env.get(repo_env_name(name, "gitlab_project")):
            repo["gitlab_project"] = value
        elif not repo.get("gitlab_project"):
            missing.append(repo_env_name(name, "gitlab_project"))
        if (value := env.get(repo_env_name(name, "env_from"))) and repo.get("stack") is not None:
            repo["stack"]["env_from"] = value
    if mode := (env.get(REVIEW_MODE_ENV) or "").strip().lower():
        out.setdefault("git", {})["review_mode"] = mode
    if source := (env.get(TECH_SPEC_SOURCE_ENV) or "").strip().lower().replace("_", "-"):
        out.setdefault("spec", {})["source"] = {"google": "google-doc", "gdoc": "google-doc",
                                               "online": "google-doc"}.get(source, source)
    if folder := (env.get(TECH_SPEC_DIR_ENV) or "").strip():
        out.setdefault("spec", {})["local_dir"] = folder
    hosts = env.get(ALLOWED_DB_HOSTS_ENV)
    run_values = {k: env[n] for k, n in RUN_VALUES_ENV.items() if env.get(n)}
    if hosts or run_values:
        acc = out.setdefault("acceptance", {})
        if hosts:
            acc["allowed_db_hosts"] = [h.strip() for h in hosts.split(",") if h.strip()]
        if run_values:
            acc.setdefault("run_values", {}).update(run_values)
    if missing:
        raise ConfigError(f"Config {source} needs these set in .env (or the environment): "
                          + ", ".join(missing))
    return out


def config_env(secrets: Secrets) -> dict[str, str]:
    """The `.env` file's values with the process environment on top."""
    from dotenv import dotenv_values

    file = Secrets.model_config.get("env_file")
    values: dict[str, str] = {}
    if file and Path(str(file)).is_file():
        values = {k: v for k, v in dotenv_values(file).items() if v is not None}
    return {**values, **os.environ}


def load_config_file(path: str | Path, secrets: Secrets | None = None,
                     env: Mapping[str, str] | None = None) -> PipelineConfig:  # fmt: skip
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"Config file {path} not found. Copy pipeline.config.example.yaml to {path}."
        )
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as err:
        raise ConfigError(f"Config file {path} is not valid YAML: {err}") from None
    if env is not None:
        data = apply_env(data, env, str(path))
    cfg = parse_config(data, str(path))
    if secrets is not None:
        problems = check_against_secrets(cfg, secrets)
        if problems:
            lines = "\n".join(f"  - {p}" for p in problems)
            raise ConfigError(f"Invalid config in {path}:\n{lines}")
    return cfg


@lru_cache(maxsize=1)
def load_secrets() -> Secrets:
    return Secrets()


@lru_cache(maxsize=1)
def load_config(path: str | None = None) -> PipelineConfig:
    """Cached config; path from argument, `PIPELINE_CONFIG`, or ./pipeline.config.yaml."""
    path = path or os.environ.get("PIPELINE_CONFIG", DEFAULT_CONFIG_PATH)
    secrets = load_secrets()
    return load_config_file(path, secrets, config_env(secrets))


def ensure_home(home: Path) -> Path:
    """Create `$PIPELINE_HOME` and its standard sub-directories."""
    home = home.expanduser()
    for sub in HOME_SUBDIRS:
        (home / sub).mkdir(parents=True, exist_ok=True)
    return home
