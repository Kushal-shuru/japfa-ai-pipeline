"""Pollers: Jira comment commands and (gates.start == auto) Jira pickup (Phase 3 8.2); GitLab
merge states for tickets waiting for their merges (Phase 6 4.5).

Jira comments `/stop --confirm`, `/retry`, `/guide …` (P4b) go through the
command bus with `source="jira"`; the reply is posted back as a Jira comment (with a marker).
Comments carrying a pipeline marker are ours and are skipped. Only comments newer than the
poller's start are read, each once.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pipeline.chat.commands import CommandBus, CommandParseError, parse_command
from pipeline.integrations.base import IntegrationError
from pipeline.logging import get_logger
from pipeline.markers import is_pipeline_jira_comment, make_marker

JIRA_VERBS = ("stop", "retry", "guide")


def jira_command_text(body: str) -> str | None:
    """`/stop --confirm` → `stop --confirm`, for the first line starting with a known verb.
    `/guide <text>` (colon optional) takes the rest of the comment as the guidance."""
    lines = (body or "").splitlines()
    for n, line in enumerate(lines):
        line = line.strip()
        if not line.startswith("/"):
            continue
        verb, _, rest = line[1:].partition(" ")
        verb_l = verb.lower().rstrip(":")
        if verb_l not in JIRA_VERBS:
            continue
        if verb_l != "guide":
            return line[1:]
        more = " ".join(x.strip() for x in lines[n + 1 :] if x.strip())
        text = " ".join(p for p in (rest.strip(), more) if p)
        if ":" in verb or ":" in text.split(" ", 1)[0] or text.startswith(":"):
            return f"guide{verb[5:]} {text}".strip()  # already has the colon (`/guide: …`)
        return f"guide: {text}" if text else "guide"
    return None


CURSOR = "jira_comments"
OVERLAP = timedelta(minutes=5)  # read a little before the cursor: late or same-second comments


class JiraCommentPoller:
    """Jira commands (`/stop`, `/retry`, `/guide`). P9 (audit F8): the position per ticket is
    kept in the tracking DB (`ticket_notes`: the newest comment time + the ids handled in the
    overlap window), so a command written while the server was down is handled once after the
    restart. A ticket without a saved position starts at the poller's start (`since`), so an
    upgrade never replays old comments."""

    def __init__(self, deps: Any, bus: CommandBus, since: datetime | None = None) -> None:
        self.deps = deps
        self.bus = bus
        self.started = since or deps.clock()

    def tickets(self) -> list[str]:
        return [t["key"] for t in self.deps.store.list_tickets() if not t.get("finished_at")]

    def _cursor(self, key: str) -> tuple[datetime, set[str]]:
        saved = self.deps.store.get_note(key, CURSOR) or {}
        since = datetime.fromisoformat(saved["since"]) if saved.get("since") else self.started
        return since, set(saved.get("seen") or [])

    def _save(self, key: str, since: datetime, seen: dict[str, datetime]) -> None:
        keep = sorted(i for i, at in seen.items() if at > since - OVERLAP)
        self.deps.store.set_note(key, CURSOR, {"since": since.isoformat(), "seen": keep})

    def poll(self) -> int:
        """One pass over in-flight tickets. Returns the number of commands handled."""
        handled = 0
        for key in self.tickets():
            since, seen_ids = self._cursor(key)
            try:
                comments = self.deps.jira.comments_since(key, since - OVERLAP)
            except IntegrationError as err:
                get_logger(ticket=key).warning("jira_comment_poll_failed", error=str(err))
                continue
            seen = {i: since for i in seen_ids}
            newest = since
            for c in sorted(comments, key=lambda c: c["created"]):
                cid = str(c["id"])
                newest = max(newest, c["created"])
                if cid in seen or is_pipeline_jira_comment(c):
                    continue
                seen[cid] = c["created"]
                self._save(key, newest, seen)  # before handling: a crash never repeats it
                text = jira_command_text(c.get("body") or "")
                if text is None:
                    continue
                handled += 1
                self._handle(key, c, text)
            if newest != since or len(seen) != len(seen_ids):
                self._save(key, newest, seen)
        return handled

    def _handle(self, key: str, comment: dict, text: str) -> None:
        actor = comment.get("author") or "Jira user"
        try:
            cmd = parse_command(
                text, actor_id=comment.get("author_id") or actor, actor_name=actor,
                source="jira", thread_key=key,
            )  # fmt: skip
            reply = self.bus.handle(cmd).message_md
        except CommandParseError as err:
            reply = str(err)
        if text.split(" ", 1)[0].lower() == "stop":  # the Jira form of the confirming command
            reply = reply.replace(f"`stop {key} --confirm`", "`/stop --confirm`")
        marker = make_marker(key, "jira_cmd", 0, str(comment["id"]))
        try:
            self.deps.jira.comment(key, reply, marker)
        except IntegrationError as err:
            get_logger(ticket=key).warning("jira_reply_failed", error=str(err))


class JiraPickupPoller:
    """`gates.start == auto`: start tickets matching `jira.pickup_jql` that aren't running."""

    def __init__(self, deps: Any, bus: CommandBus) -> None:
        self.deps = deps
        self.bus = bus

    def poll(self) -> list[str]:
        if self.deps.config.gates.get("start") != "auto" or not self.deps.config.jira.pickup_jql:
            return []
        try:
            keys = self.deps.jira.search(self.deps.config.jira.pickup_jql)
        except IntegrationError as err:
            get_logger().warning("jira_pickup_failed", error=str(err))
            return []
        started = []
        for key in keys:
            if self.deps.store.get_ticket(key) is not None:
                continue  # known ticket: humans restart it explicitly
            cmd = parse_command(
                f"implement {key}", actor_id="jira-pickup", actor_name="Jira pickup",
                source="jira",
            )  # fmt: skip
            if self.bus.handle(cmd).accepted:
                started.append(key)
        return started


