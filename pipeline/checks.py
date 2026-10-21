"""`pipeline check`: integration and environment health checks (no agent runs; those are P2)."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pipeline.config import API_KEY_ENV, PipelineConfig, Secrets, check_against_secrets
from pipeline.deps import Deps

Status = Literal["ok", "warn", "fail", "skip"]
ICON = {"ok": "✅", "warn": "⚠️", "fail": "❌", "skip": "⏭"}

DEVELOPER = 30  # GitLab access level needed to push / approve


@dataclass
class CheckResult:
    area: str
    name: str
    status: Status
    detail: str = ""


def _run(area: str, name: str, fn: Callable[[], tuple[Status, str]]) -> CheckResult:
    try:
        status, detail = fn()
    except Exception as err:  # every check reports, none aborts the table
        status, detail = "fail", str(err).splitlines()[0][:300]
    return CheckResult(area, name, status, detail)


def run_checks(deps: Deps, spec_url: str | None = None) -> list[CheckResult]:
    cfg, secrets = deps.config, deps.secrets
    results: list[CheckResult] = []
    add = results.append

    # ---------- config / local ----------
    def config_check() -> tuple[Status, str]:
        problems = check_against_secrets(cfg, secrets)
        if problems:
            return "fail", "; ".join(problems)
        order = " → ".join(cfg.repo_order(cfg.repo_names))
        return (
            "ok",
            f"{len(cfg.repos)} repos ({order}); providers {sorted(cfg.enabled_providers())}",
        )

    add(_run("Config", "pipeline.config.yaml + .env", config_check))

    def db_check() -> tuple[Status, str]:
        deps.store.init()
        deps.store.record_event("CHECK-0", "resume", data={"check": True})
        deps.store.delete_ticket("CHECK-0")
        return "ok", f"{deps.store.url} (journal {deps.store.journal_mode()})"

    add(_run("Local", "Tracking DB writable", db_check))
    add(CheckResult("Local", "Docker", "skip", "not needed: no sanity stack (D22)"))

    # ---------- Jira ----------
    jira = deps.jira
    add(_run("Jira", "Auth", lambda: ("ok", f"as {jira.whoami()}")))  # type: ignore[attr-defined]
    add(
        _run(
            "Jira",
            f"Project {cfg.jira.project}",
            lambda: ("ok", jira.project_name(cfg.jira.project)),  # type: ignore[attr-defined]
        )
    )

    def fields_check() -> tuple[Status, str]:
        wanted = [f for f in (cfg.jira.repos_field, cfg.jira.spec_field) if f]
        if not wanted:  # D202: `implement` asks for the spec link and the repos
            return "skip", "not used: `implement` asks for the tech spec link and the repositories"
        names = jira.field_names()  # type: ignore[attr-defined]
        missing = [f for f in wanted if f not in names]
        if missing:
            return "fail", f"field(s) not found: {missing}"
        return "ok", ", ".join(f"{f} ({names[f]})" for f in wanted)

    add(_run("Jira", "Custom fields", fields_check))

    # ---------- GitLab ----------
    gl = deps.gitlab
    users: dict[str, str] = {}
    for role in ("coder", "reviewer"):

        def whoami(role: str = role) -> tuple[Status, str]:
            users[role] = gl.whoami(role)  # type: ignore[attr-defined]
            return "ok", f"as {users[role]}"

        add(_run("GitLab", f"{role.capitalize()} auth", whoami))
    if len(set(users.values())) == 1 and len(users) == 2:
        if cfg.git.allow_same_gitlab_identity:
            add(CheckResult("GitLab", "Identities", "warn",
                            "coder and reviewer are the same user (allowed in the config)"))
        else:
            add(CheckResult("GitLab", "Identities", "fail", "coder and reviewer are the same user"))

    for repo in cfg.repos:
        for role in ("coder", "reviewer"):

            def access(repo=repo, role=role) -> tuple[Status, str]:
                level = gl.access_level(repo.gitlab_project, role)  # type: ignore[attr-defined]
                if level < DEVELOPER:
                    return "fail", f"access level {level}; needs ≥ {DEVELOPER} (Developer)"
                return "ok", f"access level {level}"

            add(_run("GitLab", f"{repo.name}: {role}", access))

        def branch(repo=repo) -> tuple[Status, str]:
            ok = gl.branch_exists(repo.gitlab_project, repo.default_branch)  # type: ignore[attr-defined]
            return ("ok", "exists") if ok else ("fail", "not found on GitLab")

        add(_run("GitLab", f"{repo.name}: branch {repo.default_branch}", branch))

    # ---------- Google ----------
    local_specs = cfg.spec.source == "local"
    if local_specs:
        def folder_check() -> tuple[Status, str]:
            from pipeline.intake import local_options

            options, why = local_options(cfg)
            return ("ok", f"{len(options)} document(s) in the tech spec folder") if options \
                else ("warn", why)  # fmt: skip

        add(_run("Tech spec", "Local folder", folder_check))

    def google_check() -> tuple[Status, str]:
        if local_specs and not spec_url:
            return "skip", "tech specs are local (spec.source: local); Google is not used"
        if not secrets.google_oauth_client_path or not secrets.google_oauth_client_path.exists():
            return "fail", f"OAuth client file missing: {secrets.google_oauth_client_path}"
        if not spec_url:
            token = secrets.google_token_path
            state = "token cached" if token and token.exists() else "no token yet (browser login)"
            return "warn", f"{state}; pass --spec-url <Google Doc URL> to test a fetch"
        md, digest = deps.spec.fetch(spec_url)
        return "ok", f"{len(md.splitlines())} lines, sha {digest[:10]}"

    add(_run("Google", "Spec fetch", google_check))

    # ---------- agent providers ----------
    for provider in sorted(cfg.enabled_providers()):
        results.extend(provider_checks(cfg, secrets, provider))
    return results


def provider_checks(cfg: PipelineConfig, secrets: Secrets, provider: str) -> list[CheckResult]:
    out = []
    path = shutil.which(provider)
    out.append(
        CheckResult(
            "Agents",
            f"{provider} CLI",
            "ok" if path else "fail",
            path or f"`{provider}` not found on PATH",
        )
    )
    auth = cfg.agents.providers[provider].auth  # type: ignore[index]
    key_name = API_KEY_ENV[provider].upper()
    has_key = bool(getattr(secrets, API_KEY_ENV[provider]) or os.environ.get(key_name))
    if auth == "api_key":
        status: Status = "ok" if has_key else "fail"
        detail = f"api_key: {key_name} {'set' if has_key else 'missing'}"
    else:
        detail = "subscription"
        status = "ok"
        if has_key:
            status, detail = "warn", f"subscription; {key_name} is set but will be removed for runs"
        if provider == "claude" and not secrets.claude_code_oauth_token:
            detail += "; CLAUDE_CODE_OAUTH_TOKEN not set (needed for headless runs)"
            status = "warn"
    out.append(CheckResult("Agents", f"{provider} auth", status, detail))
    return out


# ---------- agent smoke runs (`pipeline check --agents`) ----------

SMOKE_PROMPT = (
    'Write exactly {"ok": true} to the file .agent/result.json in your working directory. '
    "Do nothing else."
)


def agent_checks(deps: Deps) -> list[CheckResult]:
    """A tiny real run per enabled provider in a temp dir (read-only role, so the Codex
    final-message path of 1.9 is exercised too). Not recorded in agent_runs."""
    import tempfile
    from pathlib import Path

    from pydantic import BaseModel

    from pipeline.agents.roles import RoleSpec

    class Smoke(BaseModel):
        ok: bool

    results = []
    for provider in sorted(deps.config.enabled_providers()):
        auth = deps.config.agents.providers[provider].auth  # type: ignore[index]
        # Smoke-test the model/effort the pipeline's roles really use on this provider.
        agents = deps.config.agents
        role = next((r for r in ("coder", "planner", "reviewer", "deployment", "qa_notes")
                     if agents.provider_for(r) == provider), None)  # fmt: skip
        rc = agents.role_config(role) if role else None
        spec = RoleSpec(
            name="check", provider=provider, model=rc.model if rc else None, writable=False,
            max_turns=5, timeout_min=3, prompt_file=None, result_file="result.json",
            auth_mode=auth, effort=rc.effort if rc else None,
        )  # fmt: skip

        def smoke(spec: RoleSpec = spec, auth: str = auth) -> tuple[Status, str]:
            with tempfile.TemporaryDirectory(prefix="pipeline-check-") as tmp:
                result, run = deps.runner.execute(
                    "CHECK-0", spec, Path(tmp).resolve(), SMOKE_PROMPT, Smoke, record=False
                )
            if result is None or not result.ok:
                return "fail", f"{auth}: {run.error}"
            tokens = f", {run.input_tokens}→{run.output_tokens} tokens" if run.input_tokens else ""
            used = run.model or spec.model or "default model"
            effort = f" ({spec.effort})" if spec.effort else ""
            return "ok", f"{auth}; {used}{effort}; {run.duration_s:.0f}s{tokens}"

        results.append(_run("Agents", f"{provider} run", smoke))
    return results


def chat_llm_checks(deps: Deps) -> list[CheckResult]:
    """One tiny `summary` agent run (D209): which provider and model, and how long it takes."""
    from pipeline.chat.llm import LLMError, build_chat_llm

    cfg = deps.config.chat_llm
    chain = deps.chat_llm or build_chat_llm(deps.config, deps.secrets, deps.store, deps.runner,
                                            deps.home)
    results = []
    for provider in chain.providers:

        def call(provider: Any = provider) -> tuple[Status, str]:
            try:
                reply = provider.complete(
                    "", [{"role": "user", "content": "## Speaker\n\nCoder\n\n## Event\n\n"
                          "say hello in one short sentence\n\n## Facts\n\n- ticket: CHECK-0"}],
                    None, 50, cfg.timeout_s, "check",
                )  # fmt: skip
            except LLMError as err:
                return "fail", str(err)[:200]
            tokens = reply.usage.get("input_tokens"), reply.usage.get("output_tokens")
            return (
                "ok",
                f"{reply.provider} {reply.model}: {reply.latency_s:.1f}s, tokens in/out "
                f"{tokens[0]}/{tokens[1]}",
            )

        results.append(_run("Summary agent", provider.name, call))
    if not results:
        results.append(CheckResult("Summary agent", "provider", "warn", "chat_llm.provider: none"))
    return results
