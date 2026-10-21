"""Agent providers: Claude (Agent SDK), Codex (`codex exec`), and a fake for tests."""

from __future__ import annotations

from pipeline.agents.providers.base import AgentProvider


def default_providers() -> dict[str, AgentProvider]:
    from pipeline.agents.providers.claude_provider import ClaudeProvider
    from pipeline.agents.providers.codex_provider import CodexProvider

    return {"claude": ClaudeProvider(), "codex": CodexProvider()}
