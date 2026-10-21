"""Claude provider: Claude Agent SDK → bundled Claude Code CLI (CLAUDE.md §4.1).

Options used (claude-agent-sdk 0.2.x): cwd, add_dirs, model, max_turns, system_prompt (claude_code
preset + append), setting_sources=["project"] (loads the repo's CLAUDE.md, not ~/.claude),
permission_mode="dontAsk" with tools / allowed_tools / disallowed_tools, env, resume.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path
from typing import Any

import anyio

from pipeline.agents.providers.base import LIMIT_RE, ProviderOutput, Transcript, removed_keys
from pipeline.agents.roles import RoleSpec

LIMIT_ERRORS = {"rate_limit", "billing_error"}
SYSTEM_APPEND = (
    "You are running unattended as the {role} agent of an automated SDLC pipeline. Nobody can "
    "answer questions during the run: when something is unclear, record it in the result file "
    "as the instructions describe. Put your result in .agent/{result_file} under your working "
    "directory."
)

QueryFn = Callable[..., AsyncIterator[Any]]


def _run_blocking(fn: Callable[[], Coroutine[Any, Any, ProviderOutput]]) -> ProviderOutput:
    """Run a coroutine from sync code, also when called inside a running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return anyio.run(fn)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(anyio.run, fn).result()


def _record(msg: Any) -> dict:
    is_instance = dataclasses.is_dataclass(msg) and not isinstance(msg, type)
    data = dataclasses.asdict(msg) if is_instance else {"value": msg}
    return {"type": type(msg).__name__, **data}


class ClaudeProvider:
    name = "claude"

    def __init__(self, query_fn: QueryFn | None = None) -> None:
        self._query = query_fn

    def available(self) -> tuple[bool, str]:
        try:
            import claude_agent_sdk
        except ImportError as err:  # pragma: no cover - dependency is installed
            return False, f"claude-agent-sdk not installed: {err}"
        return True, f"claude-agent-sdk {claude_agent_sdk.__version__}"

    def options(
        self,
        spec: RoleSpec,
        cwd: Path,
        extra_dirs: tuple[Path, ...],
        env: dict[str, str],
        resume_id: str | None = None,
        stderr: Callable[[str], None] | None = None,
    ) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        # The SDK merges `env` over os.environ and cannot unset: blank the removed credentials.
        env_over = {**env, **{k: "" for k in removed_keys(env)}}
        if "CLAUDE_CODE_OAUTH_TOKEN" in env:
            env_over["CLAUDE_CODE_OAUTH_TOKEN"] = env["CLAUDE_CODE_OAUTH_TOKEN"]
        perms = spec.claude_permissions(cwd)
        return ClaudeAgentOptions(
            cwd=str(cwd),
            add_dirs=[str(d) for d in extra_dirs],
            model=spec.model,
            effort=spec.effort,  # type: ignore[arg-type]
            max_turns=spec.max_turns,
            system_prompt={
                "type": "preset",
                "preset": "claude_code",
                "append": SYSTEM_APPEND.format(role=spec.name, result_file=spec.result_file),
            },
            setting_sources=["project"],
            permission_mode="dontAsk",
            tools=perms["tools"],
            allowed_tools=perms["allowed_tools"],
            disallowed_tools=perms["disallowed_tools"],
            env=env_over,
            resume=resume_id,
            stderr=stderr,
        )

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
    ) -> ProviderOutput:
        return _run_blocking(
            lambda: self._arun(
                spec, cwd, prompt, extra_dirs, env, timeout_s, transcript_path, resume_id
            )
        )

    async def _arun(
        self,
        spec: RoleSpec,
        cwd: Path,
        prompt: str,
        extra_dirs: tuple[Path, ...],
        env: dict[str, str],
        timeout_s: float,
        transcript_path: Path,
        resume_id: str | None,
    ) -> ProviderOutput:
        from claude_agent_sdk import (
            AssistantMessage,
            CLINotFoundError,
            RateLimitEvent,
            ResultError,
            ResultMessage,
        )
        from claude_agent_sdk import query as sdk_query

        query = self._query or sdk_query

        stderr_tail: list[str] = []

        def on_stderr(line: str) -> None:
            stderr_tail.append(line)
            del stderr_tail[:-30]

        options = self.options(spec, cwd, extra_dirs, env, resume_id, on_stderr)
        result: Any = None
        limit_signal = False
        model: str | None = spec.model
        error: str | None = None
        with Transcript(transcript_path) as tx:
            tx.write({"type": "run_start", "provider": "claude", "role": spec.name,
                      "cwd": str(cwd), "model": spec.model, "resume": resume_id})  # fmt: skip
            try:
                with anyio.fail_after(timeout_s):
                    async for msg in query(prompt=prompt, options=options):
                        tx.write(_record(msg))
                        if isinstance(msg, AssistantMessage):
                            model = msg.model or model
                            if msg.error in LIMIT_ERRORS:
                                limit_signal = True
                        elif isinstance(msg, RateLimitEvent):
                            if msg.rate_limit_info.status == "rejected":
                                limit_signal = True
                        elif isinstance(msg, ResultMessage):
                            result = msg
            except TimeoutError:
                error = "timeout"
            except CLINotFoundError as err:
                error = f"provider_unavailable: {err}"
            except ResultError as err:
                if err.api_error_status == 429 or LIMIT_RE.search(err.result or ""):
                    limit_signal = True
                error = f"provider_error: {err.subtype or ''} {err.result or err}".strip()
            except Exception as err:
                text = f"{err} {' '.join(stderr_tail[-5:])}"
                if LIMIT_RE.search(text):
                    limit_signal = True
                error = f"provider_error: {err}"
            tx.write({"type": "run_end", "error": error, "stderr_tail": stderr_tail[-10:]})

        out = ProviderOutput(ok=False, error=error, model=model)
        if result is not None:
            usage = result.usage or {}
            out.turns = result.num_turns
            out.input_tokens = _sum_input(usage)
            out.output_tokens = usage.get("output_tokens")
            out.cost_usd = result.total_cost_usd
            out.session_id = result.session_id
            out.final_message = result.result
            if result.is_error:
                if result.api_error_status == 429:
                    limit_signal = True
                if result.subtype == "error_max_turns":
                    out.error = out.error or "max_turns"
                else:
                    detail = result.result or ", ".join(result.errors or []) or result.subtype
                    out.error = out.error or f"provider_error: {detail}"
            elif error is None:
                out.ok = True
        elif error is None:
            out.error = "provider_error: no result message"
        out.limit_hit = limit_signal and not out.ok
        return out


def _sum_input(usage: dict) -> int | None:
    parts = [
        usage.get(k)
        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    ]
    nums = [p for p in parts if isinstance(p, int)]
    return sum(nums) if nums else None
