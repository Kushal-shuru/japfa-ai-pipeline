"""Codex provider: `codex exec` subprocess (CLAUDE.md §4.1).

    codex exec --json -C <cwd> -s read-only|workspace-write [-m model] [--skip-git-repo-check]
               [--add-dir d] -o <last-message file> -          (prompt on stdin)
    codex exec resume <thread_id> --json -c sandbox_mode=… … -

`exec` never prompts for approvals (the sandbox is enforced up front). JSONL events go to the
transcript: `thread.started` (thread_id), `turn.completed` (usage), `turn.failed` / `error`.
Codex reports no cost and no turn count. A read-only sandbox can't write `.agent/`, so read-only
roles return their JSON as the final message and the runner writes the file (1.9).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
from pathlib import Path

from pipeline.agents.providers.base import LIMIT_RE, ProviderOutput
from pipeline.agents.providers.children import register, unregister
from pipeline.agents.roles import RoleSpec


class CodexProvider:
    name = "codex"

    def __init__(self, binary: str = "codex") -> None:
        self.binary = binary
        self.children_dir: Path | None = None  # set by AgentRunner: pidfiles (K18)

    def available(self) -> tuple[bool, str]:
        path = shutil.which(self.binary)
        if not path:
            return False, f"`{self.binary}` not found on PATH"
        try:
            proc = subprocess.run(
                [path, "--version"], capture_output=True, text=True, timeout=20, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as err:
            return False, f"{path}: {err}"
        return proc.returncode == 0, (proc.stdout or proc.stderr).strip() or path

    def command(
        self,
        spec: RoleSpec,
        cwd: Path,
        extra_dirs: tuple[Path, ...],
        last_message: Path,
        resume_id: str | None = None,
    ) -> list[str]:
        cmd = [self.binary, "exec"]
        if resume_id:
            # `exec resume` has no -C / -s: cwd comes from the subprocess, sandbox from config.
            cmd += ["resume", resume_id, "-c", f'sandbox_mode="{spec.codex_sandbox}"']
        else:
            cmd += ["-C", str(cwd), "-s", spec.codex_sandbox]
            for d in extra_dirs:
                cmd += ["--add-dir", str(d)]
        if spec.codex_sandbox == "workspace-write":
            # P9 (DEMO-9): API tests start a local HTTP server; without network the sandbox
            # refuses `listen` (EPERM). Codex keeps `.git` read-only whatever we allow, so coders
            # don't commit under Codex: the pipeline commits what they leave (coding node).
            cmd += ["-c", "sandbox_workspace_write.network_access=true"]
        cmd += ["--json", "-o", str(last_message)]
        if spec.model:
            cmd += ["-m", spec.model]
        if spec.effort:  # the role's `effort` as Codex's reasoning effort
            cmd += ["-c", f'model_reasoning_effort="{spec.effort}"']
        if not (cwd / ".git").exists():
            cmd.append("--skip-git-repo-check")
        cmd.append("-")  # prompt from stdin
        return cmd

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
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        last_message = transcript_path.with_suffix(".last.txt")
        stderr_path = transcript_path.with_suffix(".stderr.log")
        last_message.unlink(missing_ok=True)
        cmd = self.command(spec, cwd, extra_dirs, last_message, resume_id)
        timed_out = False
        try:
            with transcript_path.open("w") as stdout_fh, stderr_path.open("w") as stderr_fh:
                proc = subprocess.Popen(
                    cmd,
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=stdout_fh,
                    stderr=stderr_fh,
                    text=True,
                    start_new_session=True,
                )
                register(self.children_dir, proc.pid)  # own session: pgid == pid
                assert proc.stdin is not None
                try:
                    proc.stdin.write(prompt)
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
                try:
                    code = proc.wait(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    _kill(proc)
                    code = proc.wait()
                finally:
                    unregister(self.children_dir, proc.pid)
        except OSError as err:
            return ProviderOutput(ok=False, error=f"provider_unavailable: {err}")

        out, messages = parse_events(transcript_path)
        out.model = spec.model
        stderr = stderr_path.read_text(errors="replace") if stderr_path.exists() else ""
        if last_message.exists():
            out.final_message = last_message.read_text()
        if timed_out:
            out.ok, out.error = False, "timeout"
        elif code != 0 or out.error:
            detail = "; ".join(messages) or _tail(stderr) or f"exit code {code}"
            out.ok = False
            out.error = f"provider_error: {detail}"
            out.limit_hit = bool(LIMIT_RE.search(f"{detail} {_tail(stderr)}"))
        else:
            out.ok = True
        out.limit_hit = out.limit_hit and not out.ok
        return out


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)


def _tail(text: str, lines: int = 5) -> str:
    return " | ".join(line for line in text.strip().splitlines()[-lines:] if line.strip())


def parse_events(path: Path) -> tuple[ProviderOutput, list[str]]:
    """Token usage, thread id and error messages from a `codex exec --json` event stream.
    Only `turn.failed` marks the run failed; `error` events can be transient (reconnects)."""
    out = ProviderOutput(ok=True)
    input_tokens = output_tokens = 0
    seen_usage = False
    errors: list[str] = []
    failed = False
    text = path.read_text(errors="replace") if path.exists() else ""
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        kind = ev.get("type")
        if kind == "thread.started":
            out.session_id = ev.get("thread_id")
        elif kind == "turn.completed":
            usage = ev.get("usage") or {}
            seen_usage = True
            input_tokens += int(usage.get("input_tokens") or 0)
            output_tokens += int(usage.get("output_tokens") or 0)
        elif kind == "turn.failed":
            failed = True
            err = ev.get("error") or {}
            errors.append(err.get("message", "") if isinstance(err, dict) else str(err))
        elif kind == "error":
            errors.append(str(ev.get("message", "")))
    if seen_usage:
        out.input_tokens, out.output_tokens = input_tokens, output_tokens
    messages = [e for e in errors if e]
    if failed:
        out.ok = False
        out.error = "provider_error: " + ("; ".join(messages) or "turn failed")
    return out, messages
