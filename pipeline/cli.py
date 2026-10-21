"""`pipeline` command line. Grows each phase (CLAUDE.md §11)."""

from __future__ import annotations

from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from pipeline.config import ConfigError, ensure_home, load_secrets
from pipeline.integrations.base import IntegrationError
from pipeline.logging import configure_logging
from pipeline.store import Store

app = typer.Typer(no_args_is_help=True, add_completion=False, help="AI SDLC pipeline")
db_app = typer.Typer(no_args_is_help=True, help="Tracking database")
app.add_typer(db_app, name="db")

console = Console()
QUIET = "CRITICAL"  # console log level for `pipeline cli`


@app.callback()
def main() -> None:
    """AI SDLC pipeline: Jira ticket → reviewed GitLab MRs."""


def _fail(message: str) -> typer.Exit:
    console.print(f"[red]{message}[/red]")
    return typer.Exit(code=1)


def _home_store() -> Store:
    secrets = load_secrets()
    home = ensure_home(secrets.home)
    configure_logging(home / "logs" / "pipeline.log", secrets=secrets.secret_values())
    return Store.for_home(home)


@db_app.command("init")
def db_init() -> None:
    """Create the tracking tables (idempotent)."""
    try:
        store = _home_store()
    except ConfigError as err:
        raise _fail(str(err)) from None
    store.init()
    console.print(f"✅ Tables ready in {store.url} (journal: {store.journal_mode()})")


@db_app.command("stats")
def db_stats() -> None:
    """Row counts per table, tickets per stage, and total reported cost."""
    store = _home_store()
    store.init()
    stats = store.stats()
    table = Table(title="pipeline.db")
    table.add_column("Table")
    table.add_column("Rows", justify="right")
    for name, count in stats["counts"].items():
        table.add_row(name, str(count))
    console.print(table)
    if stats["stages"]:
        console.print(
            "Stages: " + ", ".join(f"{k or '—'}: {v}" for k, v in stats["stages"].items())
        )
    console.print(f"Total reported cost: ${stats['total_cost_usd']:.2f}")


def _deps(dry_run: bool = False, console_level: str | None = None):
    from pipeline.deps import make_deps

    try:
        deps = make_deps(dry_run=dry_run)
    except ConfigError as err:
        raise _fail(str(err)) from None
    configure_logging(
        deps.secrets.home / "logs" / "pipeline.log",
        secrets=deps.secrets.secret_values(),
        console_level=console_level,
    )
    return deps


@app.command()
def check(
    spec_url: str = typer.Option(
        None, "--spec-url", help="Google Doc URL to test a spec fetch (opens OAuth on first run)"
    ),
    agents: bool = typer.Option(
        False, "--agents", help="Also do a tiny real agent run per enabled provider"
    ),
    chat_llm: bool = typer.Option(
        False, "--chat-llm", help="Also do one tiny run of the summary agent"
    ),
) -> None:
    """Check config, DB, Jira, GitLab (both identities, every repo), Google, provider CLIs."""
    from pipeline.checks import ICON, agent_checks, chat_llm_checks, run_checks

    deps = _deps()
    with console.status("Running checks…"):
        results = run_checks(deps, spec_url)
    if agents:
        with console.status("Agent smoke runs (up to a few minutes)…"):
            results += agent_checks(deps)
    if chat_llm:
        with console.status("Summary agent run…"):
            results += chat_llm_checks(deps)
    table = Table(title="pipeline check")
    for col in ("", "Area", "Check", "Detail"):
        table.add_column(col)
    for r in results:
        table.add_row(ICON[r.status], r.area, r.name, r.detail)
    console.print(table)
    failed = [r for r in results if r.status == "fail"]
    if failed:
        raise _fail(f"{len(failed)} check(s) failed")
    console.print("[green]All required checks passed.[/green]")


def _load_context(key: str, deps):
    from pipeline.context import build_context, context_ok
    from pipeline.intake import intake_of

    fetch_problems: list[str] = []
    try:
        with console.status(f"Reading {key}…"):
            ctx = build_context(key, deps.jira, deps.spec, deps.config, fetch_problems,
                                intake_of(deps.store, key))  # fmt: skip
    except IntegrationError as err:
        raise _fail(str(err)) from None
    ok, problems = context_ok(ctx, deps.config)
    return ctx, ok, fetch_problems + problems


