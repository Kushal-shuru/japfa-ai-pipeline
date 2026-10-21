"""`pipeline serve` (Phase 3 8.1): one process with

- the graph runtime (daemon worker threads running ticket steps for the command bus),
- APScheduler pollers (Jira comment commands; GitLab
  merge states for tickets waiting for their merges, P6),
- a localhost-only HTTP endpoint for `pipeline say` / `status` / `slots`.

Handlers only parse, call the bus (which queues work) and reply; no agent runs in a handler.
"""

from __future__ import annotations

import getpass
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any

from aiohttp import web
from apscheduler.schedulers.background import BackgroundScheduler

from pipeline.chat.commands import (
    CommandBus,
    CommandParseError,
    UnknownCommandError,
    cli_hint,
    parse_command,
    voiced,
)
from pipeline.events import (
    GitLabMergePoller,
    JiraCommentPoller,
    MigrationPoller,
    QaPoller,
)
from pipeline.graph.runtime import GraphRuntime
from pipeline.logging import get_logger


@dataclass
class Services:
    """Everything a running pipeline needs; built by `build_services`."""

    deps: Any
    runtime: GraphRuntime
    bus: CommandBus


def build_services(deps: Any, sync: bool, checkpoint_path: Any = "default") -> Services:
    """Wire chat (console), pool, verifier, checkpointer,
    runtime and bus onto `deps`."""
    from pipeline.agents.pool import AgentPool
    from pipeline.chat.console_chat import ConsoleChat
    from pipeline.graph.checkpoint import make_checkpointer
    from pipeline.verify import Verifier

    if deps.chat is None:
        log_path = deps.home / "logs" / "chat.log"
        deps.chat = ConsoleChat(log_path, store=deps.store)
    if deps.pool is None:
        deps.pool = AgentPool(deps.config, deps.store)
    if deps.verifier is None and deps.workspaces is not None:
        deps.verifier = Verifier(deps.config, deps.workspaces)
    if checkpoint_path == "default":
        checkpoint_path = None if deps.dry_run else deps.home / "pipeline.db"
    workers = sum(len(s) for s in deps.config.agent_pool.values()) + 2
    runtime = GraphRuntime(deps, make_checkpointer(checkpoint_path), workers=workers, sync=sync)
    return Services(deps, runtime, CommandBus(deps, runtime))


def local_user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # pragma: no cover
        return "local user"


# ---------- HTTP (localhost only) ----------


@web.middleware
async def localhost_only(request: web.Request, handler: Any) -> web.StreamResponse:
    remote = request.remote or ""
    try:
        ok = ip_address(remote).is_loopback
    except ValueError:
        ok = False
    if not ok:
        return web.json_response({"error": "localhost only"}, status=403)
    return await handler(request)


def make_app(services: Services) -> web.Application:
    deps, runtime, bus = services.deps, services.runtime, services.bus
    app = web.Application(middlewares=[localhost_only])

    async def post_command(request: web.Request) -> web.Response:
        data = await request.json()
        text = str(data.get("text", ""))
        seq = getattr(deps.chat, "last_seq", 0)
        user = str(data.get("actor_name") or local_user())
        prefix = deps.config.jira.project
        try:
            cmd = parse_command(
                text, actor_id=str(data.get("actor_id") or local_user()), actor_name=user,
                source="cli", thread_key=data.get("thread_key"),
                bot_name=deps.config.chat.bot_name, agents=deps.config.chat.agents,
            )  # fmt: skip
        except UnknownCommandError as err:  # exact commands only (D167)
            return web.json_response({"accepted": False, "key": None, "chat_seq": seq,
                                      "message_md": cli_hint(text, err, prefix)})  # fmt: skip
        except CommandParseError as err:
            said = cli_hint(text, err, prefix)
            return web.json_response({"accepted": False, "message_md": said, "key": None,
                                      "chat_seq": seq})  # fmt: skip
        result = bus.handle(cmd)
        chat_cfg = deps.config.chat
        said = voiced(cmd, result.message_md, chat_cfg.agents, chat_cfg.bot_name)  # D111
        return web.json_response({**result.model_dump(), "message_md": said, "key": cmd.key,
                                  "verb": cmd.verb, "chat_seq": seq})  # fmt: skip

    async def get_chat(request: web.Request) -> web.Response:
        key = request.query.get("key") or None
        after = int(request.query.get("after", "0"))
        since = getattr(deps.chat, "since", None)
        messages = since(after, key) if since else []
        info = runtime.snapshot(key) if key else None
        return web.json_response({
            "messages": messages,
            "busy": bool(info and info.busy),
            "waiting": info.waiting if info else None,
            "finished": bool(info and info.finished),
        })  # fmt: skip

    async def get_status(request: web.Request) -> web.Response:
        return web.json_response(
            {"message_md": bus.status_text(request.query.get("key") or None, detailed=True)}
        )

    async def get_slots(request: web.Request) -> web.Response:
        return web.json_response({
            "slots": [{k: (str(v) if v is not None else None) for k, v in s.items()}
                      for s in deps.store.list_slots()],
            "queues": deps.pool.queues(),
        })  # fmt: skip

    app.router.add_post("/local/commands", post_command)
    app.router.add_get("/local/chat", get_chat)
    app.router.add_get("/local/status", get_status)
    app.router.add_get("/local/slots", get_slots)
    return app


