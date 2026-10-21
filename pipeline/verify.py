"""Orchestrator-side verification of a coder's work (Phase 3 §6, §9.5).

Per repo, from config: install (only if needed), lint (`{files}` = changed files, D21), test,
build, extra checks. Plus: secret scan on added lines, diff size, forbidden paths, and cheap
contract checks (warnings only). No sanity step (D34). Commands run through an injectable
`CommandRunner` so tests use fake commands.
"""

from __future__ import annotations

import fnmatch
import re
import shlex
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from pipeline.integrations.base import IntegrationError
from pipeline.logging import get_logger
from pipeline.safe_env import command_env
from pipeline.schemas import ImplementationPlan, VerifyResult

if TYPE_CHECKING:
    from pipeline.config import PipelineConfig
    from pipeline.workspace import Workspaces

CommandRunner = Callable[[str, Path, float], tuple[int, str]]  # (cmd, cwd, timeout) → (rc, out)
TAIL_LINES = 80
LINT_EXTS = (".js", ".cjs", ".mjs", ".ts", ".tsx", ".jsx", ".vue")
LOCK_FILES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml")
INSTALL_TRIGGERS = ("package.json", *LOCK_FILES)
ALLOWED_ENV_FILES = frozenset({".env.example"})
WARNING = "warning: "
# Checks that run the repo's tests: re-run once before failing (not install/lint: deterministic).
RERUN_ON_FAILURE = frozenset(
    {"test", "build", "coverage:check", "coverage", "test:api", "test:unit"}
)

SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitLab token", re.compile(r"\bglpat-[0-9A-Za-z_-]{20,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{30,}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[0-9A-Za-z-]{10,}")),
    ("API key (sk-)", re.compile(r"\bsk-(?:ant-|proj-)?[0-9A-Za-z_-]{20,}")),
    ("JWT", re.compile(r"\beyJ[0-9A-Za-z_-]{10,}\.eyJ[0-9A-Za-z_-]{10,}\.[0-9A-Za-z_-]{10,}")),
    (
        "hard-coded credential",
        re.compile(
            r"""(?i)\b[\w-]*(?:secret|passw(?:or)?d|api[_-]?key|access[_-]?token|auth[_-]?token)"""
            r"""[\w-]*\s*[:=]\s*['"][^'"\s$<{]{8,}['"]"""
        ),
    ),
]


def shell_runner(cmd: str, cwd: Path, timeout_s: float,
                 env: Mapping[str, str] | None = None) -> tuple[int, str]:  # fmt: skip
    """Run a repo command with the allowlisted environment (P9, audit F4: never the pipeline's
    credentials; `safe_env.command_env`)."""
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout_s,
            check=False, env=dict(env) if env is not None else command_env(),
        )  # fmt: skip
    except subprocess.TimeoutExpired as err:
        out = (err.stdout or b"").decode() if isinstance(err.stdout, bytes) else err.stdout or ""
        return 124, f"{out}\n[timed out after {timeout_s:.0f}s]"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def tail(text: str, n: int = TAIL_LINES) -> str:
    return "\n".join(text.rstrip().splitlines()[-n:])


# Lines that say *why* a command failed; the end of the output is often just a report (e.g. nyc's
# coverage table after mocha's "N failing" block), which hid the failing test (DEMO-4).
_FAILING_BLOCK = re.compile(r"^\s*\d+ failing\s*$")
_ERROR_LINE = re.compile(
    r"(?i)\b(error|failed|failing|does not meet|cannot find|not found|ECONNREFUSED|timed out)\b"
    r"|ERR!"
)
EXCERPT_LINES = 60


def failure_output(text: str, n: int = TAIL_LINES) -> str:
    """What to keep of a failed command: mocha's failing block (or else the first error lines),
    then the last lines. Short output is kept whole."""
    lines = text.rstrip().splitlines()
    if len(lines) <= n:
        return "\n".join(lines)
    start = next((i for i, ln in enumerate(lines) if _FAILING_BLOCK.match(ln)), None)
    if start is not None:
        head = lines[start : start + EXCERPT_LINES]
    else:
        head = [ln for ln in lines[:-30] if _ERROR_LINE.search(ln)][:20]
    last = lines[-30:]
    if not head:
        return "\n".join(lines[-n:])
    return "\n".join([*head, "…", *last])