class GitLabMergePoller:
    """Tickets waiting for `merge_all`: read every MR's state and the commits people pushed
    (`waits.snapshot`), update `ticket_mrs`, and resume the thread with the snapshot when it
    brings news (a merge, a closed MR, new commits by people). The thread decides; a busy
    ticket is simply tried again at the next poll."""

    def __init__(self, deps: Any, runtime: Any) -> None:
        self.deps = deps
        self.runtime = runtime

    def tickets(self) -> list[str]:
        return [t["key"] for t in self.deps.store.list_tickets()
                if t.get("waiting_for") == "merge_all" and not t.get("finished_at")]  # fmt: skip

    def poll(self) -> list[str]:
        """One pass. Returns the tickets resumed."""
        from pipeline.graph.nodes.waits import MERGE_GATE, has_news, round_kind, snapshot

        resumed = []
        for key in self.tickets():
            log = get_logger(ticket=key)
            info = self.runtime.snapshot(key)
            if info.waiting != MERGE_GATE or info.busy:
                continue
            try:
                snap = snapshot(self.deps, info.values)
            except IntegrationError as err:
                log.warning("gitlab_merge_poll_failed", error=str(err))
                continue
            for repo, state in snap["states"].items():
                if state != "opened":
                    self.deps.store.upsert_mr(key, repo, round_kind(info.values), state=state)
            if not has_news(info.values, snap):
                continue
            payload = {"gate": MERGE_GATE, "action": "mr_update", "by": "GitLab",
                       "source": "gitlab", **snap}  # fmt: skip
            try:
                self.runtime.resume(key, payload)
            except RuntimeError:  # a command resumed it a moment ago
                continue
            resumed.append(key)
        return resumed


class QaPoller:
    """Tickets in testing (`qa` / `qa_fix`, D126), from Jira:

    - status **QA Passed** (people move it) → resume `qa_passed`: the ticket closes;
    - `gates.qa_fix: auto` and bugs open with no new one for `QUIET_MINUTES` → `fix all`.

    A busy ticket or a failed Jira read is simply tried again at the next poll."""

    def __init__(self, deps: Any, runtime: Any) -> None:
        self.deps = deps
        self.runtime = runtime

    def tickets(self) -> list[str]:
        testing = ("qa", "qa_fix")
        return [t["key"] for t in self.deps.store.list_tickets()
                if t.get("waiting_for") in testing and not t.get("finished_at")]  # fmt: skip

    def poll(self) -> list[str]:

        resumed = []
        statuses = self.deps.config.jira.statuses
        for key in self.tickets():
            info = self.runtime.snapshot(key)
            if info.waiting not in ("qa", "qa_fix") or info.busy:
                continue
            base = {"gate": info.waiting, "by": "Jira", "source": "jira", "args": {}}
            try:
                status = self.deps.jira.current_status(key)
                payload = None
                if status.casefold() == statuses.qa_passed.casefold():
                    payload = {**base, "action": "qa_passed"}
            except IntegrationError as err:
                get_logger(ticket=key).warning("qa_poll_failed", error=str(err))
                continue
            if payload is None and self._quiet_enough(info):
                payload = {**base, "action": "fix_bugs", "by": "auto", "args": {"scope": "all"}}
            if payload is None:
                continue
            try:
                self.runtime.resume(key, payload)
            except RuntimeError:
                continue
            resumed.append(key)
        return resumed

    def _quiet_enough(self, info: Any) -> bool:
        from pipeline import bugs as B

        if info.waiting != "qa_fix" or self.deps.config.gates.get("qa_fix") != "auto":
            return False
        opened = [b for b in B.open_bugs(info.values.get("bugs"))]
        if not opened:
            return False
        last = max(datetime.fromisoformat(b["at"]) for b in opened)
        now = self.deps.clock()
        if last.tzinfo is None:
            last = last.replace(tzinfo=UTC)
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        return (now - last).total_seconds() >= B.QUIET_MINUTES * 60


class MigrationPoller:
    """Tickets waiting for their migration on the dev database (P10, `migration_wait`): once the
    dev DB's `SequelizeMeta` lists every migration the wait named (a read-only query), resume the
    ticket so the remaining live tests run. A busy ticket, a DB that can't be reached or a
    migration still missing is simply tried again at the next poll."""

    def __init__(self, deps: Any, runtime: Any) -> None:
        self.deps = deps
        self.runtime = runtime

    def tickets(self) -> list[str]:
        return [t["key"] for t in self.deps.store.list_tickets()
                if t.get("waiting_for") == "migration" and not t.get("finished_at")]  # fmt: skip

    def poll(self) -> list[str]:
        from pipeline.graph.nodes.testing import MIGRATION_GATE, service

        resumed = []
        for key in self.tickets():
            info = self.runtime.snapshot(key)
            if info.waiting != MIGRATION_GATE or info.busy:
                continue
            pending = (info.values.get("last_event") or {}).get("pending") or []
            if not pending:
                continue
            try:
                applied = service(self.deps).pending_applied(key, pending)
            except Exception as err:  # VPN down, DB away: next poll
                get_logger(ticket=key).warning("migration_poll_failed", error=str(err)[:300])
                continue
            if not applied:
                continue
            payload = {"gate": MIGRATION_GATE, "action": "migration_applied",
                       "by": "the dev database", "source": "poller"}  # fmt: skip
            try:
                self.runtime.resume(key, payload)
            except RuntimeError:
                continue
            resumed.append(key)
        return resumed

