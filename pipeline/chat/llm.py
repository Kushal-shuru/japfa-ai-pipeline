"""The small text writer (D209).

It writes short plain-language texts: the merge request description, summaries. It never runs
anything, never calls Jira/GitLab and never sees secrets (CLAUDE.md §9.16): every prompt passes
through `mask_text()` first.

D209: the writer is a tiny **agent run** (the `summary` role: Claude or Codex, chosen in
`agents.roles.summary`, on the same subscription or API key as every other agent), not a separate
model API. `ChatLLMChain.complete()` is what callers use; it returns None when the run failed (the
caller then uses its template). Every attempt is recorded as an `llm_call` event (provider, model,
latency, tokens, ok/error; never the message text).
"""

from __future__ import annotations

import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel

from pipeline.logging import get_logger
from pipeline.logging import mask_text as _mask_text

ErrorKind = Literal["timeout", "unavailable", "unsupported", "bad_output"]
RETRYABLE: frozenset[str] = frozenset({"timeout", "bad_output"})


class LLMError(Exception):
    def __init__(self, kind: ErrorKind, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


@dataclass
class LLMReply:
    text: str
    data: dict | None
    usage: dict
    provider: str
    model: str
    latency_s: float


class SummaryOut(BaseModel):
    """The `summary` role's result file."""

    text: str


class ChatLLM(Protocol):
    name: str
    model: str

    def complete(self, system: str, messages: list[dict], json_schema: dict | None = None,
                 max_tokens: int = 600, timeout_s: float = 120.0,
                 purpose: str = "chat") -> LLMReply:  # fmt: skip
        """One call. `messages`: [{"role": "user", "content": str}]. Raises `LLMError`."""
        ...


def mask_text(text: str) -> str:
    """Registered secret values (every `.env` secret) and anything that looks like a credential
    → `***`."""
    return str(_mask_text(text))


class AgentLLM:
    """The `summary` role through the agent runner. The role's prompt (`prompts/chat_summary.md`)
    is the instruction, so only the conversation is passed on."""

    name = "agent"
    model = "(summary role)"

    def __init__(self, runner: Any, home: Path) -> None:
        self.runner = runner
        self.home = Path(home)

    def complete(self, system: str, messages: list[dict], json_schema: dict | None = None,
                 max_tokens: int = 600, timeout_s: float = 120.0,
                 purpose: str = "chat") -> LLMReply:  # fmt: skip
        if self.runner is None:
            raise LLMError("unavailable", "no agent runner")
        start = time.monotonic()
        prompt = "\n\n".join(m["content"] for m in messages if m.get("role") == "user")
        runs = self.home / "runs" / "chat"
        runs.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="summary-", dir=runs) as tmp:
            # resolved: the Write permission rule and the prompt must name the same path
            # (macOS /var → /private/var)
            result, run = self.runner.run("chat", "summary", Path(tmp).resolve(), prompt,
                                          SummaryOut)  # fmt: skip
        if result is None:
            raise LLMError("unavailable", getattr(run, "error", None) or "agent run failed")
        return LLMReply(
            text=result.text,
            data=None,
            usage={"input_tokens": getattr(run, "input_tokens", None),
                   "output_tokens": getattr(run, "output_tokens", None)},  # fmt: skip
            provider=f"{getattr(run, 'provider', 'agent')}",
            model=getattr(run, "model", None) or self.model,
            latency_s=round(time.monotonic() - start, 3),
        )


