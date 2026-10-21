"""Scripted provider for tests and --dry-run: canned results per (role, repo), optional file
writes, configurable failures including `limit_hit`. Can stand in for "claude" or "codex"."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pipeline.agents.providers.base import ProviderOutput, Transcript
from pipeline.agents.roles import RoleSpec

Result = dict | str | Callable[[Path, str], dict | str] | None


@dataclass
class Script:
    role: str
    repo: str | None = None  # matched against cwd.name; None = any
    result: Result = None  # written to .agent/<result_file> (or returned as final message)
    files: dict[str, str] = field(default_factory=dict)  # relative path → content
    error: str | None = None
    limit_hit: bool = False
    sleep_s: float = 0.0
    as_final_message: bool = False  # Codex read-only style: result in the final message
    input_tokens: int | None = 100
    output_tokens: int | None = 50
    cost_usd: float | None = None
    turns: int | None = 3


class FakeProvider:
    def __init__(self, name: str = "fake", available: bool = True) -> None:
        self.name = name
        self._available = available
        self.scripts: list[Script] = []
        self.defaults: dict[str, Script] = {}  # role → used whenever no queued script matches
        self.calls: list[dict[str, Any]] = []
        # role → fn(result dict, cwd) → result dict: fills in what every run of that role must
        # answer, so scripts only state what matters to them (P9: the review's criteria)
        self.finish: dict[str, Callable[[dict, Path], dict]] = {}

    def add(self, role: str, result: Result = None, **kwargs: Any) -> Script:
        """Queue one canned run. Runs consume scripts in order (first match wins)."""
        script = Script(role=role, result=result, **kwargs)
        self.scripts.append(script)
        return script

    def set_default(self, role: str, result: Result = None, **kwargs: Any) -> Script:
        """A canned run for `role` that is never used up (after the queued ones)."""
        self.defaults[role] = Script(role=role, result=result, **kwargs)
        return self.defaults[role]

    def available(self) -> tuple[bool, str]:
        return self._available, "fake" if self._available else "fake: unavailable"

    def _pop(self, role: str, repo: str) -> Script | None:
        for i, s in enumerate(self.scripts):
            if s.role == role and s.repo in (None, repo):
                return self.scripts.pop(i)
        default = self.defaults.get(role)
        return default if default is not None and default.repo in (None, repo) else None

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
        self.calls.append(
            {"role": spec.name, "cwd": cwd, "prompt": prompt, "env": env,
             "resume_id": resume_id, "model": spec.model, "timeout_s": timeout_s}
        )  # fmt: skip
        script = self._pop(spec.name, cwd.name)
        with Transcript(transcript_path) as tx:
            tx.write({"type": "fake_run", "role": spec.name, "cwd": str(cwd)})
        if script is None:
            return ProviderOutput(ok=False, error=f"provider_error: no fake script for {spec.name}")
        if script.sleep_s:
            if script.sleep_s >= timeout_s:
                time.sleep(timeout_s)
                return ProviderOutput(ok=False, error="timeout")
            time.sleep(script.sleep_s)
        for rel, content in script.files.items():
            path = cwd / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        out = ProviderOutput(
            ok=script.error is None and not script.limit_hit,
            turns=script.turns,
            input_tokens=script.input_tokens,
            output_tokens=script.output_tokens,
            cost_usd=script.cost_usd,
            error=script.error or ("provider_error: usage limit" if script.limit_hit else None),
            limit_hit=script.limit_hit,
            session_id=f"fake-{len(self.calls)}",
            model=spec.model,
        )
        result = script.result(cwd, prompt) if callable(script.result) else script.result
        if isinstance(result, dict) and spec.name in self.finish:
            result = self.finish[spec.name](result, cwd)
        if result is not None and out.ok:
            text = result if isinstance(result, str) else json.dumps(result)
            if script.as_final_message:
                out.final_message = text
            else:
                target = cwd / ".agent" / spec.result_file
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
        return out
