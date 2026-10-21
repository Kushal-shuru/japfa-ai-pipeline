"""`run_agent()`: provider choice, limits, timeout, result validation, read-only enforcement,
fallback on plan limits, and one `agent_runs` row per attempt (CLAUDE.md §4.1, Phase 2 1.8).

Never raises: every failure comes back as `(None, AgentRun(ok=False, error=…))`.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel, ValidationError

from pipeline.agents.providers.base import AgentProvider, ProviderOutput, build_env
from pipeline.agents.providers.children import CHILDREN_DIR
from pipeline.agents.roles import RoleSpec, role_spec
from pipeline.config import AGENT_ROLES
from pipeline.logging import get_logger, mask_file
from pipeline.pause import PAUSE_FILE, wait_while_paused
from pipeline.schemas import AgentRun

if TYPE_CHECKING:
    from pipeline.config import PipelineConfig, Secrets
    from pipeline.deps import Deps
    from pipeline.store import Store

M = TypeVar("M", bound=BaseModel)
AGENT_DIR = ".agent"
WRAP_UP_TURNS = 12  # turns granted to commit + write the result after hitting max_turns
FINAL_MESSAGE_ROLES = frozenset({"summary"})
MAX_SNAPSHOT_BYTES = 5_000_000


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------- result files ----------


def extract_json(text: str) -> str:
    """JSON object from an agent's final message: strips code fences and surrounding prose."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*\n(.*?)\n```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if 0 <= start < end else text


def read_result(path: Path, model: type[M]) -> tuple[M | None, list[str]]:
    if not path.exists():
        return None, [f"{path.name} was not written"]
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        return None, [f"{path.name} is not valid JSON: {err}"]
    try:
        return model.model_validate(data), []
    except ValidationError as err:
        return None, [
            f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}" for e in err.errors()
        ]


# ---------- read-only enforcement ----------


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    return proc.stdout if proc.returncode == 0 else ""


def _is_agent_path(rel: str) -> bool:
    return rel == AGENT_DIR or rel.startswith(f"{AGENT_DIR}/")


def _file_state(path: Path) -> bytes | None:
    """Content (or a hash for big files) of a path; None if it doesn't exist."""
    if not path.is_file():
        return None
    if path.stat().st_size > MAX_SNAPSHOT_BYTES:
        return b"sha256:" + hashlib.sha256(path.read_bytes()).hexdigest().encode()
    return path.read_bytes()


def _git_dirty(repo: Path) -> list[str]:
    out = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    paths: list[str] = []
    entries = out.split("\0")
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC":  # rename/copy: next entry is the source path
            if i < len(entries) and entries[i]:
                paths.append(entries[i])
            i += 1
    return [p for p in paths if not _is_agent_path(p)]


@dataclass
class Snapshot:
    """Dirty files per watched directory. Git repos: `git status`; other dirs: a file walk
    (skipping `.agent/` and nested git repos)."""

    git: dict[Path, dict[str, bytes | None]]
    plain: dict[Path, dict[str, bytes | None]]


def _watch_roots(dirs: list[Path]) -> tuple[list[Path], list[Path]]:
    git_roots: list[Path] = []
    plain_roots: list[Path] = []
    for d in dirs:
        if not d.is_dir():
            continue
        if (d / ".git").exists():
            git_roots.append(d)
            continue
        plain_roots.append(d)
        git_roots += [c for c in sorted(d.iterdir()) if c.is_dir() and (c / ".git").exists()]
    return git_roots, plain_roots


def _plain_files(root: Path) -> dict[str, bytes | None]:
    files: dict[str, bytes | None] = {}
    stack = [root]
    while stack:
        d = stack.pop()
        for child in d.iterdir():
            rel = child.relative_to(root).as_posix()
            if _is_agent_path(rel) or child.is_symlink():
                continue
            if child.is_dir():
                if not (child / ".git").exists():
                    stack.append(child)
            else:
                files[rel] = _file_state(child)
    return files


def take_snapshot(dirs: list[Path]) -> Snapshot:
    git_roots, plain_roots = _watch_roots(dirs)
    git = {r: {p: _file_state(r / p) for p in _git_dirty(r)} for r in git_roots}
    plain = {r: _plain_files(r) for r in plain_roots}
    return Snapshot(git, plain)


