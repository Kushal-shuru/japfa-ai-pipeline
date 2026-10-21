"""The ticket's apps as local processes for the live tests (P10). One ticket at a time.

`Stack.start()`:

1. a private `redis-server` (`redis_local`: free port, TLS, a generated password);
2. the fixture server for external services (`mock_server`);
3. each repo with a `stack` block, services before the gateway, from the ticket's worktree:
   `node src/app.js` (or `stack.start`) in its own process group, with this environment:
   the allowlisted base (`safe_env`, never the pipeline's credentials) + the developer clone's
   `.env` (`stack.env_from`: the dev DB) + `stack.env` (templates filled: the local Redis, the
   other repos' local URLs, the fixture server, generated secrets) + its port and
   `PGAPPNAME=aipl_test_<KEY>_<run>`. Then it waits for the health endpoint.

The apps load their own `.env` with dotenv, which never overrides a variable that is already
set, so the stack's values win. (ws2 also loads `.env.local` *with* override: a worktree that
has one is reported, since it would win.)
"""

from __future__ import annotations

import contextlib
import os
import secrets
import shlex
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from pipeline.agents.providers.children import CHILDREN_DIR, register, unregister
from pipeline.logging import add_secret_values, get_logger
from pipeline.safe_env import command_env
from pipeline.testing.db import DbParams, params_from_env
from pipeline.testing.envfile import date_variables, expand_path, read_env_file, render_value
from pipeline.testing.mock_server import External, ExternalsServer, load_fixtures
from pipeline.testing.redis_local import LocalRedis, free_port

SECRET_HINT = ("PASSWORD", "SECRET", "TOKEN", "KEY", "CREDENTIAL", "PRIVATE")


class StackError(RuntimeError):
    """The stack couldn't start. `repo` names the app (None: Redis, fixtures, config);
    `log` holds the last lines of its output (to tell a code problem from a machine one)."""

    def __init__(self, message: str, repo: str | None = None, log: str = "") -> None:
        super().__init__(message)
        self.repo = repo
        self.log = log


@dataclass
class App:
    repo: str
    port: int
    env: dict[str, str]
    log_path: Path
    proc: subprocess.Popen[bytes] | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def run_variables(key: str, run_id: str, fixed: dict[str, str] | None = None) -> dict[str, Any]:
    """The values a test run's templates use: generated secrets (test-only, per run), the partner
    and user (`acceptance.run_values` when set, else new random ones: the run's own), a tag for
    names. `partner_owned`: the partner is new to this run, so nothing else lives under it."""
    fixed = dict(fixed or {})
    out: dict[str, Any] = {
        "key": key,
        "id": run_id,
        "tag": f"aipl-{key}-{run_id}",
        "client_id": f"aipl-{run_id}",
        "client_secret": secrets.token_urlsafe(24),
        "session_secret": secrets.token_urlsafe(32),
        "partner_id": str(uuid.uuid4()),
        # a partner that is always the run's own (never real, swept at the end): service-level
        # cases that need free ground (a running period, an overlap) use it with `<identity>!own`
        "own_partner": str(uuid.uuid4()),
        "user_uuid": str(uuid.uuid4()),
        "user_pk": 900_000_000 + secrets.randbelow(99_999_999),
        "user_id": f"aipl_{run_id}",
    }
    if "user_pk" in fixed:
        fixed["user_pk"] = int(fixed["user_pk"]) if str(fixed["user_pk"]).isdigit() \
            else fixed["user_pk"]  # fmt: skip
    out.update(fixed)
    out["partner_owned"] = "partner_id" not in fixed
    return out