# ---------- serve ----------


def start_pollers(services: Services) -> BackgroundScheduler:
    """Jira comments, testing (QA - Passed, D126) and the GitLab merge
    poller. A dry run (fake Jira / GitLab) gets
    only the merge poller plus the dry-run merger, which merges one approved MR every few
    seconds so the merge wait can be watched."""
    deps = services.deps
    scheduler = BackgroundScheduler(daemon=True)
    merges = GitLabMergePoller(deps, services.runtime)
    if getattr(deps, "dry_run", False):
        from pipeline.dryrun import DryRunMerger

        scheduler.add_job(DryRunMerger(deps, services.runtime).tick, "interval", seconds=5,
                          id="dry_run_merger", max_instances=1, coalesce=True)  # fmt: skip
        scheduler.add_job(merges.poll, "interval", seconds=2, id="gitlab_merges",
                          max_instances=1, coalesce=True)  # fmt: skip
        scheduler.start()
        return scheduler
    scheduler.add_job(merges.poll, "interval", seconds=deps.config.polling.gitlab_seconds,
                      id="gitlab_merges", max_instances=1, coalesce=True)  # fmt: skip
    comments = JiraCommentPoller(deps, services.bus)
    seconds = deps.config.polling.jira_seconds
    scheduler.add_job(QaPoller(deps, services.runtime).poll, "interval", seconds=seconds,
                      id="qa", max_instances=1, coalesce=True)  # fmt: skip
    scheduler.add_job(comments.poll, "interval", seconds=seconds, id="jira_comments",
                      max_instances=1, coalesce=True)  # fmt: skip
    from pipeline.reminders import GateReminder, post_digest

    scheduler.add_job(GateReminder(deps, services.runtime).poll, "interval", minutes=10,
                      id="gate_reminders", max_instances=1, coalesce=True)  # fmt: skip
    digest = deps.config.chat.digest_time
    if digest:
        hour, _, minute = digest.partition(":")
        scheduler.add_job(post_digest, "cron", args=[deps, services.runtime], hour=int(hour),
                          minute=int(minute or 0), id="digest", coalesce=True)  # fmt: skip
    if deps.config.acceptance.mode != "off":  # P10: a migration applied on the dev DB
        scheduler.add_job(MigrationPoller(deps, services.runtime).poll, "interval",
                          seconds=deps.config.polling.migration_seconds, id="migrations",
                          max_instances=1, coalesce=True)  # fmt: skip
    scheduler.start()
    return scheduler


def serve(services: Services, port: int, print_fn: Any = print, pollers: bool = True) -> None:
    log = get_logger()
    scheduler = start_pollers(services) if pollers else None
    resumed = services.runtime.recover()
    if resumed:
        print_fn(f"Resuming steps cut off by the last shutdown: {', '.join(resumed)}")

    async def on_shutdown(_app: web.Application) -> None:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
        services.runtime.shutdown()
        log.info("server_stopped")

    app = make_app(services)
    app.on_shutdown.append(on_shutdown)
    print_fn(
        f"Pipeline server on http://127.0.0.1:{port} (localhost only). "
        f'Send commands with: pipeline say "…". Ctrl-C to stop '
        "(running agent steps are resumed on the next start)."
    )
    web.run_app(app, host="127.0.0.1", port=port, print=None, handle_signals=True)