def changes_since(before: Snapshot, dirs: list[Path]) -> list[tuple[Path, str]]:
    """(root, relative path) of everything outside `.agent/` that changed since `before`."""
    after = take_snapshot(dirs)
    changed: list[tuple[Path, str]] = []
    for root, now in after.git.items():
        was = before.git.get(root, {})
        for p in sorted(set(now) | set(was)):
            state_now = now[p] if p in now else _file_state(root / p)
            if p not in was or was[p] != state_now:
                changed.append((root, p))
    for root, now in after.plain.items():
        was = before.plain.get(root, {})
        for p in sorted(set(now) | set(was)):
            if now.get(p) != was.get(p) or (p in now) != (p in was):
                changed.append((root, p))
    return changed


def revert(before: Snapshot, changes: list[tuple[Path, str]]) -> list[str]:
    """Undo read-only violations. Returns paths that could not be restored."""
    failed: list[str] = []
    for root, rel in changes:
        path = root / rel
        was = before.git.get(root, before.plain.get(root, {}))
        try:
            if rel in was:
                old = was[rel]
                if old is None:
                    path.unlink(missing_ok=True)
                elif old.startswith(b"sha256:"):
                    failed.append(str(path))
                else:
                    path.write_bytes(old)
            elif root in before.git and _git(root, "ls-files", "--error-unmatch", "--", rel):
                subprocess.run(
                    ["git", "restore", "--source=HEAD", "--staged", "--worktree", "--", rel],
                    cwd=root, capture_output=True, check=False,
                )  # fmt: skip
            elif root in before.git or path.is_file():
                path.unlink(missing_ok=True)
        except OSError:
            failed.append(str(path))
    return failed


# ---------- runner ----------