@app.command()
def context(key: str, full: bool = typer.Option(False, help="Print the whole spec")) -> None:
    """Show what the pipeline would give the planner for a ticket."""
    deps = _deps()
    ctx, ok, problems = _load_context(key.upper(), deps)
    console.rule(f"{ctx.key}: {ctx.summary}")
    table = Table(show_header=False, box=None)
    table.add_row("Repos (order)", " → ".join(ctx.repos) or "—")
    table.add_row("Spec URLs", "\n".join(ctx.spec_urls) or "—")
    table.add_row("Spec hash", ctx.spec_hash[:12] or "—")
    table.add_row("Epic", ctx.epic_key or "—")
    console.print(table)
    console.print("[bold]Acceptance criteria[/bold]")
    for ac in ctx.acceptance_criteria or ["—"]:
        console.print(f"  • {ac}")
    console.print("[bold]Spec[/bold]")
    lines = ctx.spec_md.splitlines()
    shown = lines if full else lines[:40]
    console.print("\n".join(shown) or "—", markup=False, highlight=False)
    if len(lines) > len(shown):
        console.print(f"[dim]… {len(lines) - len(shown)} more lines (--full)[/dim]")
    if problems:
        console.print("[red]Problems:[/red]")
        for p in problems:
            console.print(f"  ❌ {p}", markup=False)
    if not ok:
        raise typer.Exit(code=1)
    console.print("[green]Context OK[/green]")


@app.command()
def workspace(key: str) -> None:
    """Create the ticket workspace: one worktree per repo on the same ai/<KEY>-… branch."""
    from pipeline.workspace import branch_name

    deps = _deps()
    ctx, ok, problems = _load_context(key.upper(), deps)
    if not ok:
        for p in problems:
            console.print(f"❌ {p}", markup=False)
        raise _fail("Context is not complete; workspace not created")
    ws = deps.workspaces
    branch = branch_name(ctx.key, ctx.summary)
    table = Table(title=f"{ctx.key} workspace — branch {branch}")
    table.add_column("Repo")
    table.add_column("Base")
    table.add_column("Worktree")
    try:
        for repo in ctx.repos:
            with console.status(f"{repo}: clone / fetch / worktree…"):
                path = ws.create_worktree(ctx.key, repo, branch)
            table.add_row(repo, f"origin/{deps.config.repo(repo).default_branch}", str(path))
    except IntegrationError as err:
        raise _fail(str(err)) from None
    agent = ws.write_ticket_files(ctx)
    console.print(table)
    console.print(f"Ticket files: {agent}/ticket.md, spec.md")


@app.command("plan")
def plan_cmd(
    key: str,
    mode: str = typer.Option("initial", help="initial | revise | replan | qa_fix"),
    feedback: list[str] = typer.Option(  # noqa: B008
        None,
        "--feedback",
        "-f",
        help="revise: reviewer feedback; replan: a deviation; qa_fix: a bug (repeatable)",
    ),
    provider: str = typer.Option(None, help="claude | codex (overrides the role's provider)"),
    post: bool = typer.Option(False, help="Also comment the plan on the Jira ticket"),
) -> None:
    """Run the Planner Agent for a ticket and print the plan."""
    import json

    from rich.markdown import Markdown

    from pipeline.agents.planner import MODES, plan, render_plan_md
    from pipeline.markers import make_marker

    if mode not in MODES:
        raise _fail(f"--mode must be one of {', '.join(MODES)}")
    deps = _deps()
    ctx, ok, problems = _load_context(key.upper(), deps)
    if not ok:
        for p in problems:
            console.print(f"❌ {p}", markup=False)
        raise _fail("Context is not complete; not planning")

    extra: dict = {}
    notes = list(feedback or [])
    if mode != "initial":
        previous = deps.workspaces.ticket_dir(ctx.key) / ".agent" / "plan.json"
        if not previous.exists():
            raise _fail(f"--mode {mode} needs a previous plan ({previous}); run initial first")
        extra["previous_plan"] = json.loads(previous.read_text())
        field = {"revise": "feedback", "replan": "deviations", "qa_fix": "bugs"}[mode]
        extra[field] = (
            notes
            if mode == "revise"
            else [{"deviation": n} if mode == "replan" else {"description": n} for n in notes]
        )
        if not notes:
            raise _fail(f"--mode {mode} needs at least one --feedback")

    label = provider or deps.config.resolve_provider("planner")
    try:
        with console.status(f"Planning {ctx.key} ({mode}, {label})… this can take minutes"):
            result, run = plan(ctx, mode, deps, extra, provider_override=provider)
    except (IntegrationError, ValueError) as err:
        raise _fail(str(err)) from None

    tokens = f", tokens {run.input_tokens}→{run.output_tokens}" if run.input_tokens else ""
    cost = f", ${run.cost_usd:.2f}" if run.cost_usd else ""
    console.print(
        f"[dim]{run.provider} ({run.auth_mode}), run #{run.run_no}, "
        f"{run.duration_s:.0f}s, turns {run.turns or '—'}{tokens}{cost}\n"
        f"transcript: {run.transcript_path or '—'}[/dim]",
        highlight=False,
    )
    if result is None:
        raise _fail(f"Planner failed: {run.error}")
    md = render_plan_md(result)
    console.print(Markdown(md))
    agent_dir = deps.workspaces.ticket_dir(ctx.key) / ".agent"
    console.print(f"[dim]Files: {agent_dir}/plan.json, contract.json; <repo>/.agent/task.md[/dim]")
    if post:
        marker = make_marker(ctx.key, "plan", run.run_no)
        try:
            deps.jira.comment(ctx.key, md, marker)
        except IntegrationError as err:
            raise _fail(f"Jira comment failed: {err}") from None
        console.print(f"Posted to Jira ({marker})")


