"""Exact command grammar (CLAUDE.md §3.3) and the command bus.

The terminal (`pipeline cli`, `pipeline say`) and Jira comments all end up in `CommandBus.handle()`.
The bus records every command, answers `help`/`status` directly, and otherwise starts or
resumes the ticket's LangGraph thread through a `TicketRuntime` — but only when the command
matches what the thread is waiting for (the pending interrupt in the checkpoint, §9.10).
"""

from __future__ import annotations

import contextlib
import difflib
import re
import shlex
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from pipeline.bugs import open_bugs
from pipeline.chat.notifier import gate_label
from pipeline.chat.summaries import agent_name, agent_of, step_words
from pipeline.integrations.base import IntegrationError
from pipeline.schemas import Command, CommandResult

KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-\d+$")
FINDING_RE = re.compile(r"^[RH]\d+-[\w.-]+-F\d+$", re.I)
NUMBER_TOKEN_RE = re.compile(r"^#?(\d+)$")
FIX_SCOPES = ("all", "blockers", "major+", "minor")
# the words people type for a scope (D168): `major` = `major+`, `minor` = everything but nits
SCOPE_ALIASES = {"blocker": "blockers", "blockers": "blockers", "major": "major+",
                 "major+": "major+", "minor": "minor", "minor+": "minor", "minors": "minor",
                 "all": "all"}  # fmt: skip
PROVIDERS = ("claude", "codex")

# verb → the waits (pending interrupt "gate" values) it can resume
VERB_WAITS: dict[str, tuple[str, ...]] = {
    "approve_plan": ("plan_approval",),
    "revise_plan": ("plan_approval",),
    "review": ("review", "re_review"),
    # P7 (D126): during testing `fix 1 2` / `fix all` picks the QA bugs to fix
    "fix": ("fix", "qa", "qa_fix", "merge_all"),  # merge_all: the findings left open (D168)
    "accept": ("fix", "needs_human"),  # needs_human: only after a review stop (P9)
    # P6 2.6: also regenerates the note while the MRs wait to be merged
    "deploy_doc": ("deploy_doc", "merge_all"),
    "fix_bugs": ("qa", "qa_fix"),
    "bug": ("qa", "qa_fix"),
    "retry": ("needs_human", "migration"),
    # P10: run the live acceptance tests again (a migration was applied, or after a stop there)
    "test": ("migration", "needs_human"),
    # P4b: steer a failed/unclear step (then it reruns); at plan approval = revise plan;
    # D168: while the merge requests are open = a change request
    "guide": ("needs_human", "plan_approval", "review", "fix", "re_review", "merge_all"),
    # P5 §4b: a person's own change while the MRs are open (not merged; P6: also while they
    # wait to be merged, before the first merge)
    "change": ("review", "fix", "re_review", "merge_all"),
}
NEEDS_CONFIRMATION = frozenset({"accept", "stop", "pause"})
# the whole pipeline, not one ticket (P8 1.1, 3.3); pause / unpause: never from Jira
PIPELINE_VERBS = frozenset({"pause", "unpause", "report"})
# tickets.stage holds the graph node name; people read these words instead
STAGE_LABELS = {
    "load_context": "starting", "plan": "planning", "replan": "updating the plan",
    "plan_gate": "waiting for plan approval", "plan_gate_wait": "waiting for plan approval",
    "code_next_repo": "coding", "verify": "running the automated checks",
    "open_mrs": "opening merge requests", "needs_human": "waiting for help",
    "needs_human_wait": "waiting for help",
    "review_gate": "waiting for someone to start the review",
    "review_gate_wait": "waiting for someone to start the review",
    "review": "reviewing the code", "fix_gate": "waiting for a decision on the review findings",
    "fix_gate_wait": "waiting for a decision on the review findings",
    "fix": "fixing the review findings", "push_fixes": "pushing the fixes",
    "re_review_gate": "waiting for someone to start the next review",
    "re_review_gate_wait": "waiting for someone to start the next review",
    "approve": "approving the merge requests", "change": "looking at a change request",
    "deploy_doc_gate": "waiting for someone to ask for the deployment note",
    "deploy_doc_gate_wait": "waiting for someone to ask for the deployment note",
    "deploy_doc": "writing the deployment note",
    "wait_merge": "waiting for the merge requests to be merged",
        "post_qa_notes": "writing the test notes for QA", "wait_qa": "in testing",
    "env_check": "checking the live test environment", "acceptance": "running the live tests",
    "migration_wait": "waiting for its migration on the dev database",
    "qa_fix": "starting a QA round", "qa_done": "finished",
    "context_missing": "couldn't start (the Jira ticket is missing something)",
    "stopped": "stopped", "finished": "finished",
}  # fmt: skip
# what a command would do, for "… so I can't <words> right now"
VERB_WORDS = {
    "approve_plan": "approve the plan", "revise_plan": "change the plan",
    "review": "start a review", "fix": "fix review findings", "accept": "accept it as it is",
    "deploy_doc": "write the deployment note", "devops_done": "mark the DevOps work as done",
    "bug": "record a bug", "fix_bugs": "fix the bugs", "retry": "try the step again",
    "guide": "use your guidance", "change": "make that change",
    "pause": "pause the pipeline", "unpause": "resume the pipeline",
    "test": "run the live tests again",
}  # fmt: skip
# After a ticket couldn't start (nothing planned or coded yet), these start it again.
RESTART_VERBS = frozenset({"retry", "guide"})
REPO_STAGES = frozenset({"code_next_repo", "verify"})  # the status line names the repo
COMMAND_WORDS = (
    "implement", "approve", "approve plan", "revise plan", "review", "fix", "accept",
    "deploy-doc", "devops-done", "bug", "fix bugs", "status", "stop", "retry", "guide", "change",
    "help", "pause", "unpause", "report", "test",
)  # fmt: skip
KEYLESS_OK = frozenset({"help", "status"}) | PIPELINE_VERBS