@dataclass
class Stack:
    deps: Any
    key: str
    repos: list[str]  # repos with a `stack` block, in coding order (services first)
    run: dict[str, Any] = field(default_factory=dict)
    apps: dict[str, App] = field(default_factory=dict)
    redis: LocalRedis | None = None
    externals: ExternalsServer | None = None
    env_from: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def directory(self) -> Path:
        return self.deps.workspaces.ticket_dir(self.key) / ".stack" / str(self.run["id"])

    @property
    def app_name(self) -> str:
        return f"aipl_test_{self.key}_{self.run['id']}"

    # ---------- what the cases need ----------

    def variables(self) -> dict[str, Any]:
        """Templates: run.*, stack.dir (this run's folder), redis.*, url:<repo>, mock:<external>."""
        out: dict[str, Any] = {"run": dict(self.run), "stack": {"dir": str(self.directory)},
                               "date": date_variables(), "lookup": {}}
        if self.redis is not None:
            out["redis"] = {"host": self.redis.host, "port": self.redis.port,
                            "password": self.redis.password, "user": "default"}  # fmt: skip
        out["url"] = {r: a.url for r, a in self.apps.items()}
        if self.externals is not None:
            out["mock"] = {n: self.externals.url(n) for n in self.externals.externals}
        return out

    def env_of(self, repo: str) -> dict[str, str]:
        app = self.apps.get(repo)
        return app.env if app else {}

    def url(self, repo: str) -> str:
        return self.apps[repo].url

    def prefix(self, repo: str) -> str:
        cfg = self.deps.config.repo(repo).stack
        name = cfg.path_prefix_env if cfg else None
        return (self.env_of(repo).get(name, "") if name else "").rstrip("/")

    def db_params(self, repo: str) -> DbParams | None:
        cfg = self.deps.config.repo(repo).stack
        if cfg is None or cfg.db is None:
            return None
        return params_from_env(self.env_from.get(repo) or self._read_env_from(repo), cfg.db)

    def log_tail(self, repo: str, lines: int = 30) -> str:
        app = self.apps.get(repo)
        if app is None or not app.log_path.is_file():
            return ""
        return "\n".join(app.log_path.read_text(errors="replace").splitlines()[-lines:])

    # ---------- start / stop ----------

    def _read_env_from(self, repo: str) -> dict[str, str]:
        cfg = self.deps.config.repo(repo).stack
        try:
            values = read_env_file(cfg.env_from if cfg else None, self.deps.home)
        except FileNotFoundError as err:
            raise StackError(f"{repo}: stack.env_from {err.filename} doesn't exist") from None
        add_secret_values(v for k, v in values.items()
                          if any(h in k.upper() for h in SECRET_HINT) and len(v) >= 6)  # fmt: skip
        self.env_from[repo] = values
        return values

    def _externals(self) -> dict[str, External]:
        acc = self.deps.config.acceptance
        out: dict[str, External] = {}
        for name, ext in acc.externals.items():
            directory = (expand_path(ext.fixtures, self.deps.home) if ext.fixtures
                         else self.deps.home / "fixtures" / name)  # fmt: skip
            real = None
            if ext.real_url_env:
                real = next((env[ext.real_url_env] for env in self.env_from.values()
                             if env.get(ext.real_url_env)), None)  # fmt: skip
            out[name] = External(name=name, mode=ext.mode, fixtures=load_fixtures(directory),
                                 real_url=real)  # fmt: skip
        return out

    def start(self) -> None:
        """Everything up, or StackError (and whatever started is stopped again)."""
        if not self.run:
            self.run = run_variables(self.key, secrets.token_hex(3),
                                     self.deps.config.acceptance.run_values)  # fmt: skip
        add_secret_values([self.run["client_secret"], self.run["session_secret"]])
        self.directory.mkdir(parents=True, exist_ok=True)
        children = self.deps.home / CHILDREN_DIR
        try:
            for repo in self.repos:
                self._read_env_from(repo)
            self.redis = LocalRedis(self.directory / "redis", self.deps.config.acceptance.redis_tls,
                                    children)  # fmt: skip
            try:
                self.redis.start()
            except (OSError, RuntimeError, subprocess.SubprocessError) as err:
                raise StackError(f"the local Redis didn't start: {err}") from None
            add_secret_values([self.redis.password])
            self.externals = ExternalsServer(self._externals())
            self.externals.start()
            for repo in self.repos:  # ports first: a gateway's env names the service's URL
                self.apps[repo] = App(repo=repo, port=free_port(), env={},
                                      log_path=self.directory / f"{repo}.log")  # fmt: skip
            for repo in self.repos:
                self._start_app(repo, children)
        except BaseException:
            self.stop()
            raise

    def _env(self, repo: str) -> dict[str, str]:
        cfg = self.deps.config.repo(repo).stack
        assert cfg is not None
        env = command_env(tuple(self.deps.config.verify.env_passthrough))
        env.update(self.env_from.get(repo) or {})
        try:
            overrides = render_value(dict(cfg.env), self.variables())
        except KeyError as err:
            raise StackError(f"{repo}: stack.env uses {{{{{err.args[0]}}}}}, which this run "
                             "doesn't have") from None  # fmt: skip
        env.update({k: str(v) for k, v in overrides.items()})
        env[cfg.port_env] = str(self.apps[repo].port)
        env["PGAPPNAME"] = self.app_name[:63]
        env.setdefault("NODE_ENV", "development")
        return env

    def _start_app(self, repo: str, children: Path) -> None:
        cfg = self.deps.config.repo(repo).stack
        assert cfg is not None
        app = self.apps[repo]
        worktree = self.deps.workspaces.worktree_path(self.key, repo)
        if (worktree / ".env.local").exists():
            get_logger(ticket=self.key, repo=repo).warning(
                "stack_env_local", hint=".env.local overrides the stack's values")
        install = getattr(self.deps.verifier, "install", None)
        if install is not None:
            install(self.key, repo)
        app.env = self._env(repo)
        log = open(app.log_path, "ab")  # noqa: SIM115 (lives with the process)
        try:
            app.proc = subprocess.Popen(shlex.split(cfg.start), cwd=worktree, env=app.env,
                                        stdout=log, stderr=subprocess.STDOUT,
                                        start_new_session=True)  # fmt: skip
        except OSError as err:
            raise StackError(f"{repo} didn't start (`{cfg.start}`): {err}", repo) from None
        register(children, app.proc.pid)
        health = app.url + self.prefix(repo) + cfg.health_path
        deadline = time.monotonic() + cfg.boot_timeout_s
        while time.monotonic() < deadline:
            if app.proc.poll() is not None:
                raise StackError(f"{repo} exited while starting (exit {app.proc.returncode})",
                                 repo, self.log_tail(repo))  # fmt: skip
            with contextlib.suppress(httpx.HTTPError):
                if httpx.get(health, timeout=3).status_code < 400:
                    return
            time.sleep(0.5)
        raise StackError(f"{repo} didn't answer {cfg.health_path} within "
                         f"{cfg.boot_timeout_s:.0f}s", repo, self.log_tail(repo))  # fmt: skip

    def stop(self) -> None:
        children = self.deps.home / CHILDREN_DIR
        for app in reversed(list(self.apps.values())):
            proc = app.proc
            if proc is None:
                continue
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(8)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(5)
            unregister(children, proc.pid)
            app.proc = None
        if self.externals is not None:
            self.externals.stop()
        if self.redis is not None:
            self.redis.stop()