def forbidden(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    if path == ".gitlab-ci.yml" or path.startswith(".gitlab-ci/"):
        return True
    # P9 (DEMO-9): the pipeline writes the deployment note (Jira); it never goes into a repo
    if name.upper().startswith(("DEPLOYMENT_NOTE", "DEPLOYMENT_TEMPLATE")):
        return True
    return name.startswith(".env") and name not in ALLOWED_ENV_FILES


def shared(path: str, patterns: list[str]) -> bool:
    """Is `path` under one of the repo's `shared_paths` (dir ending "/" or a glob)?"""
    for pat in patterns:
        if pat.endswith("/") and path.startswith(pat.lstrip("./")):
            return True
        if not pat.endswith("/") and fnmatch.fnmatch(path, pat.lstrip("./")):
            return True
    return False


SHARED_HINT = ("existing shared code is off-limits: revert it; if a criterion or finding can only "
               "be met there, report it (\"shared code: …\") instead")


def added_lines(diff: str) -> list[tuple[str, str]]:
    """(file, line) for every added line of a unified diff."""
    out: list[tuple[str, str]] = []
    current = ""
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[4:].removeprefix("b/")
        elif line.startswith("+") and not line.startswith("+++"):
            out.append((current, line[1:]))
    return out


def scan_secrets(diff: str) -> list[str]:
    hits: list[str] = []
    for file, line in added_lines(diff):
        for label, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                hits.append(f"possible {label} added in {file}")
                break
    return sorted(set(hits))


# test files (the Tester leaves them out of the source diff it reads)
_TEST_PATH = re.compile(r"(^|/)(test|tests|__tests__|spec)/|\.(spec|test)\.[cm]?[jt]sx?$", re.I)



def _path_probe(path: str) -> str | None:
    """Most specific static segment of an endpoint path (`/api/x/v1/configs/:id` → `configs`)."""
    static = [s for s in path.split("/") if s and not s.startswith(":") and "{" not in s]
    return static[-1] if static else None


def contract_warnings(
    repo: str, role: str, plan: ImplementationPlan | None, diff: str
) -> list[str]:
    """Cheap text checks: the endpoints this repo provides / calls should show up in its diff."""
    if plan is None:
        return []
    task = next((t for t in plan.tasks if t.repo == repo), None)
    if task is None:
        return []
    added = "\n".join(line for _, line in added_lines(diff))
    internal = {e.id: e for e in plan.contract.internal_endpoints}
    public = {e.id: e for e in plan.contract.public_endpoints}
    checks: list[tuple[str, str, str]] = []  # (endpoint id, path, what)
    for i in task.implements:
        ep = internal.get(i) or public.get(i)
        if ep is not None and getattr(ep, "is_new", True):
            checks.append((i, ep.path, "implements"))
    if role == "gateway":
        for pe in (public[i] for i in task.implements if i in public):
            checks += [(c, internal[c].path, "calls") for c in pe.calls if c in internal]
    for i in task.consumes:
        if i in internal:
            checks.append((i, internal[i].path, "calls"))
    warnings = []
    for ep_id, path, what in dict.fromkeys(checks):
        probe = _path_probe(path)
        if probe and probe not in added:
            warnings.append(
                f"{WARNING}{repo} {what} {ep_id} ({path}) but '{probe}' is not in its diff"
            )
    return warnings


class Verifier:
    def __init__(
        self,
        config: PipelineConfig,
        workspaces: Workspaces,
        run_cmd: CommandRunner = shell_runner,
        timeout_s: float = 1800,
    ) -> None:
        self.config = config
        self.workspaces = workspaces
        if run_cmd is shell_runner:  # the configured passthrough on top of the allowlist (P9)
            extra = tuple(config.verify.env_passthrough)
            run_cmd = lambda cmd, cwd, t: shell_runner(cmd, cwd, t, command_env(extra))  # noqa: E731
        self.run_cmd = run_cmd
        self.timeout_s = timeout_s

    def install(self, key: str, repo: str) -> str:
        """Install the repo's dependencies before a coder starts when `node_modules` is missing
        (D129): the coder then doesn't find out mid-run. Never raises; returns what happened."""
        wt = self.workspaces.worktree_path(key, repo)
        if (wt / "node_modules").exists() or not (wt / "package.json").exists():
            return "not needed"
        if self.workspaces.seed_node_modules(key, repo):  # same lock file installed before (K19)
            return "copied from the cache"
        cmd = self.config.repo(repo).commands.install
        rc, out = self.run_cmd(cmd, wt, self.timeout_s)
        if rc != 0:
            get_logger(ticket=key, repo=repo).warning("install_failed", cmd=cmd, tail=tail(out))
            return f"failed: {cmd} (exit {rc})"
        self.workspaces.save_node_modules(repo, wt)
        return f"installed: {cmd}"

    def warm(self, repo: str) -> str:
        """`pipeline warm` (4.3): fetch the repo and install the default branch's dependencies
        into the cache, so the first ticket's worktrees start without an `npm ci`."""
        path = self.workspaces.warm_checkout(repo)
        try:
            cache = self.workspaces.deps_cache(repo, path)
            if cache is None:
                return "no lock file: nothing to install"
            if cache.is_dir():
                return "already cached"
            cmd = self.config.repo(repo).commands.install
            rc, out = self.run_cmd(cmd, path, self.timeout_s)
            if rc != 0:
                return f"failed: {cmd} (exit {rc})\n{tail(out, 15)}"
            self.workspaces.save_node_modules(repo, path)
            return f"installed and cached: {cmd}"
        finally:
            self.workspaces.drop_warm_checkout(repo)

    def verify(self, key: str, repo: str, plan: ImplementationPlan | None = None) -> VerifyResult:
        cfg = self.config.repo(repo)
        wt = self.workspaces.worktree_path(key, repo)
        self.workspaces.ensure_env(key, repo)  # same env as the coder had (D69)
        checks: dict[str, str] = {}
        output: dict[str, str] = {}
        problems: list[str] = []

        try:
            head = self.workspaces.head(wt)  # P9: the commit these results belong to
            changed = self.workspaces.changed_paths(wt, cfg.default_branch)["vs_default"]
            diff = self.workspaces.diff_vs_default(wt, cfg.default_branch)
        except IntegrationError as err:
            return VerifyResult(repo=repo, ok=False, checks={"git": "failed"},
                                problems=[f"cannot read the diff: {err}"])  # fmt: skip

        if not changed:
            checks["changes"] = "failed"
            problems.append("no changes compared with the base branch")

        bad = [p for p in changed if forbidden(p)]
        checks["forbidden_paths"] = "failed" if bad else "passed"
        if bad:
            problems.append(f"forbidden files changed: {', '.join(bad)}")

        # P9 (DEMO-10): coders never edit existing shared code (middleware, utils …)
        if cfg.shared_paths:
            touched = [p for p in changed if shared(p, cfg.shared_paths)]
            try:
                edited = self.workspaces.existing_on_default(wt, cfg.default_branch, touched)
            except IntegrationError:
                edited = touched
            checks["shared_code"] = "failed" if edited else "passed"
            if edited:
                problems.append(f"shared code changed: {', '.join(edited)} ({SHARED_HINT})")

        secrets = scan_secrets(diff)
        checks["secrets"] = "failed" if secrets else "passed"
        problems += secrets

        c = cfg.commands
        needs_install = not (wt / "node_modules").exists() or any(
            p.rsplit("/", 1)[-1] in INSTALL_TRIGGERS for p in changed
        )
        steps: list[tuple[str, str | None]] = []
        if needs_install:
            steps.append(("install", c.install))
        lint_files = [p for p in changed if p.endswith(LINT_EXTS) and (wt / p).exists()]
        if "{files}" in c.lint:
            if lint_files:
                files = " ".join(shlex.quote(p) for p in lint_files)
                steps.append(("lint", c.lint.replace("{files}", files)))
            else:
                checks["lint"] = "skipped"
        else:
            steps.append(("lint", c.lint))
        steps += [("test", c.test), ("build", c.build)]
        steps += [(cmd.removeprefix("npm run "), cmd) for cmd in c.extra]

        for name, cmd in steps:
            if not cmd:
                continue
            if checks.get("install") == "failed":
                checks[name] = "skipped"
                continue
            rc, out = self.run_cmd(cmd, wt, self.timeout_s)
            if rc != 0 and name in RERUN_ON_FAILURE and rc != 124:
                # Flake guard: one re-run. A pass there = a flaky check, not the coder's problem
                # (DEMO-4: random failures in untouched tests cost a coder attempt).
                first = failure_output(out)
                rc, out = self.run_cmd(cmd, wt, self.timeout_s)
                if rc == 0:
                    problems.append(f"{WARNING}{name} failed once and passed when re-run (flaky)"
                                    f":\n{first}")  # fmt: skip
            checks[name] = "passed" if rc == 0 else "failed"
            output[name] = tail(out) if rc == 0 else failure_output(out)
            if rc != 0:
                problems.append(f"{name} failed (exit {rc}): `{cmd}`")

        problems += contract_warnings(repo, cfg.role, plan, diff)
        ok = all(v != "failed" for v in checks.values())
        return VerifyResult(repo=repo, ok=ok, checks=checks, output_tail=output,  # type: ignore[arg-type]
                            problems=problems, head_sha=head)  # fmt: skip


def failures_md(result: VerifyResult) -> str:
    """What the coder gets on a retry (`.agent/verify_failures.md`)."""
    lines = [f"# Orchestrator checks failed for {result.repo}", ""]
    lines += [f"- {p}" for p in result.problems if not p.startswith(WARNING)]
    for name, status in result.checks.items():
        if status == "failed" and result.output_tail.get(name):
            lines += ["", f"## {name} (last lines)", "", "```", result.output_tail[name], "```"]
    return "\n".join(lines) + "\n"