@dataclass
class FakeLLM:
    """Replies from `script` (a str, or an LLMError to fail), consumed in order;
    `respond(system, messages, purpose)` computes one instead."""

    script: list[Any] = field(default_factory=list)
    respond: Callable[[str, list[dict], str], Any] | None = None
    name: str = "fake"
    model: str = "fake"
    calls: list[dict] = field(default_factory=list)

    def complete(self, system: str, messages: list[dict], json_schema: dict | None = None,
                 max_tokens: int = 600, timeout_s: float = 120.0,
                 purpose: str = "chat") -> LLMReply:  # fmt: skip
        self.calls.append({"system": system, "messages": messages, "purpose": purpose})
        if self.script:
            out = self.script.pop(0)
        elif self.respond is not None:
            out = self.respond(system, messages, purpose)
        else:
            raise LLMError("unavailable", "fake: nothing scripted")
        if isinstance(out, LLMError):
            raise out
        return LLMReply(str(out), None, {}, self.name, self.model, 0.0)


class ChatLLMChain:
    """The provider (one retry for a transient error) → None when it failed."""

    def __init__(self, primary: Any, *, timeout_s: float = 120.0, max_tokens: int = 600,
                 store: Any = None,
                 clock: Callable[[], float] = time.monotonic) -> None:  # fmt: skip
        self.primary = primary
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens
        self.store = store
        self.clock = clock

    @property
    def log(self) -> Any:
        # bound per use: the chain is built (in make_deps) before logging is configured
        return get_logger(component="chat_llm")

    @property
    def providers(self) -> list[Any]:
        return [p for p in (self.primary,) if p is not None]

    def describe(self) -> str:
        return " → ".join(f"{p.name} ({p.model})" for p in self.providers) or "none"

    def complete(self, system: str, messages: list[dict], json_schema: dict | None = None, *,
                 purpose: str = "chat", key: str | None = None,
                 max_tokens: int | None = None) -> LLMReply | None:  # fmt: skip
        """The reply, or None when the run failed (the caller uses its template)."""
        system = mask_text(system)
        messages = [{**m, "content": mask_text(str(m.get("content", "")))} for m in messages]
        for provider in self.providers:
            for _attempt in range(2):
                start = self.clock()
                try:
                    reply = provider.complete(system, messages, json_schema,
                                              max_tokens or self.max_tokens, self.timeout_s,
                                              purpose)  # fmt: skip
                except LLMError as err:
                    self._record(key, purpose, provider, None, str(err)[:200],
                                 self.clock() - start)  # fmt: skip
                    if err.kind not in RETRYABLE:
                        break
                    continue
                except Exception as err:  # a provider bug must not break a pipeline step
                    self.log.exception("chat_llm_provider_crashed", provider=provider.name)
                    self._record(key, purpose, provider, None, f"internal: {err}"[:200],
                                 self.clock() - start)  # fmt: skip
                    break
                self._record(key, purpose, provider, reply, None, reply.latency_s)
                return reply
        return None

    def _record(self, key: str | None, purpose: str, provider: Any, reply: LLMReply | None,
                error: str | None, latency_s: float) -> None:  # fmt: skip
        data = {
            "provider": reply.provider if reply else provider.name,
            "model": reply.model if reply else provider.model,
            "latency_s": round(latency_s, 3),
            "input_tokens": reply.usage.get("input_tokens") if reply else None,
            "output_tokens": reply.usage.get("output_tokens") if reply else None,
            "ok": reply is not None,
            "error": error,
        }
        self.log.info("chat_llm_call", purpose=purpose, ticket=key, **data)
        if self.store is None:
            return
        try:
            self.store.record_event(key or "chat", "llm_call", node=purpose, data=data)
        except Exception:  # tracking must never break a step
            self.log.exception("chat_llm_record_failed")


def build_chat_llm(config: Any, secrets: Any = None, store: Any = None, runner: Any = None,
                   home: Path | None = None) -> ChatLLMChain:
    """The configured writer (`chat_llm.provider`: agent | fake | none)."""
    cfg = config.chat_llm
    primary: Any = None
    if cfg.provider == "agent":
        primary = AgentLLM(runner, home or Path("."))
    elif cfg.provider == "fake":
        primary = FakeLLM()
    return ChatLLMChain(primary, timeout_s=cfg.timeout_s, store=store)