# ---------- Phase 3: server, commands, status ----------


def _port(port: int | None) -> int:
    if port:
        return port
    from pipeline.config import load_config

    try:
        return load_config().chat.local_port
    except ConfigError as err:
        raise _fail(str(err)) from None


def _server(port: int, path: str, params: dict | None = None, body: dict | None = None):
    """JSON from the running server, or None if no server is listening."""
    import httpx

    url = f"http://127.0.0.1:{port}{path}"
    try:
        if body is not None:
            r = httpx.post(url, json=body, timeout=30)
        else:
            r = httpx.get(url, params=params, timeout=30)
    except httpx.ConnectError:
        return None
    r.raise_for_status()
    return r.json()


def _print_md(text: str, key: str | None = None) -> None:
    from rich.markdown import Markdown

    if key:
        console.rule(f"[{key}]", align="left", style="dim")
    console.print(Markdown(text))


def _config_secrets():
    from pipeline.config import load_config, load_secrets

    return load_config(), load_secrets()


def _services(sync: bool):
    from pipeline.server import build_services

    deps = _deps(console_level="WARNING")
    return build_services(deps, sync=sync)


@app.command()
def serve(
    port: int = typer.Option(None, help="Local port (default chat.local_port)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="All fake (Jira, GitLab, agents)"),
    key: str = typer.Option("DEMO-1", help="--dry-run: the fake ticket to implement"),
) -> None:
    """Run the pipeline headless: workers, pollers and the localhost endpoint for `pipeline say`
    and `pipeline cli` (which attaches to it)."""
    from pipeline.server import serve as run_server

    port = _port(port)
    if _server(port, "/local/status") is not None:
        raise _fail(f"A pipeline server is already running on port {port}.")
    if not dry_run:
        services = _services(sync=False)
        run_server(services, port, console.print)
        return

    import tempfile
    from pathlib import Path

    from pipeline.dryrun import make_dry_run

    try:
        config, secrets = _config_secrets()
    except ConfigError as err:
        raise _fail(str(err)) from None
    root = Path(tempfile.mkdtemp(prefix="pipeline-dry-serve-"))
    configure_logging(root / "pipeline.log", secrets=secrets.secret_values(),
                      console_level="WARNING")  # fmt: skip
    key = key.upper()
    services = make_dry_run(key, config, secrets, root, sync=False)
    console.print(f"[bold]Dry run[/bold]: fake Jira/GitLab/agents, data in {root}. "
                  f"Try: implement {key}")  # fmt: skip
    run_server(services, port, console.print)  # dry run: only the merge poller + merger


@app.command("cli")
def terminal(
    port: int = typer.Option(None, help="Local port (default chat.local_port)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="All fake (Jira, GitLab, agents)"),
    key: str = typer.Option("DEMO-1", help="--dry-run: the fake ticket to implement"),
) -> None:
    """The interactive terminal (D167): type exact commands (`implement DEMO-12`, `/retry
    DEMO-12`, `help`); every ticket's messages show up here. Runs the pipeline in this process,
    or attaches to a running `pipeline serve`. Exact commands only."""
    from rich.console import Console

    from pipeline.chat.console_chat import ConsoleChat
    from pipeline.terminal import LocalBackend, RemoteBackend, Terminal

    # the prompt shows the ticket messages; warnings and tracebacks go to logs/pipeline.log only
    # (they printed over the prompt; the chat already says what went wrong in plain words)
    port = _port(port)
    term = Console(force_terminal=console.is_terminal or None)
    try:
        config, secrets = _config_secrets()
    except ConfigError as err:
        raise _fail(str(err)) from None
    history = secrets.home / "cli_history"
    attached = _server(port, "/local/status") is not None
    if attached and not dry_run:
        deps = _deps(console_level=QUIET)
        deps.config = config
        shell = Terminal(None, term, history)
        backend: Any = RemoteBackend(port, deps, shell.show)
        shell.backend = backend
        term.print(f"Attached to the pipeline server on port {port}. Type `help` for the "
                   "commands, `exit` to leave (the server keeps running).")  # fmt: skip
    else:
        if dry_run:
            import tempfile
            from pathlib import Path

            from pipeline.dryrun import make_dry_run

            root = Path(tempfile.mkdtemp(prefix="pipeline-dry-cli-"))
            configure_logging(root / "pipeline.log", secrets=secrets.secret_values(),
                              console_level=QUIET)  # fmt: skip
            services = make_dry_run(key.upper(), config, secrets, root, sync=False)
            term.print(f"[bold]Dry run[/bold]: fake Jira/GitLab/agents, data in {root}. "
                       f"Try: implement {key.upper()}")  # fmt: skip
            port_or_none = None if attached else port
        else:
            from pipeline.server import build_services

            deps = _deps(console_level=QUIET)
            deps.config = config
            deps.chat = ConsoleChat(deps.home / "logs" / "chat.log", console=term,
                                    store=deps.store, command_prefix="")  # fmt: skip
            services = build_services(deps, sync=False)
            port_or_none = port
        chat = services.deps.chat
        if isinstance(chat, ConsoleChat):  # cards show the command to type, at this prompt
            chat.command_prefix, chat.console = "", term
        shell = Terminal(None, term, history)
        backend = LocalBackend(services, port_or_none, term.print)
        shell.backend = backend
        term.print("SDLC terminal. Type `help` for the commands, `exit` to leave (running "
                   "steps carry on at the next start).")  # fmt: skip
    try:
        shell.run()
    finally:
        backend.close()


@app.command()
def say(
    text: str = typer.Argument(..., help='A chat command, e.g. "implement DEMO-1"'),
    follow: bool = typer.Option(True, help="Follow the ticket's chat until it pauses"),
    port: int = typer.Option(None, help="Local port of `pipeline serve`"),
) -> None:
    """Send a chat command  to the running server, or run it here."""
    import time

    from pipeline.chat.commands import (
        CommandParseError,
        UnknownCommandError,
        cli_hint,
        parse_command,
        voiced,
    )
    from pipeline.server import local_user

    port = _port(port)
    user = local_user()
    reply = _server(port, "/local/commands", body={"text": text, "actor_id": user,
                                                   "actor_name": user})  # fmt: skip
    if reply is None:
        console.print("[dim]No server running: handling the command in this process.[/dim]")
        services = _services(sync=True)
        try:
            chat_cfg = services.deps.config.chat
            cmd = parse_command(text, actor_id=user, actor_name=user, source="cli",
                                agents=chat_cfg.agents)  # fmt: skip
        except (UnknownCommandError, CommandParseError) as err:  # exact commands only (D167)
            raise _fail(cli_hint(text, err, services.deps.config.jira.project)) from None
        result = services.bus.handle(cmd)
        chat_cfg = services.deps.config.chat
        _print_md(voiced(cmd, result.message_md, chat_cfg.agents))
        if not result.accepted:
            raise typer.Exit(code=1)
        return

    _print_md(reply["message_md"])
    key = reply.get("key")
    if not reply["accepted"]:
        raise typer.Exit(code=1)
    if not follow or not key or reply.get("verb") in ("help", "status"):
        return
    seq = reply.get("chat_seq", 0)
    try:
        while True:
            data = _server(port, "/local/chat", params={"key": key, "after": seq})
            if data is None:
                raise _fail("The server stopped.")
            for m in data["messages"]:
                seq = max(seq, m["seq"])
                _print_md(m["text"], m["key"])
            if not data["busy"] and not data["messages"]:
                break
            time.sleep(1)
    except KeyboardInterrupt:
        console.print("[dim]Detached; the ticket keeps running in the server.[/dim]")
        return
    from pipeline.chat.notifier import gate_label

    state = (
        f"waiting for {gate_label(data['waiting'])}"
        if data["waiting"]
        else ("finished" if data["finished"] else "idle")
    )
    console.print(f"[dim]{key} is {state}.[/dim]")


@app.command()
def status(
    key: str = typer.Argument(None, help="Ticket key (omit for all in-flight tickets)"),
    port: int = typer.Option(None, help="Local port of `pipeline serve`"),
) -> None:
    """Stage, waiting for, MRs, slots, rounds, cost."""
    key = key.upper() if key else None
    data = _server(_port(port), "/local/status", params={"key": key or ""})
    if data is None:
        data = {"message_md": _services(sync=True).bus.status_text(key, detailed=True)}
    _print_md(data["message_md"])


@app.command()
def slots(port: int = typer.Option(None, help="Local port of `pipeline serve`")) -> None:
    """Agent pool: slots, which ticket holds each, and the queues."""
    data = _server(_port(port), "/local/slots")
    if data is None:
        store = _home_store()
        store.init()
        data = {"slots": store.list_slots(), "queues": {}}
        console.print("[dim]No server running: slots from the database, no queues.[/dim]")
    table = Table(title="Agent slots")
    for col in ("Slot", "Pool", "Provider", "Busy with", "Role", "Since"):
        table.add_column(col)
    for s in data["slots"]:
        table.add_row(s["name"], s["pool"], s["provider"], s["busy_key"] or "—",
                      s["busy_role"] or "—", str(s["busy_since"] or "—")[:19])  # fmt: skip
    console.print(table)
    for pool, keys in data["queues"].items():
        if keys:
            console.print(f"Queue {pool}: {', '.join(keys)}")
    if not any(data["queues"].values()):
        console.print("Queues: empty")


@app.command()
def reset(
    key: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation"),
    port: int = typer.Option(None, help="Local port of `pipeline serve`"),
) -> None:
    """Forget a ticket: checkpoints, tracking rows (commands log kept) and local worktrees.
    MRs and branches on GitLab and the Jira ticket are not touched."""
    from pipeline.graph.checkpoint import make_checkpointer

    key = key.upper()
    live = _server(_port(port), "/local/chat", params={"key": key, "after": 0})
    if live is not None and live["busy"]:
        raise _fail(f"{key} is running a step in the server; stop it first.")
    if not yes and not typer.confirm(f"Reset {key} (checkpoints, tracking rows, worktrees)?"):
        raise typer.Exit(code=1)
    from pipeline.maintenance import local_reset

    deps = _deps(console_level="WARNING")
    for problem in local_reset(deps, key, make_checkpointer(deps.home / "pipeline.db")):
        console.print(f"[yellow]{problem}[/yellow]")
    console.print(f"✅ {key} reset. GitLab MRs/branches and the Jira ticket were not changed.")


def _pipeline_command(text: str, port: int | None) -> None:
    """pause / unpause through the running server (same bus as the chat), else in-process."""
    from pipeline.server import local_user

    body = {"text": text, "actor_name": local_user(), "actor_id": local_user()}
    data = _server(_port(port), "/local/commands", body=body)
    if data is None:
        from pipeline.chat.commands import parse_command

        services = _services(sync=True)
        cmd = parse_command(text, actor_id=local_user(), actor_name=local_user(), source="cli")
        data = services.bus.handle(cmd).model_dump()
    _print_md(data["message_md"])
    if not data.get("accepted"):
        raise typer.Exit(code=1)


@app.command()
def pause(
    reason: str = typer.Argument("", help="Why (shown in the chat)"),
    port: int = typer.Option(None, help="Local port of `pipeline serve`"),
) -> None:
    """Pause the whole pipeline: no step or agent starts until `pipeline unpause`."""
    _pipeline_command("pause --confirm" + (f": {reason}" if reason else ""), port)


@app.command()
def unpause(port: int = typer.Option(None, help="Local port of `pipeline serve`")) -> None:
    """Resume the pipeline; waiting tickets carry on by themselves."""
    _pipeline_command("unpause", port)


@app.command()
def report(
    html_out: bool = typer.Option(False, "--html", help="Also write $PIPELINE_HOME/report.html"),
    days: int = typer.Option(None, help="Only tickets started in the last N days"),
) -> None:
    """Per ticket: stages, time waiting on people, rounds, agent runs, tokens, cost."""
    from pipeline.report import collect, render_markdown, write_html

    store = _home_store()
    store.init()
    rep = collect(store, days=days)
    _print_md(render_markdown(rep))
    if html_out:
        from pipeline.config import load_secrets

        console.print(f"HTML report: {write_html(rep, load_secrets().home)}")


@app.command()
def logs(
    key: str,
    transcripts: bool = typer.Option(False, help="Also list the agent transcripts"),
) -> None:
    """A ticket's timeline: events and commands in order, then its agent runs."""
    from pipeline.logs import timeline

    store = _home_store()
    store.init()
    console.print(timeline(store, key.upper(), transcripts=transcripts), markup=False)


@app.command()
def gc(
    cache: bool = typer.Option(False, help="Also clear the node_modules cache"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Only list what would go"),
) -> None:
    """Remove the workspaces of finished, stopped or unknown tickets."""
    from pipeline.maintenance import gc as run_gc

    removed = run_gc(_deps(console_level="WARNING"), cache=cache, dry_run=dry_run)
    verb = "Would remove" if dry_run else "Removed"
    console.print(f"{verb}: {', '.join(removed)}" if removed else "Nothing to remove.")


@app.command("demo-reset")
def demo_reset(
    keys: list[str] = typer.Argument(None, help="Tickets (default: demo.tickets in the config)"),  # noqa: B008
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation"),
    port: int = typer.Option(None, help="Local port of `pipeline serve`"),
) -> None:
    """Demo tickets back to the start: close their AI merge requests, delete their ai/ branches,
    delete the bot's Jira comments and deployment notes, Jira → To Do, forget them locally.
    `develop` is never touched: code a rehearsal merged stays there."""
    from pipeline.graph.checkpoint import make_checkpointer
    from pipeline.maintenance import demo_reset as reset_one

    deps = _deps(console_level="WARNING")
    names = [k.upper() for k in keys] if keys else [k.upper() for k in deps.config.demo.tickets]
    if not names:
        raise _fail("Name the tickets, or list them under `demo.tickets` in the config.")
    for key in names:
        live = _server(_port(port), "/local/chat", params={"key": key, "after": 0})
        if live is not None and live["busy"]:
            raise _fail(f"{key} is running a step in the server; stop it first.")
    if not yes and not typer.confirm(
        f"Reset {', '.join(names)} on GitLab (close MRs, delete ai/ branches), Jira (bot "
        "comments, deployment notes, To Do) and locally?"
    ):
        raise typer.Exit(code=1)
    checkpointer = make_checkpointer(deps.home / "pipeline.db")
    failed = False
    for key in names:
        result = reset_one(deps, key, checkpointer)
        failed |= bool(result.problems)
        for line in result.lines():
            console.print(line, markup=False)
    if failed:
        raise typer.Exit(code=1)


@app.command()
def warm(repo: str = typer.Argument(None, help="One repo (default: all configured)")) -> None:
    """Fetch the base clones and install each repo's dependencies into the node_modules cache,
    so new worktrees start without an `npm ci` (run before a demo)."""
    from pipeline.verify import Verifier

    deps = _deps(console_level="WARNING")
    verifier = Verifier(deps.config, deps.workspaces)
    for name in [repo] if repo else deps.config.repo_names:
        console.print(f"{name}: …", end="\r")
        try:
            outcome = verifier.warm(name)
        except IntegrationError as err:
            outcome = f"failed: {err}"
        console.print(f"{name}: {outcome}")


# ---------- live acceptance tests (P10) ----------


def _agent_deps(deps) -> None:
    """What `pipeline serve` adds before agents run: the slot pool and a chat (a terminal log)."""
    from pipeline.agents.pool import AgentPool
    from pipeline.chat.console_chat import ConsoleChat

    if deps.pool is None:
        deps.pool = AgentPool(deps.config, deps.store)
    if deps.chat is None:
        deps.chat = ConsoleChat(deps.home / "logs" / "chat.log", store=deps.store)


def _ticket_state(deps, key: str) -> dict:
    """The ticket's saved state (checkpoint), else its repos from Jira (no plan yet)."""
    from pipeline.graph.build import build_graph
    from pipeline.graph.checkpoint import make_checkpointer
    from pipeline.graph.runtime import thread_config

    graph = build_graph(deps, make_checkpointer(deps.home / "pipeline.db"))
    values = dict(graph.get_state(thread_config(key)).values or {})
    if values.get("repos"):
        return values
    ctx, ok, problems = _load_context(key, deps)
    if not ok:
        raise _fail("; ".join(problems))
    return {"key": key, "context": ctx, "repos": {r: {} for r in ctx.repos}}


@app.command()
def env(
    key: str,
    boot: bool = typer.Option(True, help="Start the apps once (the worktrees' current code)"),
) -> None:
    """The live test preflight for a ticket: tools, the dev DB, the env, the apps boot."""
    from pipeline.testing.preflight import preflight, report_md

    key = key.upper()
    deps = _deps(console_level="WARNING")
    state = _ticket_state(deps, key)
    with console.status("Checking the live test environment…"):
        report = preflight(deps, key, list(state["repos"]), state.get("plan"), boot=boot)
    _print_md(report_md(report))
    if not report.ok:
        for p in report.problems:
            console.print(f"❌ {p}", markup=False)
        raise typer.Exit(code=1)
    console.print("[green]Ready for live tests.[/green]")


@app.command()
def acceptance(
    key: str,
    cleanup: bool = typer.Option(False, "--cleanup", help="Only delete a crashed run's test rows"),
) -> None:
    """Run the live acceptance tests on the ticket's worktrees now (shared dev DB; only the rows
    the run creates are deleted), or `--cleanup` a run that crashed."""
    from pipeline.testing.service import AcceptanceService

    key = key.upper()
    deps = _deps(console_level="WARNING")
    _agent_deps(deps)  # the Tester needs a pool slot (and the chat for a queue message)
    svc = AcceptanceService(deps)
    if cleanup:
        report = svc.cleanup_leftovers(key)
        if report is None:
            console.print(f"Nothing left over for {key}.")
            return
        console.print(f"Deleted {report.get('deleted', 0)} rows; {report.get('left', 0)} left"
                      + (f"; error: {report['error']}" if report.get("error") else "."))
        if report.get("error") or report.get("left"):
            raise typer.Exit(code=1)
        return
    if svc.mode == "off":
        raise _fail("acceptance.mode is off in the config")
    state = _ticket_state(deps, key)
    if state.get("plan") is None:
        raise _fail(f"{key} has no plan yet: the cases come from its criteria and contract")
    # the ticket as Jira words it now (it may have been edited since the plan): the criteria
    # the tests prove come from here; a criterion the ticket dropped is no longer tested
    fresh, ok, problems = _load_context(key, deps)
    if not ok:
        raise _fail("couldn't read the ticket from Jira: " + "; ".join(problems))
    state["context"] = fresh
    deps.workspaces.write_ticket_files(fresh)
    from pipeline.agents.planner import ticket_ac_split

    numbered = ticket_ac_split(fresh.description_md)[0]
    console.print(f"Ticket refreshed from Jira ({len(numbered)} numbered criteria).")
    with console.status("Running the live acceptance tests…"):
        outcome = svc.run(state, lambda holder: console.print(f"Waiting for {holder}…"))
    res = outcome.result
    table = Table(title=f"{key} live acceptance tests")
    for col in ("Case", "Criterion", "Repo", "Status", "Why"):
        table.add_column(col)
    for r in res.results:
        table.add_row(r.case_id, r.criterion or "contract", r.repo, r.status, r.reason[:120])
    console.print(table)
    for label, value in (("Pending migrations", ", ".join(res.pending_migrations)),
                         ("Cleanup", str(res.cleanup or "")),
                         ("Problem", outcome.machine or outcome.tester_error or ""),
                         ("Doesn't start", outcome.boot_failure[0] if outcome.boot_failure
                          else "")):  # fmt: skip
        if value:
            console.print(f"{label}: {value}", markup=False)
    if not res.ok or outcome.machine:
        raise typer.Exit(code=1)


@app.command()
def migrate(
    key: str,
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask for confirmation"),
) -> None:
    """Apply the ticket's migrations on the dev database (a person's command; the pipeline
    never runs migrations on its own). Lists every migration `db:migrate` would run first."""
    import subprocess

    from pipeline.safe_env import command_env
    from pipeline.testing.db import check_host, params_from_env
    from pipeline.testing.envfile import read_env_file
    from pipeline.testing.service import AcceptanceService

    key = key.upper()
    deps = _deps(console_level="WARNING")
    svc = AcceptanceService(deps)
    state = _ticket_state(deps, key)
    repos = [r for r in svc.repos(state) if deps.config.repo(r).stack.db is not None]
    if not repos:
        raise _fail(f"none of {key}'s repos has a `stack.db` in the config")
    for repo in repos:
        cfg = deps.config.repo(repo)
        worktree = deps.workspaces.worktree_path(key, repo)
        env_vals = read_env_file(cfg.stack.env_from, deps.home)
        params = params_from_env(env_vals, cfg.stack.db)
        check_host(params, deps.config.acceptance.allowed_db_hosts)
        run_env = {**command_env(tuple(deps.config.verify.env_passthrough)), **env_vals,
                   "NODE_ENV": "development"}  # fmt: skip
        status = subprocess.run(["npx", "sequelize-cli", "db:migrate:status"], cwd=worktree,
                                env=run_env, capture_output=True, text=True,
                                timeout=180)  # fmt: skip
        down = [ln.split(None, 1)[1].strip() for ln in status.stdout.splitlines()
                if ln.strip().lower().startswith("down")]  # fmt: skip
        if status.returncode != 0:
            raise _fail(f"{repo}: db:migrate:status failed:\n{status.stderr[-1500:]}")
        if not down:
            console.print(f"{repo}: nothing to apply on {params.where()}.")
            continue
        redundant = svc.redundant_migrations(key, repo, down)
        if redundant:
            raise _fail(
                f"{repo}: db:migrate would run {', '.join(redundant)}, but the tables they create "
                "already exist on the dev database (applied under other file names). Running "
                "them would add constraints and indexes to the team's tables; the live tests "
                "don't need them. Nothing was applied.")  # fmt: skip
        console.print(f"[bold]{repo}[/bold]: these would be applied on {params.where()}:")
        for name in down:
            console.print(f"  - {name}", markup=False)
        if not yes and not typer.confirm("Apply them on the shared dev database?"):
            raise typer.Exit(code=1)
        done = subprocess.run(["npx", "sequelize-cli", "db:migrate"], cwd=worktree, env=run_env,
                              capture_output=True, text=True, timeout=900)  # fmt: skip
        deps.store.record_event(key, "migrate", node="cli", repo=repo,
                                data={"applied": down, "ok": done.returncode == 0})  # fmt: skip
        if done.returncode != 0:
            raise _fail(f"{repo}: db:migrate failed:\n{(done.stdout + done.stderr)[-2000:]}")
        console.print(f"✅ {repo}: applied {len(down)}. The waiting ticket carries on at the next "
                      f"check, or now with `pipeline say \"test {key}\"`.")


@app.command()
def record(
    external: str,
    path: str,
    method: str = typer.Option("GET", help="GET or HEAD only"),
    header: list[str] = typer.Option(  # noqa: B008 (typer: a repeatable option)
        None, "--header", "-H", help="'Name: value' (repeatable)"),
    name: str = typer.Option(None, help="Fixture file name (default from the path)"),
) -> None:
    """Save a real dev service's answer as a fixture for the live tests (reads only)."""
    import json
    import re
    from datetime import UTC, datetime

    import httpx

    from pipeline.testing.envfile import expand_path, read_env_file

    deps = _deps(console_level="WARNING")
    ext = deps.config.acceptance.externals.get(external)
    if ext is None:
        raise _fail(f"`{external}` isn't in acceptance.externals")
    if method.upper() not in ("GET", "HEAD"):
        raise _fail("only GET / HEAD: a write would change the real service")
    if not ext.real_url_env:
        raise _fail(f"acceptance.externals.{external}.real_url_env isn't set")
    base = None
    for repo in deps.config.repos:
        if repo.stack and repo.stack.env_from:
            base = read_env_file(repo.stack.env_from, deps.home).get(ext.real_url_env) or base
    if not base:
        raise _fail(f"no repo's stack.env_from sets {ext.real_url_env}")
    headers = dict(h.split(":", 1) for h in header or [])
    resp = httpx.request(method.upper(), base.rstrip("/") + path,
                         headers={k.strip(): v.strip() for k, v in headers.items()}, timeout=30)
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    directory = expand_path(ext.fixtures, deps.home) if ext.fixtures else \
        deps.home / "fixtures" / external  # fmt: skip
    directory.mkdir(parents=True, exist_ok=True)
    stem = name or (method.lower() + re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_"))
    target = directory / f"{stem}.json"
    target.write_text(json.dumps({
        "method": method.upper(), "path": path, "status": resp.status_code, "body": body,
        "source": "recorded", "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }, indent=1))  # fmt: skip
    console.print(f"Saved {resp.status_code} → {target} (edit `path` to `/users/{{id}}` to match "
                  "any id).")


@app.command()
def run(
    key: str,
    dry_run: bool = typer.Option(False, "--dry-run", help="Everything fake: fast, free"),
    keep: bool = typer.Option(False, help="Dry run: keep the temp dir"),
) -> None:
    """Shortcut for `say "implement <KEY>"` (in this process). --dry-run: all fakes, the plan
    is auto-approved, and the run goes through to the MRs."""
    import shutil
    import tempfile
    from pathlib import Path

    key = key.upper()
    if not dry_run:
        say(f"implement {key}", follow=True, port=None)
        return

    from pipeline.config import load_config
    from pipeline.dryrun import make_dry_run, run_dry

    try:
        config = load_config()
    except ConfigError as err:
        raise _fail(str(err)) from None
    root = Path(tempfile.mkdtemp(prefix="pipeline-dry-run-"))
    configure_logging(
        root / "pipeline.log", secrets=load_secrets().secret_values(), console_level="WARNING"
    )
    try:
        services = make_dry_run(key, config, load_secrets(), root)
        outcome = run_dry(services, key)
    finally:
        if not keep:
            shutil.rmtree(root, ignore_errors=True)
    console.rule("Dry run result")
    for mr in outcome["mrs"]:
        console.print(f"  MR {mr['repo']}: {mr['mr_url']} ({mr['state']})")
    console.print(f"  Jira (fake): {outcome['jira_status']}")
    if keep:
        console.print(f"  Files: {root}")
    if not outcome["finished"] or not outcome["mrs"]:
        raise _fail(f"Dry run did not finish: {outcome['halted_reason'] or outcome['waiting']}")
    console.print("[green]Dry run completed: every step connected.[/green]")


if __name__ == "__main__":
    app()