# CLI mode (D167): the exact commands, grouped (D168). `help` shows them; a mistake shows the
# lines of its verb. KEY is the ticket key placeholder (the project's prefix fills it in).
USAGE: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("Start", (
        ("implement KEY [--spec LINK --repos a,b] [--provider claude|codex] [--slot Coder-B] "
         "[testing=true]",
         "start a ticket; in the terminal it asks for the tech spec link and the repositories "
         "(elsewhere give --spec and --repos); testing=true adds the live acceptance tests "
         "(off by default)"),
    )),
    ("Steer", (
        ("approve KEY", "approve the plan, so coding starts"),
        ("guide KEY: <text>", "your input: revise the plan, help a stopped step, or a change "
                              "while the merge requests are open"),
    )),
    ("Review", (
        ("review KEY", "start the review (only when the review gate is manual)"),
        ("fix KEY [blockers|major|minor|all] [: notes]", "fix by severity (default: major); "
                                                        "after the approval, the ones left open"),
        ("fix KEY #1 #3 [dismiss #2] [: notes]", "fix these findings; dismiss the ones you "
                                                 "reject, with your reason"),
        ("accept KEY", "approve without fixing the rest (a recorded override)"),
    )),
    ("Delivery", (
        ("deploy-doc KEY", "write the deployment note again"),
    )),
    ("QA", (
        ("bug KEY: <description>", "record a bug (QA usually writes them on Jira)"),
        ("fix KEY DEMO-13 DEMO-14", "fix the bugs filed as those Jira tickets (in testing; "
                              "`fix KEY` asks for them)"),
        ("fix KEY 1 2 · fix KEY all", "fix bugs already recorded (in testing)"),
    )),
    ("Recovery", (
        ("retry KEY", "run the step that stopped again"),
        ("test KEY", "run the live acceptance tests again (e.g. after its migration is on dev)"),
        ("stop KEY", "halt the ticket"),
        ("reset KEY [--full]", "forget the ticket here; --full also closes its merge requests, "
                               "deletes its branches and bot comments, Jira back to To Do"),
    )),
    ("Information", (
        ("status [KEY]", "every ticket in flight, or one"),
        ("report", "tickets, waiting time, runs, cost"),
        ("help", "this list"),
    )),
    ("Admin", (
        ("pause [: reason] · unpause", "hold or release the whole pipeline"),
        ("exit", "leave the terminal (running steps carry on at the next start)"),
    )),
)  # fmt: skip


def usage_md(prefix: str = "DEMO") -> str:
    """`help` in CLI mode: every exact command, grouped."""
    key = f"{prefix}-12"
    lines = ["**Commands** (a leading `/` is fine; `stop`, `accept` and `pause` ask you to "
             "confirm, `--confirm` skips that)"]  # fmt: skip
    for group, items in USAGE:
        lines += ["", f"**{group}**", "", "| Command | What it does |", "|---|---|"]
        cells = [(c.replace("KEY", key).replace("|", "\\|"), w) for c, w in items]
        lines += [f"| `{c}` | {w} |" for c, w in cells]
    return "\n".join(lines)


def cli_hint(text: str, err: Exception, prefix: str = "DEMO") -> str:
    """CLI mode (D167): no Chat LLM, so a mistake gets the exact form instead: the usage of
    the verb it starts with, or the closest command."""
    key = f"{prefix}-12"
    words = text.strip().lstrip("/").lower().split()
    first = words[0] if words else ""
    lines = [c.replace("KEY", key) for _, items in USAGE for c, _ in items
             if c.split()[0] == first]  # fmt: skip
    if lines:
        usage = "\n".join(f"- `{u}`" for u in lines)
        return f"{err}\n\nUsage:\n{usage}"
    if not isinstance(err, UnknownCommandError):  # a known verb (an older name) with a mistake
        return str(err)
    close = difflib.get_close_matches(" ".join(words[:2]), COMMAND_WORDS, n=1, cutoff=0.6) or \
        difflib.get_close_matches(first, COMMAND_WORDS, n=1, cutoff=0.6)  # fmt: skip
    hint = f" Did you mean `{close[0]}`?" if close else ""
    return f"`{text.strip()}` isn't a command.{hint} Type `help` for the list."


class CommandParseError(ValueError):
    """Text is not an exact command. The message is meant for the user."""


class UnknownCommandError(CommandParseError):
    """The text doesn't start with a known verb: free text, a job for the intent agent (P4)."""


class ExtraWordsError(UnknownCommandError):
    """A known verb followed by words the grammar doesn't take ("stop DEMO-1 for now please",
    "fix the blockers"): natural language, so the Chat LLM gets it (P4b). A missing key or
    colon text stays a plain `CommandParseError` with the grammar's hint."""


# ---------- parser ----------


def suggest(text: str) -> str:
    """Short help with the closest commands (3.5)."""
    words = text.lower().split()
    probes = [" ".join(words[:2]), words[0]] if words else []
    close: list[str] = []
    for p in probes:
        # 0.6: typos still match ("statsu", "aprove plan"); "thanks" doesn't become `change`
        for c in difflib.get_close_matches(p, COMMAND_WORDS, n=3, cutoff=0.6):
            if c not in close:
                close.append(c)
    hint = f" Did you mean “{close[0]}”?" if close else ""
    return (f"Sorry, I didn't understand that.{hint} Tell me in your own words, or ask me what I "
            "can do.")  # fmt: skip


def _strip_mention(text: str, agents: dict[str, str] | None = None) -> str:
    return split_address(text.strip(), agents)[1]


# ---------- the team inside the bot (D111) ----------

# which agent does a command's work (and answers it); None = anyone (status, help, stop)
COMMAND_AGENT: dict[str, str | None] = {
    "implement": "coder", "approve_plan": "coder", "revise_plan": "coder", "guide": "coder",
    "retry": "coder", "change": "coder", "fix": "coder", "fix_bugs": "coder", "bug": "coder",
    "review": "reviewer", "accept": "reviewer",
    "deploy_doc": "devops", "devops_done": "devops",
    "status": None, "help": None, "stop": None, "pause": None, "unpause": None, "report": None,
}  # fmt: skip


def split_address(text: str, agents: dict[str, str] | None) -> tuple[str | None, str]:
    """ "@Reviewer please review" / "reviewer, please review" → ("reviewer", "please review")."""
    for agent, name in sorted((agents or {}).items(), key=lambda kv: -len(kv[1])):
        # "DevOps" addresses DevOps, but "devops-done" is the command (no hyphen after a name)
        m = re.match(rf"^\s*@?{re.escape(name)}(?![\w-])[\s,:]*", text, flags=re.I)
        if m and text[m.end() :].strip():
            return agent, text[m.end() :].strip()
    return None, text


