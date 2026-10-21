"""Dependency container passed to every node (CLAUDE.md §6). Nodes never import clients."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pipeline.config import (
    ConfigError,
    PipelineConfig,
    Secrets,
    ensure_home,
    load_config,
    load_secrets,
)
from pipeline.integrations.base import GitLabPort, JiraPort, SpecPort
from pipeline.store import Store
from pipeline.workspace import Workspaces


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Deps:
    jira: JiraPort
    gitlab: GitLabPort
    spec: SpecPort
    store: Store
    config: PipelineConfig
    secrets: Secrets
    clock: Callable[[], datetime] = utcnow
    runner: Any = None  # AgentRunner (Phase 2)
    pool: Any = None  # AgentPool (Phase 3)
    chat: Any = None  # ChatPort (Phase 3)
    workspaces: Workspaces | None = None
    dry_run: bool = False
    verifier: Any = None  # Verifier (Phase 3)
    chat_llm: Any = None  # ChatLLMChain (the `summary` agent, D209); None = templates only
    acceptance: Any = None  # AcceptanceService (P10); None = the real one, built when needed

    @property
    def notifier(self) -> Any:
        from pipeline.chat.notifier import Notifier

        return Notifier(
            self.chat,
            self.jira,
            self.store,
            None,  # terminal messages are templates (D167); only the MR description uses the writer
            self.config.chat_llm,
            self.config.chat.agents,
            verbosity=self.config.chat.verbosity,
            auto_plan=self.config.gates.get("plan_approval") == "auto",
        )

    @property
    def home(self) -> Any:
        return self.secrets.home


def _runner(config: PipelineConfig, secrets: Secrets, store: Store, home: Any) -> Any:
    from pipeline.agents.runner import AgentRunner

    return AgentRunner(config, secrets, store, home)


def _chat_llm(config: PipelineConfig, secrets: Secrets, store: Store, runner: Any,
              home: Any) -> Any:  # fmt: skip
    from pipeline.chat.llm import build_chat_llm

    return build_chat_llm(config, secrets, store, runner, home)


def _require(secrets: Secrets, *names: str) -> None:
    missing = [n.upper() for n in names if not getattr(secrets, n)]
    if missing:
        raise ConfigError(f"Missing in .env: {', '.join(missing)}")


def make_deps(
    dry_run: bool = False,
    config: PipelineConfig | None = None,
    secrets: Secrets | None = None,
) -> Deps:
    """Real clients, or (dry_run) in-memory fakes + in-memory store. Workspaces are real in
    both modes (local git only; pushes need the coder token)."""
    config = config or load_config()
    secrets = secrets or load_secrets()
    home = ensure_home(secrets.home)
    token = secrets.gitlab_coder_token.get_secret_value() if secrets.gitlab_coder_token else None
    workspaces = Workspaces(home, config, secrets.gitlab_base_url, token)

    if dry_run:
        from pipeline.integrations.fakes import FakeGitLab, FakeJira, FakeSpec

        store = Store("sqlite://")
        store.init()
        runner = _runner(config, secrets, store, home)
        # Agents run for real in dry-run too: they only touch local workspaces.
        return Deps(
            jira=FakeJira(store),
            gitlab=FakeGitLab(),
            spec=FakeSpec(),
            store=store,
            config=config,
            secrets=secrets,
            workspaces=workspaces,
            runner=runner,
            dry_run=True,
            chat_llm=_chat_llm(config, secrets, store, runner, home),
        )

    from pipeline.integrations.gdocs import GoogleDocsSpec
    from pipeline.integrations.gitlab_client import GitLabClient
    from pipeline.integrations.jira_client import JiraClient

    _require(secrets, "jira_base_url", "jira_email", "jira_api_token")
    _require(secrets, "gitlab_coder_token", "gitlab_reviewer_token")
    store = Store.for_home(home)
    store.init()
    assert secrets.jira_api_token and secrets.gitlab_reviewer_token and token
    runner = _runner(config, secrets, store, home)
    return Deps(
        jira=JiraClient(
            secrets.jira_base_url,
            secrets.jira_email,
            secrets.jira_api_token.get_secret_value(),
            config.jira,
            store,
        ),
        gitlab=GitLabClient(
            secrets.gitlab_base_url, token, secrets.gitlab_reviewer_token.get_secret_value(), store
        ),
        spec=GoogleDocsSpec(
            secrets.google_oauth_client_path, secrets.google_token_path, home / "specs"
        ),
        store=store,
        config=config,
        secrets=secrets,
        workspaces=workspaces,
        runner=runner,
        chat_llm=_chat_llm(config, secrets, store, runner, home),
    )


def _spec(google: Any, config: Any) -> Any:
    """Google links go to `google`; `file://` links (`spec.source: local`) to tech_spec/."""
    from pipeline.integrations.local_spec import LocalSpec, SpecRouter, spec_dir

    return SpecRouter(google, LocalSpec(spec_dir(config.spec.local_dir)))
