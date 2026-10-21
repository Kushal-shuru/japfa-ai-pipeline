"""Provider protocol, run output, and the agent's environment (CLAUDE.md §4.1)."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from pipeline.agents.roles import RoleSpec
    from pipeline.config import Secrets

# Keys that select API-key auth. Removed in subscription mode: a key silently overrides the login.
PROVIDER_KEYS: dict[str, tuple[str, ...]] = {
    "claude": ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"),
    "codex": ("OPENAI_API_KEY", "CODEX_API_KEY"),
}
# Never handed to an agent (§9.12): pipeline integration credentials.
PIPELINE_SECRET_ENV = (
    "JIRA_API_TOKEN",
    "GITLAB_CODER_TOKEN",
    "GITLAB_REVIEWER_TOKEN",
    "PIPELINE_GIT_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",  # re-added below for claude only
)

LIMIT_RE = re.compile(
    r"usage limit|rate[ _-]?limit|quota|too many requests|\b429\b|billing|credit balance", re.I
)


@dataclass
class ProviderOutput:
    ok: bool
    turns: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None  # as reported; the runner decides whether it is real spend
    error: str | None = None  # timeout | max_turns | provider_error: … | provider_unavailable
    limit_hit: bool = False
    session_id: str | None = None  # for a follow-up run in the same conversation
    final_message: str | None = None  # last agent message (Codex read-only result, 1.9)
    model: str | None = None


class AgentProvider(Protocol):
    name: str

    def available(self) -> tuple[bool, str]: ...

    def run(
        self,
        spec: RoleSpec,
        cwd: Path,
        prompt: str,
        extra_dirs: tuple[Path, ...],
        env: dict[str, str],
        timeout_s: float,
        transcript_path: Path,
        resume_id: str | None = None,
    ) -> ProviderOutput: ...


def build_env(
    provider: str,
    auth_mode: str,
    secrets: Secrets,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Clean environment for an agent run.

    subscription: every provider API key removed; Claude gets CLAUDE_CODE_OAUTH_TOKEN if set.
    api_key: only the matching provider's key is injected (Codex reads CODEX_API_KEY in exec).
    Pipeline credentials never reach the agent.
    """
    env = dict(os.environ if base is None else base)
    for key in (*PIPELINE_SECRET_ENV, *(k for keys in PROVIDER_KEYS.values() for k in keys)):
        env.pop(key, None)
    if auth_mode == "api_key":
        if provider == "claude" and secrets.anthropic_api_key:
            env["ANTHROPIC_API_KEY"] = secrets.anthropic_api_key.get_secret_value()
        elif provider == "codex" and secrets.openai_api_key:
            key = secrets.openai_api_key.get_secret_value()
            env["CODEX_API_KEY"] = key
            env["OPENAI_API_KEY"] = key
    elif provider == "claude" and secrets.claude_code_oauth_token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = secrets.claude_code_oauth_token.get_secret_value()
    return env


def removed_keys(env: Mapping[str, str]) -> list[str]:
    """Credential variables absent from `env` (for providers that can only override, not unset)."""
    names = (*PIPELINE_SECRET_ENV, *(k for keys in PROVIDER_KEYS.values() for k in keys))
    return [k for k in names if k not in env]


class Transcript:
    """Append-only JSONL transcript of one agent run."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fh = path.open("a", encoding="utf-8")

    def write(self, record: Any) -> None:
        self._fh.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
        self._fh.flush()

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> Transcript:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