def addressed_agent(text: str, agents: dict[str, str] | None) -> str | None:
    return split_address(text.strip(), agents)[0]


def voiced(cmd: Command, message_md: str, agents: dict[str, str]) -> str:
    """The reply as the team (D111): the agent that owns the command answers ("**Reviewer:**
    Starting the review."); if the person asked another agent, it hands over first. `help`,
    `status` and `stop` answer as the agent asked (plain when nobody was named)."""
    if not message_md:
        return message_md
    asked = addressed_agent(cmd.raw, agents)
    owner = COMMAND_AGENT.get(cmd.verb)
    speaker = agent_of(owner, agents) if owner else asked
    if speaker is None:
        return message_md
    text = f"**{agent_name(speaker, agents)}:** {message_md}"
    if asked is not None and asked != speaker:
        text = (f"**{agent_name(asked, agents)}:** That one's for "
                f"{agent_name(speaker, agents)}, passing it on.\n\n{text}")  # fmt: skip
    return text


def _split_colon(text: str) -> tuple[str, str | None]:
    head, sep, tail = text.partition(":")
    return (head.strip(), tail.strip()) if sep else (text.strip(), None)


def _take_key(tokens: list[str], thread_key: str | None) -> str | None:
    if tokens and KEY_RE.match(tokens[0]):
        return tokens.pop(0).upper()
    return thread_key


def parse_command(
    text: str,
    *,
    actor_id: str,
    actor_name: str,
    source: str,
    thread_key: str | None = None,
    agents: dict[str, str] | None = None,
) -> Command:
    """Exact grammar → `Command`. Case-insensitive; tolerant of a
    team member's name (`@Reviewer review DEMO-3`, `coder, fix #1`; D111)."""
    raw = text
    body = _strip_mention(text, agents)
    body = body[1:].lstrip() if body.startswith("/") else body  # `/retry DEMO-12` (D167)
    if not body:
        raise CommandParseError("What would you like me to do? Tell me in your own words.")
    # `implement KEY --spec <link>`: the link has colons, so it is taken out before the grammar
    spec_url = None
    if re.match(r"\s*implement\b", body, re.I):
        found = re.search(r"\s--spec[\s=]+(\S+)", body)
        if found:
            spec_url = found.group(1)
            body = (body[: found.start()] + body[found.end():]).strip()
    # a Confirm button appends `--confirm` after any colon text (`bug K: … --confirm`)
    trailing = re.search(r"\s+--(?:confirm|yes)\s*$", body) if ":" in body else None
    if trailing:
        body = body[: trailing.start()]
    head, colon_text = _split_colon(body)
    try:
        tokens = shlex.split(head)
    except ValueError:
        tokens = head.split()
    if not tokens:
        raise CommandParseError("What would you like me to do? Tell me in your own words.")
    words = [t.lower() for t in tokens]
    args: dict[str, Any] = {}
    confirmed = False

    def flag(name: str) -> bool:
        if f"--{name}" in words:
            i = words.index(f"--{name}")
            del tokens[i], words[i]
            return True
        return False

    def option(name: str) -> str | None:
        if f"--{name}" in words:
            i = words.index(f"--{name}")
            if i + 1 >= len(tokens):
                raise CommandParseError(f"`--{name}` needs a value.")
            value = tokens[i + 1]
            del tokens[i : i + 2], words[i : i + 2]
            return value
        return None

    confirmed = flag("confirm") or flag("yes") or bool(trailing)
    first = words[0]
    two = " ".join(words[:2])

    if two in ("approve plan", "revise plan", "fix bugs"):
        verb = two.replace(" ", "_")
        rest = tokens[2:]
    elif first == "approve":  # `approve DEMO-12`: only ever the plan (D168)
        verb = "approve_plan"
        rest = tokens[1:]
    elif first in ("deploy-doc", "deploy_doc", "devops-done", "devops_done"):
        verb = first.replace("-", "_")
        rest = tokens[1:]
    elif first in ("implement", "review", "fix", "accept", "bug", "status", "stop", "retry",
                   "guide", "change", "help", "pause", "unpause", "report", "test"):  # fmt: skip
        verb = first
        rest = tokens[1:]
    else:
        raise UnknownCommandError("Sorry, I didn't understand that. Tell me in your own words.")

    if verb == "implement":
        provider = option("provider")
        slot = option("slot")
        repos = option("repos")
        testing = option("testing")
        if spec_url:
            args["spec_url"] = spec_url
        if repos is not None:
            args["repos"] = [r for r in re.split(r"[,\s;]+", repos) if r]
        if flag("no-testing"):
            testing = "false"
        for i, tok in enumerate(tokens):  # `testing=false` (P10): this ticket skips the live tests
            m = re.fullmatch(r"(?:--)?testing=(\S+)", tok, re.I)
            if m:
                testing = m.group(1)
                del tokens[i], words[i]
                break
        rest = tokens[1:]
        if testing is not None:
            if testing.lower() in ("false", "off", "no", "0"):
                args["testing"] = False
            elif testing.lower() in ("true", "on", "yes", "1"):
                args["testing"] = True
            else:
                raise CommandParseError("`testing` is `true` or `false`: for example "
                                        "“implement DEMO-12 testing=false”.")  # fmt: skip
        if provider is not None:
            if provider.lower() not in PROVIDERS:
                raise CommandParseError(f"Unknown provider `{provider}`; use claude or codex.")
            args["provider"] = provider.lower()
        if slot is not None:
            args["slot"] = slot
        if not rest or not KEY_RE.match(rest[0]):
            raise CommandParseError("Which ticket should I start? For example “start DEMO-12”.")
    key = None if verb in PIPELINE_VERBS else _take_key(rest, thread_key)

    if verb == "revise_plan":
        if not colon_text:
            raise CommandParseError("What should I change in the plan? Just tell me.")
        args["feedback"] = colon_text
    elif verb == "bug":
        if not colon_text:
            raise CommandParseError("What is the bug? Describe it in your own words.")
        args["text"] = colon_text
    elif verb == "guide":
        if not colon_text:
            raise CommandParseError("What should I do differently? Tell me in your own words.")
        args["text"] = colon_text
    elif verb == "change":
        if not colon_text:
            if rest:  # "change the plan of DEMO-1", "change it to 204": natural language (D95)
                raise ExtraWordsError(f"Unexpected `{' '.join(rest)}` after `change`.")
            raise CommandParseError("What would you like to change? Tell me in your own words.")
        args["text"] = colon_text
    elif verb == "pause":  # `pause: demo break` (the reason is optional)
        if colon_text:
            args["text"] = colon_text
    elif verb in ("fix", "fix_bugs"):  # `fix bugs 1 2` works like `fix 1 2` in testing
        args.update(_fix_args(rest, colon_text))
        rest = []
    elif colon_text is not None:
        raise ExtraWordsError(f"`{verb.replace('_', ' ')}` doesn't take text after a colon.")
    if verb not in ("fix", "fix_bugs") and rest:
        raise ExtraWordsError(f"Unexpected `{' '.join(rest)}` after `{verb.replace('_', ' ')}`.")

    if key is None and verb not in KEYLESS_OK:
        raise CommandParseError(
            "Which ticket do you mean? Name it (for example DEMO-12), or write in the "
            "ticket's thread."
        )
    return Command(
        verb=verb, key=key, args=args, actor_id=actor_id, actor_name=actor_name,
        source=source, raw=raw, confirmed=confirmed,
    )  # fmt: skip


