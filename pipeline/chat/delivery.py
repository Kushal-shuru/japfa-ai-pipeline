"""The minimal notification policy (Phase 9, `chat.verbosity: minimal`): one record per ticket
from which two things are rebuilt whenever it changes:

- the **Jira delivery summary**: one comment per ticket (marker `aipl:KEY:summary:0`), edited in
  place: status, which plan is current, merge requests, deployment note, decisions people made.
  Each plan version is its own Jira comment that is never edited, so earlier plans stay
  readable; Jira statuses and the QA comments (test notes, numbered bugs) stay as they are;
- the **merge-request card** in the ticket's thread: posted once when the merge requests open (per
  round: `initial`, `qa1` …), then edited with the approval and the deployment note.

The record lives in the tracking DB (`ticket_notes`, name `delivery`), so a restart neither
loses it nor posts anything twice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pipeline.chat.cards import Card

NOTE = "delivery"
LEFT_HEAD = (
    "**Left open** (not blocking, so not fixed automatically; before the merge you can still "
    "ask me to fix them, all of them or by number):"
)
MAX_DECISIONS = 30


def blank() -> dict:
    return {"status": "", "plan_md": "", "plan_rev": "", "mrs": [], "round": "", "merged": [],
            "mr_status": "", "mr_message": "", "deploy": None, "decisions": [],
            "left": []}  # fmt: skip


def stamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")


def add_decision(note: dict, text: str) -> None:
    line = f"{stamp()}: {text.strip()}"
    if not any(d.split(": ", 1)[-1] == text.strip() for d in note["decisions"][-3:]):
        note["decisions"] = [*note["decisions"], line][-MAX_DECISIONS:]


def mr_lines(note: dict) -> list[str]:
    merged = set(note.get("merged") or [])
    return [f"{n}. **{m['repo']}** ({m.get('role', '')}): {m.get('url') or '(no link)'}"
            + (" (merged)" if m["repo"] in merged else "")
            for n, m in enumerate(note.get("mrs") or [], 1)]  # fmt: skip


def summary_md(key: str, note: dict) -> str:
    """The Jira delivery summary."""
    parts = [f"**{key}: delivery summary** (the AI pipeline keeps this comment up to date)"]
    if note.get("status"):
        parts.append(f"**Status:** {note['status']}")
    if note.get("plan_rev"):  # each plan version is its own comment (never edited)
        parts.append(f"**Current plan:** {note['plan_rev']} (the newest plan comment; earlier "
                     "versions stay in their own comments)")  # fmt: skip
    if note.get("mrs"):
        head = "**Merge requests**, in the order to merge them"
        if note.get("mr_status"):
            head += f": {note['mr_status']}"
        parts.append(head + "\n\n" + "\n".join(mr_lines(note)))
    if note.get("live"):  # P10
        parts.append(f"**Live acceptance tests:** {note['live']}")
    if note.get("left"):
        parts.append(LEFT_HEAD + "\n\n" + "\n".join(f"- {x}" for x in note["left"]))
    deploy = note.get("deploy") or {}
    if deploy.get("file"):
        parts.append(f"**Deployment note:** [^{deploy['file']}]"
                     + (f" ({deploy['line']})" if deploy.get("line") else ""))  # fmt: skip
    if note.get("decisions"):
        parts.append("**Decisions**\n\n" + "\n".join(f"- {d}" for d in note["decisions"]))
    return "\n\n".join(parts)


def mr_card(key: str, note: dict) -> Card:
    """The one merge-request message: links in merge order, where review stands, and the
    deployment note once it's written."""
    facts = [("Status", note.get("mr_status") or "Reviewing")]
    if note.get("live"):  # P10
        facts.append(("Live tests", str(note["live"])))
    deploy = note.get("deploy") or {}
    facts += [tuple(f) for f in deploy.get("facts") or []]  # type: ignore[misc]
    sections = ["In merge order:\n\n" + "\n".join(mr_lines(note))]
    if note.get("left"):
        sections.append(LEFT_HEAD + "\n\n" + "\n".join(f"- {x}" for x in note["left"]))
    sections += list(deploy.get("sections") or [])
    return Card(title=f"{key}: merge requests", facts=facts, sections=sections)  # type: ignore[arg-type]


def load(store: Any, memory: dict, key: str) -> dict:
    saved = store.get_note(key, NOTE) if store is not None else memory.get(key)
    return {**blank(), **(saved or {})}


def save(store: Any, memory: dict, key: str, note: dict) -> None:
    if store is not None:
        store.set_note(key, NOTE, note)
    else:
        memory[key] = note
