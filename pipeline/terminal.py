"""`pipeline cli` (D167): the interactive terminal, exact commands.

People type exact commands (`implement DEMO-12`, `/retry DEMO-12`, `fix DEMO-12 minor`); there
is no Chat LLM here: a mistake gets the command's exact form (`commands.cli_hint`). Every
ticket's messages print above the prompt as they arrive, cards with the commands to type.
`stop`, `accept` and `pause` ask `[y/N]` (`--confirm` skips the question). `reset KEY
[--full]` and `exit` exist only here.

Two ways to run:

- **in process** (no server running): the pipeline runs here (workers, pollers, restart
  recovery) and serves the localhost endpoint too, so `pipeline say` / `status` work next to it;
- **attached** (a `pipeline serve` is running on the port): commands go to that server and its
  messages are followed. Commands are still checked here first, so nothing is interpreted.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.markdown import Markdown

from pipeline.chat.commands import (
    KEY_RE,
    CommandParseError,
    cli_hint,
    parse_command,
    voiced,
)

EXIT_WORDS = frozenset({"exit", "quit"})
Choose = Callable[[list[tuple[str, str]]], list[str]]
PROMPT = "sdlc> "


# ---------- backends ----------


class LocalBackend:
    """The pipeline in this process (`build_services(sync=False)`)."""

    def __init__(self, services: Any, port: int | None, say: Callable[[str], None]) -> None:
        from pipeline.server import local_user, start_pollers

        self.services = services
        self.deps = services.deps
        self.user = local_user()
        self.scheduler = start_pollers(services)
        resumed = services.runtime.recover()
        if resumed:
            say(f"Resuming steps cut off by the last shutdown: {', '.join(resumed)}")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._http: Any = None
        if port:
            self._serve_http(port, say)

    def _serve_http(self, port: int, say: Callable[[str], None]) -> None:
        """The localhost endpoint (`pipeline say`, `pipeline status`) on its own event loop."""
        from aiohttp import web

        from pipeline.server import make_app

        app = make_app(self.services)

        async def start() -> Any:
            runner = web.AppRunner(app)
            await runner.setup()
            await web.TCPSite(runner, "127.0.0.1", port).start()
            return runner

        loop = asyncio.new_event_loop()
        threading.Thread(target=loop.run_forever, daemon=True, name="cli-http").start()
        try:
            self._http = asyncio.run_coroutine_threadsafe(start(), loop).result(10)
            self._loop = loop
        except OSError as err:
            loop.call_soon_threadsafe(loop.stop)
            say(f"The local endpoint isn't available ({err}); `pipeline say` won't reach this "
                "terminal.")  # fmt: skip

    def send(self, text: str) -> dict:
        chat = self.deps.config.chat
        try:
            cmd = parse_command(text, actor_id=self.user, actor_name=self.user, source="cli",
                                agents=chat.agents)  # fmt: skip
        except CommandParseError as err:
            return {"accepted": False, "message_md": cli_hint(text, err, self.prefix)}
        result = self.services.bus.handle(cmd)
        said = voiced(cmd, result.message_md, chat.agents)
        return {**result.model_dump(), "key": cmd.key, "verb": cmd.verb, "message_md": said}

    @property
    def prefix(self) -> str:
        return self.deps.config.jira.project

    def busy(self, key: str) -> bool:
        return self.services.runtime.snapshot(key).busy

    def waiting(self, key: str) -> str | None:
        return self.services.runtime.snapshot(key).waiting

    def reset_deps(self) -> tuple[Any, Any]:
        return self.deps, self.services.runtime.checkpointer

    def close(self) -> None:
        self.scheduler.shutdown(wait=False)
        if self._loop is not None and self._http is not None:
            loop = self._loop
            asyncio.run_coroutine_threadsafe(self._http.cleanup(), loop).result(10)
            loop.call_soon_threadsafe(loop.stop)
        self.services.runtime.shutdown()


class RemoteBackend:
    """A running `pipeline serve`: commands over the localhost endpoint, messages followed."""

    def __init__(self, port: int, deps: Any, show: Callable[[str, str | None], None]) -> None:
        from pipeline.server import local_user

        self.port = port
        self.deps = deps
        self.user = local_user()
        self.show = show
        self._stop = threading.Event()
        first = self._get("/local/chat", {"after": 0}) or {"messages": []}
        self.seq = max([m["seq"] for m in first["messages"]], default=0)  # no history replay
        threading.Thread(target=self._follow, daemon=True, name="cli-follow").start()

    @property
    def prefix(self) -> str:
        return self.deps.config.jira.project

    def _get(self, path: str, params: dict) -> dict | None:
        import httpx

        try:
            r = httpx.get(f"http://127.0.0.1:{self.port}{path}", params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPError:
            return None

    def _follow(self) -> None:
        while not self._stop.is_set():
            data = self._get("/local/chat", {"after": self.seq})
            for m in (data or {}).get("messages", []):
                self.seq = max(self.seq, m["seq"])
                self.show(m["text"], m["key"])
            self._stop.wait(1)

    def send(self, text: str) -> dict:
        import httpx

        chat = self.deps.config.chat
        try:  # checked here, so a mistake gets the exact form
            parse_command(text, actor_id=self.user, actor_name=self.user, source="cli",
                          agents=chat.agents)  # fmt: skip
        except CommandParseError as err:
            return {"accepted": False, "message_md": cli_hint(text, err, self.prefix)}
        try:
            r = httpx.post(
                f"http://127.0.0.1:{self.port}/local/commands",
                timeout=60,
                json={"text": text, "actor_id": self.user, "actor_name": self.user},
            )
            r.raise_for_status()
        except httpx.HTTPError as err:
            return {"accepted": False, "message_md": f"The pipeline server didn't answer: {err}"}
        return r.json()

    def busy(self, key: str) -> bool:
        data = self._get("/local/chat", {"key": key, "after": 10**12})
        return bool(data and data.get("busy"))

    def waiting(self, key: str) -> str | None:
        data = self._get("/local/chat", {"key": key, "after": 10**12})
        return data.get("waiting") if data else None

    def reset_deps(self) -> tuple[Any, Any]:
        from pipeline.graph.checkpoint import make_checkpointer

        return self.deps, make_checkpointer(self.deps.home / "pipeline.db")

    def close(self) -> None:
        self._stop.set()


# ---------- the prompt ----------


class Terminal:
    def __init__(self, backend: Any, console: Console, history: Path | None = None) -> None:
        self.backend = backend
        self.console = console
        self.history = history

    def show(self, text: str, key: str | None = None) -> None:
        self.console.rule(f"[{key or 'pipeline'}] {time.strftime('%H:%M:%S')}", align="left",
                          style="dim")  # fmt: skip
        self.console.print(Markdown(text))

    def handle(self, text: str, ask: Callable[[str], bool],
               prompt: Callable[[str], str] | None = None,
               choose: Choose | None = None) -> bool:
        """One line typed at the prompt. False = leave. `prompt` / `choose`: how `implement`
        asks for the tech spec link and ticks the repositories (D202)."""
        words = text.lstrip("/").split()
        if not words:
            return True
        if words[0].lower() in EXIT_WORDS:
            return False
        if words[0].lower() == "reset":
            self.reset(words[1:], ask)
            return True
        if words[0].lower() == "implement" and prompt is not None and choose is not None:
            text = self.intake(text, words, prompt, choose)
            if text is None:
                return True
        if words[0].lower() == "fix" and prompt is not None:
            text = self.bug_tickets(text, words, prompt)
            if text is None:
                return True
        reply = self.backend.send(text)
        if reply.get("needs_confirmation"):
            if not ask(f"Go ahead with `{text}`?"):
                self.console.print("Cancelled. Nothing changed.")
                return True
            reply = self.backend.send(f"{text} --confirm")
        self.console.print(Markdown(reply.get("message_md") or ""))
        return True

    def bug_tickets(self, text: str, words: list[str],
                    prompt: Callable[[str], str]) -> str | None:
        """`fix KEY` while the ticket is in testing (D206): QA files each bug as its own Jira
        ticket, so ask for their numbers (`DEMO-13 DEMO-14`). None = nothing sent."""
        keys = [w.upper() for w in words[1:] if KEY_RE.match(w)]
        if len(keys) != 1 or [w for w in words[1:] if not KEY_RE.match(w)]:
            return text  # tickets already given, or numbers / scopes: the command handles it
        if self.backend.waiting(keys[0]) not in ("qa", "qa_fix"):
            return text
        try:
            answer = prompt(f"Bug ticket number(s) for {keys[0]}, for example "
                            f"{self.backend.prefix}-13 {self.backend.prefix}-14 "
                            "(empty = the bugs already recorded): ").strip()
        except (KeyboardInterrupt, EOFError):
            self.console.print("Cancelled. Nothing changed.")
            return None
        tickets = [w for w in answer.replace(",", " ").split() if w]
        bad = [w for w in tickets if not KEY_RE.match(w)]
        if bad:
            self.console.print(f"`{' '.join(bad)}` is not a ticket number (like "
                               f"{self.backend.prefix}-13). Nothing sent.")  # fmt: skip
            return None
        return f"{text} {' '.join(tickets)}" if tickets else text

    def intake(self, text: str, words: list[str], prompt: Callable[[str], str],
               choose: Choose) -> str | None:
        """`implement KEY` without `--spec` / `--repos` (D202): ask for the Google Doc link, check
        it, then let the person tick the repositories in scope (the config's `repos`). Returns
        the command to send, with both added; None = the ticket is not started (the reason is
        printed)."""
        from pipeline.intake import check_spec, repo_options

        flags = [w.lower().split("=")[0] for w in words]
        if "--spec" in flags or "--repos" in flags:
            return text  # given on the line: the command checks them itself
        key = next((w.upper() for w in words[1:] if KEY_RE.match(w)), None)
        if key is None or self.backend.busy(key):
            return text  # the command explains (no ticket named / already running)
        try:
            link = prompt("Please paste the tech spec link (Google Doc): ").strip()
        except (KeyboardInterrupt, EOFError):
            self.console.print("Cancelled. Nothing started.")
            return None
        with self.console.status("Checking the link…"):
            spec, why = check_spec(link, self.backend.deps.spec)
        if spec is None:
            print(f"Cannot implement {key}: {why}.")
            return None
        print("Link OK. Which repositories are in scope for this ticket?")
        names = choose(repo_options(self.backend.deps.config))
        if not names:
            print(f"Cannot implement {key}: no repository was chosen.")
            return None
        return f"{text} --spec {spec} --repos {','.join(names)}"

    def reset(self, args: list[str], ask: Callable[[str], bool]) -> None:
        from pipeline.maintenance import demo_reset, local_reset

        full = "--full" in [a.lower() for a in args]
        keys = [a for a in args if a.lower() != "--full"]
        if len(keys) != 1 or not KEY_RE.match(keys[0]) or len(args) > 2:
            prefix = self.backend.prefix
            self.console.print(f"Usage: `reset {prefix}-12` (here only) or `reset {prefix}-12 "
                               "--full` (also GitLab and Jira).")  # fmt: skip
            return
        key = keys[0].upper()
        if self.backend.busy(key):
            self.console.print(f"{key} is running a step; stop it first (`stop {key}`).")
            return
        what = ("close its merge requests, delete its branches, the bot's Jira comments and "
                "deployment notes, move Jira back to To Do, and forget it here" if full
                else "forget it here (checkpoints, tracking rows, worktrees); GitLab and Jira "
                "stay as they are")  # fmt: skip
        if not ask(f"Reset {key}: {what}?"):
            self.console.print("Cancelled. Nothing changed.")
            return
        deps, checkpointer = self.backend.reset_deps()
        if full:
            for line in demo_reset(deps, key, checkpointer).lines():
                self.console.print(line, markup=False)
            return
        problems = local_reset(deps, key, checkpointer)
        for p in problems:
            self.console.print(f"[yellow]{p}[/yellow]")
        self.console.print(f"{key} is reset here. GitLab and Jira were not changed.")

    def run(self) -> None:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory, InMemoryHistory
        from prompt_toolkit.patch_stdout import patch_stdout

        history = FileHistory(str(self.history)) if self.history else InMemoryHistory()
        session: PromptSession[str] = PromptSession(history=history)

        def ask(question: str) -> bool:
            try:
                answer = session.prompt(f"{question} [y/N] ")
            except (KeyboardInterrupt, EOFError):
                return False
            return answer.strip().lower() in ("y", "yes")

        def prompt(question: str) -> str:
            return session.prompt(question)

        def choose(options: list[tuple[str, str]]) -> list[str]:
            """The repositories in scope: a plain numbered list in the terminal, answered with
            numbers or names (`1,2`, `all`)."""
            from pipeline.intake import parse_repos

            for n, (_name, label) in enumerate(options, 1):
                print(f"  {n}. {label}")
            answer = session.prompt("Repositories in scope (numbers or names, comma-separated, "
                                    "or all): ")
            names, why = parse_repos(answer, self.backend.deps.config)
            if not names and answer.strip():
                print(f"  {why}")
            return names

        with patch_stdout(raw=True):  # messages print above the line being typed
            while True:
                try:
                    text = session.prompt(PROMPT).strip()
                except KeyboardInterrupt:
                    continue
                except EOFError:
                    break
                try:
                    if not self.handle(text, ask, prompt, choose):
                        break
                except Exception as err:  # one bad command must not end the terminal
                    self.console.print(f"[red]Something went wrong: {err}[/red]")