def _fix_args(tokens: list[str], colon_text: str | None) -> dict[str, Any]:
    """`fix [all|blockers|major+|#1 #3|R1-…-F2] [: notes]`, or the card's `fix KEY: 1, 3 —
    notes`: a scope, or finding numbers / ids (P5 3.2), plus optional notes for the coder."""
    from pipeline.findings import dismissed_in

    ids: list[str] = []
    numbers: list[int] = []
    dismissed: list[int] = []  # P9: `dismiss #2`: a person says it's fine (never guessed)
    dismissing = False
    bug_tickets: list[str] = []  # testing: the Jira tickets that hold the QA bugs
    scope: str | None = None
    notes = (colon_text or "").strip()
    for tok in (t for part in tokens for t in part.split(",") if t):
        low = tok.lower()
        if low in ("and", "&"):
            continue
        if KEY_RE.match(tok):
            bug_tickets.append(tok.upper())
        elif low == "dismiss":
            dismissing = True
        elif low in SCOPE_ALIASES:
            scope = SCOPE_ALIASES[low]
        elif FINDING_RE.match(tok):
            ids.append(tok.upper())
        elif NUMBER_TOKEN_RE.match(tok):
            n = int(NUMBER_TOKEN_RE.match(tok).group(1))  # type: ignore[union-attr]
            (dismissed if dismissing else numbers).append(n)
        else:
            raise ExtraWordsError(
                f"`{tok}` is not a fix scope (blockers, major, minor, all), a finding number or id."
            )
    if not (ids or numbers or scope) and notes:  # the card's text box: "1, 3: …" / "all: …"
        first, _, after = notes.partition(" ")
        if first.lower().rstrip(":,;") in SCOPE_ALIASES:
            scope = SCOPE_ALIASES[first.lower().rstrip(":,;")]
            notes = after.lstrip(" :—–-;").strip()
        else:
            from pipeline.findings import parse_selection

            numbers, ids, notes = parse_selection(notes)
    if (ids or numbers) and scope:
        raise CommandParseError(
            "Tell me either which findings to fix (their numbers) or “all” / “the blockers”, "
            "not both."
        )
    out: dict[str, Any] = {}
    if bug_tickets:
        out["bug_tickets"] = list(dict.fromkeys(bug_tickets))
    if ids or numbers:
        out["scope"] = "ids"
        if ids:
            out["finding_ids"] = ids
        if numbers:
            out["finding_numbers"] = list(dict.fromkeys(numbers))
    elif scope:
        out["scope"] = scope
    dismissed += dismissed_in(notes)
    if dismissed:
        out["dismissed_numbers"] = list(dict.fromkeys(dismissed))
    if notes:
        out["notes"] = notes
    return out


def command_text(cmd: Command) -> str:
    """Canonical text of a command (used for confirmation prompts)."""
    verb = cmd.verb.replace("_", " ").replace("deploy doc", "deploy-doc")
    verb = verb.replace("devops done", "devops-done")
    parts = [verb, cmd.key or ""]
    if cmd.verb == "fix":
        picked = [f"#{n}" for n in cmd.args.get("finding_numbers", [])]
        picked += cmd.args.get("finding_ids", [])
        parts.append(" ".join(picked) or cmd.args.get("scope", ""))
        if cmd.args.get("dismissed_numbers"):
            parts.append("dismiss " + " ".join(f"#{n}" for n in cmd.args["dismissed_numbers"]))
    text = " ".join(p for p in parts if p)
    if cmd.verb == "implement":
        for opt in ("provider", "slot"):
            if cmd.args.get(opt):
                text += f" --{opt} {cmd.args[opt]}"
        if cmd.args.get("spec_url"):
            text += f" --spec {cmd.args['spec_url']}"
        if cmd.args.get("repos"):
            text += " --repos " + ",".join(cmd.args["repos"])
        if cmd.args.get("testing") is True:
            text += " testing=true"
    colon = cmd.args.get("feedback") or cmd.args.get("text") or cmd.args.get("notes")
    return f"{text}: {colon}" if colon else text


# ---------- runtime interface ----------


@dataclass
class ThreadInfo:
    exists: bool = False
    finished: bool = False
    waiting: str | None = None  # pending interrupt's "gate" (plan_approval, needs_human, …)
    busy: bool = False  # a job is queued or running for this ticket
    values: dict = field(default_factory=dict)


class TicketRuntime(Protocol):
    def snapshot(self, key: str) -> ThreadInfo: ...
    def start(self, key: str, state: dict) -> None: ...
    def resume(self, key: str, payload: dict) -> None: ...
    def request_stop(self, key: str, by: str) -> None: ...
    def continue_to_review(self, key: str, by: str) -> None: ...


# ---------- bus ----------