class AgentRunner:
    def __init__(
        self,
        config: PipelineConfig,
        secrets: Secrets,
        store: Store,
        home: Path,
        providers: dict[str, AgentProvider] | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        if providers is None:
            from pipeline.agents.providers import default_providers

            providers = default_providers()
        self.config = config
        self.secrets = secrets
        self.store = store
        self.home = home
        self.providers = providers
        self.clock = clock
        for provider in providers.values():  # processes that outlive the server (K18)
            if hasattr(provider, "children_dir"):
                provider.children_dir = home / CHILDREN_DIR

    # ----- public -----

    def run(
        self,
        key: str,
        role: str,
        cwd: Path,
        prompt: str,
        result_model: type[M],
        repo: str | None = None,
        extra_dirs: tuple[Path, ...] | list[Path] = (),
        provider_override: str | None = None,
        slot: str | None = None,
        prompt_vars: dict[str, str] | None = None,
        check: Callable[[M], list[str]] | None = None,
        check_error: str = "invalid_result",
        continue_session: bool = False,
        follow_up: str | None = None,
        session_scope: str | None = None,
        edit_in_place: bool = False,
        repair: Callable[[M], M] | None = None,
        protect: tuple[str, ...] = (),
    ) -> tuple[M | None, AgentRun]:
        """`protect`: more paths (relative to `cwd`) a writable role may not edit: a ticket-level
        coder protects each repo's `shared_paths` under the repo's folder.
        `edit_in_place`: the result file already holds the previous result; a role that can
        write files (not a final-message role) changes it with Edit calls instead of writing it
        again, and an unchanged file is rejected. `check`: the caller's own checks on a
        schema-valid result (problems, [] = fine). Any problems go back to the same session
        once, to fix in place; if they remain, the run fails with `check_error: <problems>`.
        `repair`: applied to the result of that follow-up before the final check (the last
        chance): the caller completes what the agent still left out, so a plan isn't lost for
        a gap the pipeline can close itself.
        `continue_session` (P9): continue this ticket's last successful session of the same
        role and repo with only `follow_up` (or `prompt`):
        it already has the role prompt and what it read. A new session with the full prompt when
        there is none, it was on another provider, or continuing fails. `session_scope` (e.g.
        the branch): a session is only continued within the same scope (a QA round's new
        branch starts fresh)."""
        try:
            spec = role_spec(self.config, role, provider_override, slot)
        except (KeyError, ValueError) as err:
            return None, self._stub_run(role, repo, slot, provider_override, f"invalid: {err}")
        if spec.writable:  # protected files can't be edited (Claude tools)
            protected = tuple(protect)
            if repo:
                with contextlib.suppress(KeyError, ValueError):
                    protected += tuple(self.config.repo(repo).shared_paths)
            if protected:
                spec = replace(spec, protected=protected)
        try:
            return self.execute(
                key, spec, Path(cwd), prompt, result_model, repo, tuple(map(Path, extra_dirs)),
                slot, prompt_vars=prompt_vars, check=check, check_error=check_error,
                continue_session=continue_session, follow_up=follow_up,
                session_scope=session_scope, edit_in_place=edit_in_place, repair=repair,
            )  # fmt: skip
        except Exception as err:  # never raise into the graph
            get_logger(ticket=key, role=role, repo=repo).exception("agent_runner_crashed")
            return None, self._stub_run(role, repo, slot, spec.provider, f"internal: {err}")

    def execute(
        self,
        key: str,
        spec: RoleSpec,
        cwd: Path,
        prompt: str,
        result_model: type[M],
        repo: str | None = None,
        extra_dirs: tuple[Path, ...] = (),
        slot: str | None = None,
        record: bool = True,
        prompt_vars: dict[str, str] | None = None,
        check: Callable[[M], list[str]] | None = None,
        check_error: str = "invalid_result",
        continue_session: bool = False,
        follow_up: str | None = None,
        session_scope: str | None = None,
        edit_in_place: bool = False,
        repair: Callable[[M], M] | None = None,
    ) -> tuple[M | None, AgentRun]:
        log = get_logger(ticket=key, role=spec.name, repo=repo, slot=slot)
        run_ctx = _RunCtx(key, repo, slot, cwd, extra_dirs, result_model, record)
        run_ctx.in_place = edit_in_place
        full_prompt = self._compose(spec, cwd, prompt, prompt_vars, self._in_place(run_ctx, spec))

        run_ctx.scope = session_scope
        run_ctx.in_place = edit_in_place
        saved = (
            self._session(key, spec, repo, session_scope) if continue_session and record else None
        )
        earlier = saved.get("id") if saved else None
        out, run = None, None
        if saved and earlier:
            parts = [(follow_up or prompt).strip(),
                     self._result_instruction(spec, cwd, self._in_place(run_ctx, spec))]
            if saved.get("role") not in (None, spec.name):  # e.g. a fix continues the coding
                parts.insert(0, spec.prompt_text(prompt_vars).strip())
            text = "\n\n---\n\n".join(p for p in parts if p)
            out, run = self._attempt(run_ctx, spec, text, resume_id=earlier,
                                     fresh_result=True)  # fmt: skip
            if out is None or (not run.ok and not out.limit_hit and not _hit_max_turns(run)):
                log.warning("session_continue_failed", error=run.error)  # expired, not found …
                if out is not None:
                    self._record(run_ctx, run)
                out, run = None, None
            else:
                log.info("session_continued", session=earlier[:12])
        if out is None:
            out, run = self._attempt(run_ctx, spec, full_prompt)
        assert run is not None
        if out is not None and out.limit_hit:
            fallback = self.config.agents.fallback_provider
            if fallback and fallback != spec.provider:
                log.warning("provider_limit_fallback", provider=spec.provider, fallback=fallback)
                self._record(run_ctx, run)
                spec = _with_provider(self.config, spec, fallback)
                full_prompt = self._compose(spec, cwd, prompt, prompt_vars,
                                            self._in_place(run_ctx, spec))  # fmt: skip
                out, run = self._attempt(run_ctx, spec, full_prompt)
            if out is not None and out.limit_hit:
                run = run.model_copy(update={"error": "provider_limit"})
        if out is not None and not run.ok and _hit_max_turns(run) and out.session_id:
            # Out of turns (often with the work done, DEMO-4): resume the same session briefly
            # to commit and hand in, instead of throwing the run away.
            log.warning("max_turns_wrap_up", turns=run.turns, wrap_up_turns=WRAP_UP_TURNS)
            self._record(run_ctx, run)
            out, run = self._attempt(run_ctx, replace(spec, max_turns=WRAP_UP_TURNS),
                                     _wrap_up_prompt(spec), resume_id=out.session_id)  # fmt: skip
        if out is None or not run.ok:
            self._record(run_ctx, run)
            log.warning("agent_run_failed", error=run.error, provider=run.provider)
            return None, run

        result, problems = self._collect(spec, run_ctx, out)
        problems = problems or self._unchanged(spec, run_ctx)
        rejected = bool(not problems and check and check(result))
        if rejected:
            problems = check(result)  # type: ignore[misc]
        if not problems:
            self._record(run_ctx, run)
            self._remember_session(run_ctx, spec, out)
            log.info("agent_run_ok", provider=run.provider, duration_s=run.duration_s)
            return result, run

        # One follow-up in the same session asking to fix the result file in place.
        error = check_error if rejected else "invalid_result"
        log.warning("agent_result_invalid", problems=problems[:10], error=error)
        self._record(run_ctx, run.model_copy(update={"ok": False, "error": error}))
        fix = self._fix_prompt(spec, result_model, problems, rejected)
        out, run = self._attempt(run_ctx, spec, fix, resume_id=out.session_id)
        if out is not None and run.ok:
            result, problems = self._collect(spec, run_ctx, out)
            problems = problems or self._unchanged(spec, run_ctx)
            if not problems and repair is not None:  # the last chance: the caller completes it
                result = repair(result)
            if not problems and check:
                problems = check(result)
                error = check_error
            if not problems:
                self._record(run_ctx, run)
                self._remember_session(run_ctx, spec, out)
                return result, run
            detail = f"{error}: " + "; ".join(problems) if error != "invalid_result" else error
            run = run.model_copy(update={"ok": False, "error": detail[:1000]})
        elif out is not None and out.limit_hit:
            run = run.model_copy(update={"error": "provider_limit"})
        self._record(run_ctx, run)
        return None, run

    # ----- internals -----

    # ----- sessions (P9) -----

    @staticmethod
    def _session_note(spec: RoleSpec, repo: str | None) -> str:
        """Roles of one family share a session per repo (coder_fix continues the coder's)."""
        family = AGENT_ROLES.get(spec.name) or spec.name
        return f"session:{family}:{repo or 'ticket'}"

    def _session(self, key: str, spec: RoleSpec, repo: str | None,
                 scope: str | None) -> dict | None:  # fmt: skip
        """The last successful session of this role family here: same provider, same scope."""
        saved = self.store.get_note(key, self._session_note(spec, repo)) or {}
        if saved.get("provider") != spec.provider or saved.get("scope") != scope:
            return None
        return saved

    def _remember_session(self, ctx: _RunCtx, spec: RoleSpec, out: ProviderOutput | None) -> None:
        if ctx.record and out is not None and out.session_id:
            self.store.set_note(ctx.key, self._session_note(spec, ctx.repo),
                                {"id": out.session_id, "provider": spec.provider,
                                 "role": spec.name, "scope": ctx.scope})  # fmt: skip

    def _attempt(
        self,
        ctx: _RunCtx,
        spec: RoleSpec,
        prompt: str,
        resume_id: str | None = None,
        fresh_result: bool = False,
    ) -> tuple[ProviderOutput | None, AgentRun]:
        """One provider run (not yet recorded). Checks pause/caps/availability first."""
        run_no = self.store.ticket_run_count(ctx.key) + 1 if ctx.record else 1
        base = AgentRun.model_validate(
            {"role": spec.name, "repo": ctx.repo, "slot": ctx.slot, "run_no": run_no,
             "provider": spec.provider, "auth_mode": spec.auth_mode, "model": spec.model,
             "ok": False, "duration_s": 0.0, "transcript_path": ""}
        )  # fmt: skip
        blocked = self._blocked(ctx, spec.name)
        if blocked:
            return None, base.model_copy(update={"error": blocked})
        provider = self.providers.get(spec.provider)
        if provider is None:
            return None, base.model_copy(update={"error": "provider_unavailable"})
        ok, detail = provider.available()
        if not ok:
            return None, base.model_copy(update={"error": f"provider_unavailable: {detail}"})

        label = ctx.repo or "ticket"
        transcript = (
            self.home / "runs" / ctx.key / f"{spec.name}-{label}-{run_no}-{spec.provider}.jsonl"
        )
        env = build_env(spec.provider, spec.auth_mode, self.secrets)
        watch = [ctx.cwd, *ctx.extra_dirs]
        before = None if spec.writable else take_snapshot(watch)
        if resume_id is None or fresh_result:  # a continued session's new task: no stale result
            result_path = ctx.cwd / AGENT_DIR / spec.result_file
            if self._in_place(ctx, spec) and result_path.is_file():
                ctx.seed = result_path.read_bytes()  # kept: the agent edits it in place
            else:
                ctx.seed = None
                result_path.unlink(missing_ok=True)

        started = time.monotonic()
        out = provider.run(
            spec, ctx.cwd, prompt, ctx.extra_dirs, env, spec.timeout_s, transcript, resume_id
        )
        duration = round(time.monotonic() - started, 2)

        error = out.error if not out.ok else None
        if before is not None:
            changes = changes_since(before, watch)
            if changes:
                failed = revert(before, changes)
                listed = ", ".join(sorted({str(r / p) for r, p in changes})[:10])
                get_logger(ticket=ctx.key, role=spec.name).error(
                    "read_only_violation", changed=listed, not_restored=failed
                )
                out.ok, error = False, "read_only_violation"
                out.limit_hit = False

        for written in (
            transcript,
            transcript.with_suffix(".last.txt"),
            transcript.with_suffix(".stderr.log"),
        ):  # 1.8: secrets never stay on disk
            mask_file(written)
        cost = out.cost_usd if spec.auth_mode == "api_key" else None
        if out.cost_usd is not None and cost is None:
            get_logger(ticket=ctx.key, role=spec.name).info(
                "subscription_cost_estimate", estimate_usd=out.cost_usd
            )
        run = base.model_copy(
            update={"ok": out.ok, "error": error, "turns": out.turns, "cost_usd": cost,
                    "input_tokens": out.input_tokens, "output_tokens": out.output_tokens,
                    "duration_s": duration, "model": out.model or spec.model,
                    "transcript_path": str(transcript)}
        )  # fmt: skip
        return out, run

    def _blocked(self, ctx: _RunCtx, role: str = "") -> str | None:
        if (self.home / PAUSE_FILE).exists():  # 1.1: wait for `unpause` (a stop ends it)
            row = self.store.get_ticket(ctx.key) or {}
            wait_while_paused(self.home, lambda: str(
                (self.store.get_ticket(ctx.key) or row).get("halted_reason") or ""
            ).startswith("stop_requested"))  # fmt: skip
            if (self.home / PAUSE_FILE).exists():
                return "paused"
        return None

    def _collect(self, spec: RoleSpec, ctx: _RunCtx, out: ProviderOutput) -> tuple[Any, list[str]]:
        path = ctx.cwd / AGENT_DIR / spec.result_file
        if _final_message_mode(spec) and out.final_message:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(extract_json(out.final_message))
        return read_result(path, ctx.result_model)

    @staticmethod
    def _in_place(ctx: _RunCtx, spec: RoleSpec) -> bool:
        """Edit the existing result file instead of writing a new one: only a role that writes
        files (a final-message role, e.g. Codex read-only, returns the whole JSON instead)."""
        return ctx.in_place and not _final_message_mode(spec)

    @staticmethod
    def _unchanged(spec: RoleSpec, ctx: _RunCtx) -> list[str]:
        """An in-place run that left the result file as it found it did nothing."""
        if ctx.seed is None:
            return []
        try:
            same = (ctx.cwd / AGENT_DIR / spec.result_file).read_bytes() == ctx.seed
        except OSError:
            return []
        if not same:
            return []
        return [f"`.agent/{spec.result_file}` is unchanged: it still holds the previous result. "
                "Apply the requested changes to it with Edit calls."]

    def _compose(
        self, spec: RoleSpec, cwd: Path, prompt: str, prompt_vars: dict[str, str] | None = None,
        in_place: bool = False,
    ) -> str:
        parts = [p for p in (spec.prompt_text(prompt_vars).strip(), prompt.strip()) if p]
        parts.append(self._result_instruction(spec, cwd, in_place))
        return "\n\n---\n\n".join(parts)

    @staticmethod
    def _result_instruction(spec: RoleSpec, cwd: Path, in_place: bool = False) -> str:
        if in_place:
            return (
                "## How to return your result\n\n"
                f"`.agent/{spec.result_file}` (i.e. `{cwd / AGENT_DIR / spec.result_file}`) "
                "already holds the **previous** result. Do not write the file again: change it "
                "in place with Edit calls, only the parts that change (plus the fields this "
                "mode asks you to update). It must stay one valid JSON object in the same "
                "format, and it must differ from what it holds now. This overrides any "
                "instruction above to write the whole file."
            )
        if _final_message_mode(spec):
            return (
                "## How to return your result\n\n"
                "Do not write any files (on Codex you are in a read-only sandbox). This "
                f"overrides any instruction above to write `.agent/{spec.result_file}` yourself: "
                "when you are done, reply with ONLY the JSON object as your final message (no "
                "prose, no code "
                f"fences). The orchestrator saves it as `.agent/{spec.result_file}`."
            )
        return (
            "## How to return your result\n\n"
            f"Write your result as a single JSON object to `.agent/{spec.result_file}` "
            f"(i.e. `{cwd / AGENT_DIR / spec.result_file}`). The file must contain only JSON."
        )

    @staticmethod
    def _fix_prompt(
        spec: RoleSpec, model: type[BaseModel], problems: list[str], rejected: bool = False
    ) -> str:
        where = (
            "Reply with ONLY the corrected JSON object as your final message."
            if _final_message_mode(spec)
            else f"Correct `.agent/{spec.result_file}` in place with the Edit tool: change only "
            "what these problems need; don't write the whole file again."
        )
        listed = "\n".join(f"- {p}" for p in problems[:30])
        if rejected:  # schema-valid, but the orchestrator's own checks failed
            return (
                f"The orchestrator rejected your result `.agent/{spec.result_file}`:\n\n{listed}"
                f"\n\nFix only these problems; keep everything else the same. {where}"
            )
        schema = json.dumps(model.model_json_schema(), indent=1)
        return (
            f"Your result `.agent/{spec.result_file}` failed validation:\n\n{listed}\n\n"
            f"Fix only these problems; keep the content otherwise the same. {where}\n\n"
            f"JSON Schema of the result:\n\n```json\n{schema}\n```"
        )

    def _record(self, ctx: _RunCtx, run: AgentRun) -> None:
        if ctx.record and run.transcript_path:  # blocked runs never started: no row
            self.store.record_agent_run(ctx.key, run, started_at=self.clock())

    @staticmethod
    def _stub_run(
        role: str, repo: str | None, slot: str | None, provider: str | None, error: str
    ) -> AgentRun:
        return AgentRun.model_validate(
            {"role": role, "repo": repo, "slot": slot, "run_no": 0,
             "provider": provider if provider in ("claude", "codex", "fake") else "fake",
             "auth_mode": "none", "model": None, "ok": False, "duration_s": 0.0,
             "transcript_path": "", "error": error}
        )  # fmt: skip


@dataclass
class _RunCtx:
    key: str
    repo: str | None
    slot: str | None
    cwd: Path
    extra_dirs: tuple[Path, ...]
    result_model: type[BaseModel]
    record: bool
    scope: str | None = None  # P9: where a remembered session may be continued (e.g. the branch)
    in_place: bool = False  # the result file is edited in place (planner, non-initial modes)
    seed: bytes | None = None  # what that file held before the run


def _hit_max_turns(run: AgentRun) -> bool:
    return "max_turns" in (run.error or "")


def _wrap_up_prompt(spec: RoleSpec) -> str:
    """The resume prompt after a run hit its turn limit: hand in, nothing else."""
    steps = [
        "You reached the turn limit of this run. Stop exploring, investigating and polishing now; "
        f"you have at most {WRAP_UP_TURNS} more turns. Do only this:",
    ]
    if spec.writable:
        steps.append(
            "1. If your changes are not committed yet: `git add` the files you changed (never "
            "`.agent/`) and commit them with the usual message. Do not push."
        )
    steps.append(
        f"{len(steps)}. Write `.agent/{spec.result_file}` as described in your instructions. "
        "If the task is complete and the checks passed, report success; otherwise report what "
        "is done and what is left (a failed / needs-clarification status, with details)."
    )
    return "\n\n".join(steps)


def _final_message_mode(spec: RoleSpec) -> bool:
    """Codex read-only roles return the JSON as the final message (Phase 2 1.9); so does the
    `summary` role on any provider (no file write = one turn less)."""
    return spec.name in FINAL_MESSAGE_ROLES or (spec.provider == "codex" and not spec.writable)


def _with_provider(config: PipelineConfig, spec: RoleSpec, provider: str) -> RoleSpec:
    from dataclasses import replace

    auth = config.agents.providers.get(provider)  # type: ignore[call-overload]
    # A role's model name belongs to its configured provider; the fallback uses its default.
    return replace(
        spec, provider=provider, model=None, effort=None,
        auth_mode=auth.auth if auth else "subscription",
    )  # fmt: skip


def run_agent(
    key: str,
    role: str,
    cwd: Path,
    prompt: str,
    result_model: type[M],
    deps: Deps,
    repo: str | None = None,
    extra_dirs: tuple[Path, ...] | list[Path] = (),
    provider_override: str | None = None,
    slot: str | None = None,
    prompt_vars: dict[str, str] | None = None,
) -> tuple[M | None, AgentRun]:
    """Phase 2 1.8 entry point; uses `deps.runner` (an `AgentRunner`)."""
    return deps.runner.run(
        key, role, cwd, prompt, result_model, repo, extra_dirs, provider_override, slot,
        prompt_vars=prompt_vars,
    )  # fmt: skip