class CommandBus:
    def __init__(self, deps: Any, runtime: TicketRuntime) -> None:
        self.deps = deps
        self.runtime = runtime

    def handle(self, cmd: Command) -> CommandResult:
        result = self._dispatch(cmd)
        reason = None if result.accepted else _first_line(result.message_md)
        self.deps.store.record_command(cmd, result.accepted, reason)
        return result

    # ----- dispatch -----

    def _dispatch(self, cmd: Command) -> CommandResult:
        if cmd.verb == "help":
            return CommandResult(accepted=True, message_md=self.help_text())
        if cmd.verb == "status":
            return CommandResult(accepted=True, message_md=self.status_text(cmd.key))
        if cmd.verb in PIPELINE_VERBS:
            return self._pipeline(cmd)
        assert cmd.key is not None
        if cmd.verb in NEEDS_CONFIRMATION and not cmd.confirmed:
            return CommandResult(
                accepted=False,
                needs_confirmation=True,
                message_md=(
                    f"Please confirm first: send `{command_text(cmd)} --confirm` to go ahead."
                ),
            )
        if cmd.verb == "implement":
            return self._implement(cmd)
        if cmd.verb == "devops_done":  # D124: DevOps works before the merges
            return _reject(DEVOPS_DONE_GONE)
        info = self.runtime.snapshot(cmd.key)
        stage = self._stage_of(cmd.key) if info.finished else None
        if cmd.verb in RESTART_VERBS and stage == "context_missing":
            # "I fixed the ticket, try again": same as `implement` (D97)
            return self._implement(cmd.model_copy(update={"verb": "implement", "args": {}}))
        if cmd.verb == "stop":
            return self._stop(cmd, info)
        if cmd.verb == "review" and info.finished and stage == "open_mrs" and not info.busy:
            return self._review_finished(cmd)
        if cmd.verb == "deploy_doc" and info.finished and stage == "approve" and not info.busy:
            return self._deploy_finished(cmd)
        if (
            cmd.verb == "change"
            and info.finished
            and stage in ("approve", "post_qa_notes", "finished")
        ):
            return _reject(  # 4b.3: after approval / merge it's new work
                f"The merge requests for {cmd.key} are already approved, so I can't change them "
                "any more. Please create a new ticket, or report a bug once it's in testing."
            )
        if not info.exists and not info.busy:  # busy + no checkpoint yet = just started
            return _reject(f"I haven't started {cmd.key} yet. Just ask me to start it.")
        if cmd.verb == "fix" and not info.busy and info.waiting in FIX_AS_CHANGE:
            cmd = fix_after_approval(cmd)
        if cmd.verb == "guide" and not info.busy and info.waiting in VERB_WAITS["change"]:
            cmd = cmd.model_copy(update={"verb": "change"})  # your input = a change (D168)
        waits = VERB_WAITS.get(cmd.verb, ())
        if cmd.verb == "bug" and info.waiting not in waits and \
                (info.values.get("qa_round") or 0) > 0:  # fmt: skip
            return _reject(
                f"I'm working on the last QA round for {cmd.key}. Please write the "
                "bug on the Jira ticket: I'll read it when it's back in testing."
            )
        if info.busy or info.waiting not in waits:
            return _reject(self._mismatch(cmd, info, waits))
        if cmd.verb == "fix" and info.waiting in ("fix", "merge_all"):
            problem = fix_choice_problem(cmd, info.values, self.deps.config)
            if problem:
                return _reject(problem)
        if cmd.verb == "test" and info.waiting == "needs_human":  # P10: = retry the live tests
            failed = (info.values.get("last_event") or {}).get("node")
            if failed not in ("acceptance", "env_check"):
                where = step_words(failed)
                return _reject(f"{cmd.key} stopped during {where}, not in the live tests. Tell "
                               "me to try again, give guidance, or stop it.")  # fmt: skip
            cmd = cmd.model_copy(update={"verb": "retry"})
        if cmd.verb == "accept" and info.waiting == "needs_human":
            from pipeline.graph.nodes.human import ACCEPT_AT

            failed = (info.values.get("last_event") or {}).get("node")
            if failed not in ACCEPT_AT:
                return _reject(f"{cmd.key} stopped during {step_words(failed)}, not in the review, "
                               "so there's nothing to accept. Tell me to try again, give "
                               "guidance, or stop it.")  # fmt: skip
        if info.waiting == "merge_all":
            problem = merge_wait_problem(cmd, info.values)
            if problem:
                return _reject(problem)
        if cmd.verb == "change":  # at once, before any planning (D131)
            problem = change_limit_problem(cmd, info.values, self.deps.config)
            if problem:
                return _reject(problem)
        if cmd.verb == "guide" and info.waiting == "plan_approval":  # = revise plan (P4b 4.1)
            feedback = {"feedback": cmd.args.get("text", "")}
            cmd = cmd.model_copy(update={"verb": "revise_plan", "args": feedback})
        payload = {
            "gate": info.waiting,
            "action": cmd.verb,
            "by": cmd.actor_name,
            "actor_id": cmd.actor_id,
            "source": cmd.source,
            "args": dict(cmd.args),
        }
        if (info.waiting in ("qa", "qa_fix") and cmd.verb in ("fix", "fix_bugs")
                and not cmd.args.get("bug_tickets")
                and not open_bugs(info.values.get("bugs"))):  # fmt: skip
            return _reject(f"Which tickets hold the bugs? Give their numbers: "
                           f"`fix {cmd.key} {self.deps.config.jira.project}-13 "
                           f"{self.deps.config.jira.project}-14`.")
        if not self._resume(cmd.key, payload):
            return _reject(f"I'm busy with {cmd.key} right now. Try again when it pauses.")
        if info.waiting in ("qa", "qa_fix") and cmd.verb in ("fix", "fix_bugs"):
            return CommandResult(accepted=True, message_md="On it: I'm reading the bug tickets "
                                 "and will plan the fixes.")  # fmt: skip
        return CommandResult(accepted=True, message_md=_verb_ack(cmd))

    def _pipeline(self, cmd: Command) -> CommandResult:
        """pause / unpause / report: the whole pipeline (P8 1.1, 3.3)."""
        from pipeline.pause import clear_pause, pause_info, paused_words, set_pause

        if cmd.verb == "report":
            from pipeline.report import chat_summary

            return CommandResult(accepted=True, message_md=chat_summary(self.deps))
        if cmd.source == "jira":
            return _reject(f"Only a person in the terminal can {VERB_WORDS[cmd.verb]}.")
        home = self.deps.home
        if cmd.verb == "pause":
            if not cmd.confirmed:
                return CommandResult(accepted=False, needs_confirmation=True, message_md=(
                    f"Please confirm first: send `{command_text(cmd)} --confirm` to go ahead."
                ))  # fmt: skip
            reason = str(cmd.args.get("text") or "")
            if not set_pause(home, cmd.actor_name, self.deps.clock().isoformat(timespec="seconds"),
                             reason):  # fmt: skip
                info = pause_info(home) or {}
                return _reject(f"The pipeline is already {paused_words(info)}.")
            text = (f"{cmd.actor_name} paused the pipeline"
                    + (f" ({reason})" if reason else "") + ". Nothing new starts until someone "
                    "resumes it; tickets carry on from where they are after that.")  # fmt: skip
            self._announce(cmd, text)
            return CommandResult(accepted=True, message_md=text)
        was = clear_pause(home)
        if was is None:
            return _reject("The pipeline isn't paused.")
        text = f"{cmd.actor_name} resumed the pipeline. Waiting tickets carry on now."
        self._announce(cmd, text)
        return CommandResult(accepted=True, message_md=text)

    def _announce(self, cmd: Command, text: str) -> None:
        """A pipeline-wide message in the main channel."""
        chat = getattr(self.deps, "chat", None)
        if chat is None:
            return
        with contextlib.suppress(Exception):  # the pause is done; the message is a courtesy
            chat.post(None, text)

    def _implement(self, cmd: Command) -> CommandResult:
        key = cmd.key
        assert key is not None
        prefix = self.deps.config.jira.project
        if not key.startswith(f"{prefix}-"):
            return _reject(f"`{key}` is not a {prefix} ticket. Ticket keys look like {prefix}-12.")
        slot = cmd.args.get("slot")
        if slot and slot not in {s.name for s in self.deps.config.agent_pool["coder"]}:
            names = ", ".join(s.name for s in self.deps.config.agent_pool["coder"])
            return _reject(f"There is no coder called `{slot}`. The coders are: {names}.")
        info = self.runtime.snapshot(key)
        if info.exists and not info.finished:
            return _reject(f"I'm already working on {key}.\n\n{self.status_text(key)}")
        missing = self._missing_in_jira(key)
        if missing:
            return _reject(missing)
        refused = self._take_intake(cmd)
        if refused:
            return _reject(refused)
        if self.deps.store.mrs_for(key):
            return _reject(
                f"{key} already has merge requests. To start over, a developer can run "
                f"`pipeline reset {key}`."
            )
        state: dict[str, Any] = {"key": key, "started_by": cmd.actor_name}
        # P10: live tests are off unless asked for (`testing=true`; default changed 2026-10-21)
        state["testing"] = cmd.args.get("testing") is True
        if cmd.args.get("provider"):
            state["provider_override"] = cmd.args["provider"]
        if slot:
            state["preferred_slots"] = {"coder": slot}
        self.runtime.start(key, state)
        # no reply of its own: the ticket's first message ("I've started on …") follows at once
        return CommandResult(accepted=True, message_md="")

    def _review_finished(self, cmd: Command) -> CommandResult:
        """`review` on a ticket that ended when its MRs opened (run before Phase 5 added the
        review gate): continue that thread into the review, if its MRs are still open."""
        key = cmd.key
        assert key is not None
        mrs = self.deps.store.mrs_for(key)
        if not mrs or any(m["state"] != "opened" for m in mrs):
            return _reject(f"{key}'s merge requests aren't all open any more, so I can't review "
                           "them. Start it again with a new ticket.")  # fmt: skip
        try:
            self.runtime.continue_to_review(key, cmd.actor_name)
        except RuntimeError:
            return _reject(f"I'm busy with {key} right now. Try again when it pauses.")
        return CommandResult(accepted=True, message_md=_verb_ack(cmd))

    def _deploy_finished(self, cmd: Command) -> CommandResult:
        """`deploy-doc` on a ticket that ended at `approve` (run before Phase 6 added the
        deployment note and the merge wait): continue that thread into the note, if its MRs are
        still open. Nothing is reviewed again."""
        key = cmd.key
        assert key is not None
        mrs = self.deps.store.mrs_for(key)
        if not mrs or any(m["state"] != "opened" for m in mrs):
            return _reject(f"{key}'s merge requests aren't all open any more, so I can't add a "
                           "deployment note to them.")  # fmt: skip
        try:
            self.runtime.continue_from(key, "deploy_doc_gate", "deploy_doc", cmd.actor_name)
        except RuntimeError:
            return _reject(f"I'm busy with {key} right now. Try again when it pauses.")
        return CommandResult(accepted=True, message_md=_verb_ack(cmd))

    def _take_intake(self, cmd: Command) -> str | None:
        """The tech spec link and the repositories (D202): what the person gave (checked: the
        link must be a Google Doc the pipeline can read, the repos must be configured ones)
        is kept as the ticket's `intake` note; else an earlier answer, or what the Jira ticket
        carries, is used. None = fine; else why the ticket can't start."""
        from pipeline.intake import check_spec, intake_of, parse_repos

        key = cmd.key
        assert key is not None
        store, config = self.deps.store, self.deps.config
        saved = intake_of(store, key)
        link = cmd.args.get("spec_url")
        repos = cmd.args.get("repos")
        if link is None and repos is None:
            if saved.get("spec_url") and saved.get("repos"):
                return None
            return None if self._jira_has_intake(key) else self._ask_for_intake(key)
        spec_url = saved.get("spec_url")
        if link is not None:
            spec_url, why = check_spec(link, self.deps.spec)
            if spec_url is None:
                return f"I can't start {key}: {why}."
        names = saved.get("repos")
        if repos is not None:
            names, why = parse_repos(",".join(repos), config)
            if not names:
                return f"I can't start {key}: {why}."
        if not spec_url or not names:
            return self._ask_for_intake(key)
        store.set_note(key, "intake", {"spec_url": spec_url, "repos": names,
                                       "by": cmd.actor_name})  # fmt: skip
        return None

    def _jira_has_intake(self, key: str) -> bool:
        """Does the Jira ticket itself name the repos and the spec (the old way: custom fields,
        `Repos:` / `Spec:` lines, or the epic's spec)? True too when Jira can't be read: let
        load_context report that properly."""
        from pipeline.context import repos_from_description, spec_urls_from_description

        jira = getattr(self.deps, "jira", None)
        if jira is None:
            return True
        try:
            issue = jira.get_issue(key)
        except IntegrationError:
            return True
        text = issue.get("description_md") or issue.get("description") or ""
        repos = issue.get("repos") or repos_from_description(text)
        spec = issue.get("spec_urls") or spec_urls_from_description(text) or issue.get("parent_key")
        return bool(repos and spec)

    def _ask_for_intake(self, key: str) -> str:
        from pipeline.intake import repo_options

        names = ", ".join(n for n, _ in repo_options(self.deps.config))
        return (f"I need the tech spec link and the repositories for {key}. In the terminal, "
                f"`implement {key}` asks for them; anywhere else: `implement {key} --spec "
                f"<Google Doc link> --repos a,b`. Repositories: {names}.")  # fmt: skip

    def _stage_of(self, key: str) -> str | None:
        return (self.deps.store.get_ticket(key) or {}).get("stage")

    def _missing_in_jira(self, key: str) -> str | None:
        """A clear answer now instead of a Needs-Human card from load_context later."""
        jira = getattr(self.deps, "jira", None)
        if jira is None:
            return None
        try:
            jira.get_issue(key)
        except IntegrationError as err:
            if "not found" not in str(err).lower() and "404" not in str(err):
                return None  # Jira hiccup: let load_context report it properly
            if getattr(self.deps, "dry_run", False):
                return (f"I can't find {key}: this is a **dry run**, whose fake Jira only has "
                        "the `--key` ticket of `pipeline serve --dry-run` (default DEMO-1). For "
                        "real tickets, run `pipeline serve` without `--dry-run`.")  # fmt: skip
            return f"I can't find {key} in Jira. Check the key and try again."
        return None

    def _stop(self, cmd: Command, info: ThreadInfo) -> CommandResult:
        key = cmd.key
        assert key is not None
        if not info.exists or info.finished:
            return _reject(f"I'm not working on {key}, so there is nothing to stop.")
        if info.waiting and not info.busy:
            payload = {"gate": info.waiting, "action": "stop", "by": cmd.actor_name,
                       "actor_id": cmd.actor_id, "source": cmd.source, "args": {}}  # fmt: skip
            if not self._resume(key, payload):
                self.runtime.request_stop(key, cmd.actor_name)
        else:
            self.runtime.request_stop(key, cmd.actor_name)
        return CommandResult(
            accepted=True,
            message_md=f"Stopping {key}. It will wait until someone decides what to do next.",
        )

    def _resume(self, key: str, payload: dict) -> bool:
        try:
            self.runtime.resume(key, payload)
        except RuntimeError:  # another command resumed it a moment ago
            return False
        return True

    # ----- replies -----

    def _mismatch(self, cmd: Command, info: ThreadInfo, waits: tuple[str, ...]) -> str:
        key = cmd.key or ""
        if info.busy:
            return f"I'm still working on {key} ({self._doing(key, info)}). I'll post here when " \
                   "I need you."  # fmt: skip
        action = VERB_WORDS.get(cmd.verb, f"`{cmd.verb.replace('_', ' ')}`")
        if info.waiting:
            return f"{key} is waiting for {gate_label(info.waiting)}, so I can't {action} " \
                   "right now."  # fmt: skip
        if info.finished:
            return f"{key} is {self._stage(key, info)}, so I can't {action} right now."
        return (f"{key} is {self._stage(key, info)} and isn't waiting for anything, so I can't "
                f"{action} right now.")  # fmt: skip

    def _doing(self, key: str, info: ThreadInfo, detailed: bool = False) -> str:
        """What a ticket is doing, in words: "coding demo-ws2, repository 1 of 2, for 6
        minutes". `detailed` (developer CLI) adds the agent slot and provider."""
        row = self.deps.store.get_ticket(key) or {}
        stage = row.get("stage") or ""
        text = self._stage(key, info)
        repos = sorted((info.values.get("repos") or {}).items(),
                       key=lambda kv: (kv[1].get("order", 99), kv[0]))  # fmt: skip
        current = next(((n, r, w) for n, (r, w) in enumerate(repos, 1)
                        if w.get("status", "pending") == "pending"), None)  # fmt: skip
        if stage in REPO_STAGES and current is not None:
            n, repo, _work = current
            text = f"{text} {repo}, repository {n} of {len(repos)}"
        for slot in self.deps.store.list_slots():
            if slot["busy_key"] == key:
                if detailed:
                    text += f", {slot['name']} ({slot['provider']})"
                since = slot.get("busy_since")
                if since is not None:
                    if since.tzinfo is None:
                        since = since.replace(tzinfo=UTC)
                    clock = getattr(self.deps, "clock", None)
                    now = clock() if clock else datetime.now(UTC)
                    if now.tzinfo is None:  # SQLite hands back naive UTC timestamps
                        now = now.replace(tzinfo=UTC)
                    minutes = int((now - since).total_seconds() // 60)
                    unit = "minute" if minutes == 1 else "minutes"
                    if minutes >= 1:
                        text += f", for {minutes} {unit}"
        return text

    def _stage(self, key: str, info: ThreadInfo) -> str:
        row = self.deps.store.get_ticket(key) or {}
        stage = row.get("stage") or ("finished" if info.finished else "starting")
        if stage == "open_mrs" and info.finished:
            return "done: its merge requests are open for review"
        if stage == "approve" and info.finished:
            return "approved: its merge requests are ready to merge"
        return STAGE_LABELS.get(stage, stage.replace("_", " "))

    def help_text(self) -> str:
        return usage_md(self.deps.config.jira.project)

    def status_text(self, key: str | None, detailed: bool = False) -> str:
        """Chat: plain sentences. `detailed` (the developer CLI `pipeline status`): slots,
        providers, rounds and cost too."""
        store = self.deps.store
        if key is None:
            tickets = [t for t in store.list_tickets() if not t.get("finished_at")]
            lines = ["**What I'm working on**", ""]
            if not tickets:
                lines.append("- Nothing right now.")
            for t in tickets:
                info = self.runtime.snapshot(t["key"])
                lines.append(f"- **{t['key']}**: {self._line(t['key'], info, detailed)}")
            slots = store.list_slots()
            if detailed:
                lines += ["", "**Agent slots**", ""]
                for s in slots:
                    busy = f"busy: {s['busy_key']} ({s['busy_role']})" if s["busy_key"] else "free"
                    lines.append(f"- {s['name']} ({s['pool']}, {s['provider']}): {busy}")
            else:
                coders = [s for s in slots if s["pool"] == "coder"]
                busy_n = sum(1 for s in coders if s["busy_key"])
                lines += ["", f"Coders busy: {busy_n} of {len(coders)}."]
            return "\n".join(lines)

        info = self.runtime.snapshot(key)
        row = store.get_ticket(key)
        if row is None and not info.exists:
            return f"I haven't started {key}."
        lines = [f"**{key}**: {self._line(key, info, detailed)}"]
        if row:
            if row.get("jira_status"):
                lines.append(f"- Jira status: {row['jira_status']}")
            if row.get("halted_reason") and info.waiting == "needs_human":
                lines.append(f"- Why it stopped: {row['halted_reason']}")
            rounds = [("review rounds", row["review_rounds"]), ("QA rounds", row["qa_rounds"]),
                      ("plan updates after coding started", row["replans"])]  # fmt: skip
            if detailed:
                lines.append(
                    f"- Repositories: {', '.join(row.get('repos') or []) or 'none'} · review "
                    f"rounds {row['review_rounds']} · QA rounds {row['qa_rounds']} · replans "
                    f"{row['replans']}" + f" · cost ${row['total_cost_usd']:.2f} (reported)"
                )
            else:
                done = [f"{n}: {v}" for n, v in rounds if v]
                if done:
                    lines.append("- " + " · ".join(done).capitalize())
        for mr in store.mrs_for(key):
            state = {"opened": "open", "merged": "merged", "closed": "closed"}.get(
                mr["state"], mr["state"])  # fmt: skip
            lines.append(f"- Merge request for {mr['repo']} ({state}): {mr['mr_url']}")
        if detailed:
            for s in [s for s in store.list_slots() if s["busy_key"] == key]:
                lines.append(f"- Slot {s['name']} ({s['provider']}) running {s['busy_role']}")
        return "\n".join(lines)

    def _line(self, key: str, info: ThreadInfo, detailed: bool = False) -> str:
        if info.busy:
            return self._doing(key, info, detailed)
        if info.waiting:
            return f"waiting for {gate_label(info.waiting)}"
        return self._stage(key, info)


def change_requests_made(values: dict) -> int:
    """A person's change requests on the ticket so far (kept in `guidance`, D104)."""
    return sum(1 for g in values.get("guidance") or [] if g.get("request"))


def change_limit_problem(cmd: Command, values: dict, config: Any) -> str | None:
    """A person's change requests don't count against `max_replans` (the Coder's own
    deviations); they share `max_plan_revisions` per ticket (D131)."""
    limit = config.limits.max_plan_revisions
    made = change_requests_made(values)
    if made < limit:
        return None
    return (f"{cmd.key} has had {made} change requests already, which is the limit per ticket. "
            "Please create a new ticket for this change, or ask a developer to raise the "
            "limit.")  # fmt: skip


# P9: once the MRs are approved and wait for the merges, "fix #2 …" is a person's change
# request on the same merge requests, not a refusal (DEMO-9)
FIX_AS_CHANGE = ("merge_all",)


def fix_after_approval(cmd: Command) -> Command:
    """While the merge requests wait to be merged (D168): a scope or finding numbers fix the
    findings the review left open (back into the fix loop); a bare `fix` takes all of them;
    words alone ("fix the date format") are a change request."""
    args = dict(cmd.args)
    if args.get("scope") or args.get("finding_numbers") or args.get("finding_ids"):
        return cmd
    if args.get("notes"):
        return cmd.model_copy(update={"verb": "change", "args": {"text": args["notes"]}})
    return cmd.model_copy(update={"args": {**args, "scope": "all"}})


def merge_wait_problem(cmd: Command, values: dict) -> str | None:
    """While the MRs wait to be merged: `change` only before the first merge (P6 4.1);
    `deploy-doc` any time until the last merge (2.6)."""
    repos = values.get("repos") or {}
    merged = sorted(r for r, w in repos.items() if w.get("status") == "merged")
    if cmd.verb in ("change", "fix") and merged:
        return (f"{', '.join(merged)} {'is' if len(merged) == 1 else 'are'} already merged, so "
                f"I can't change {cmd.key}'s merge requests any more. Please create a new ticket "
                "for the change, or report a bug once it's in testing.")  # fmt: skip
    return None


def fix_choice_problem(cmd: Command, state: dict, config: Any) -> str | None:
    """A `fix` choice that picks nothing, or numbers / ids that aren't open (3.2)."""
    from pipeline.findings import SCOPE_WORDS, in_scope, numbered, resolve_choice

    key = cmd.key
    rejected = set((state.get("fix_scope") or {}).get("disputed_by_human") or {})
    items = [(n, f) for n, f in numbered(state) if f.id not in rejected]
    valid = ", ".join(f"#{n}" for n, _ in items) or "none"
    scope = cmd.args.get("scope") or config.chat.default_fix_scope
    if scope == "ids":
        _ids, unknown = resolve_choice(state, cmd.args.get("finding_numbers") or [],
                                       cmd.args.get("finding_ids") or [])  # fmt: skip
        if unknown:
            return (f"{', '.join(unknown)} {'is' if len(unknown) == 1 else 'are'} not an open "
                    f"finding of {key}. The open findings are {valid}.")  # fmt: skip
        return None
    if not in_scope(scope, [f for _, f in items]):
        return (f"There are no open findings in {SCOPE_WORDS.get(scope, scope)} for {key}. "
                f"Choose another scope, or numbers ({valid}).")  # fmt: skip
    return None


DEVOPS_DONE_GONE = (
    "DevOps done isn't needed any more: DevOps does the steps on the deployment card before "
    "the merge requests are merged, and the ticket goes to testing once everything is merged."
)


def _reject(message: str) -> CommandResult:
    return CommandResult(accepted=False, message_md=message)


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0][:300] if text.strip() else ""


def _verb_ack(cmd: Command) -> str:
    return {
        "approve_plan": "Thanks, starting on the code now.",
        "revise_plan": "Got it, I'll update the plan.",
        "review": "Starting the review.",
        "fix": "On it, fixing the review findings.",
        "accept": "Accepted as it is.",
        "deploy_doc": "Writing the deployment note.",
        "fix_bugs": "On it, fixing the QA bugs.",
        "devops_done": "Thanks, I've noted that the DevOps work is done.",
        "bug": "Thanks, I've recorded the bug.",
        "retry": "Trying that step again.",
        "guide": "Thanks, I'll try that step again with your guidance.",
        "change": "Got it, I'll look at your change.",
        "test": "Running the live tests again.",
    }.get(cmd.verb, "Done.")
