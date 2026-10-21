"""QA bugs (Phase 7, D126; D206): QA reports each bug as its own Jira ticket.

After RFT, QA files the bugs as new tickets (the bug is in the description). The person starts
a QA round with `fix <main ticket> DEMO-13 DEMO-14` (the terminal asks for the bug tickets);
the pipeline reads each ticket's summary and description and gives it a number for the life of
the main ticket (#1, #2, …). The numbered list goes on the main ticket's Jira; nothing is read
from comments any more. `bug KEY: …` typed in the terminal still adds a bug by hand.

Bookkeeping lives in `TicketState.bugs` (a `list[dict]`, §7), one entry per:

- `{"kind": "rft", "round": n}`: a testing round starts;
- `{"kind": "bug", "n", "text", "label", "ticket", "by", "at", "source", "status", "round"}`:
  `text` = the whole description, `label` = "DEMO-13: summary", `ticket` = the bug ticket (None
  when typed); `status` open → fixing (chosen for QA round `round`) → fixed (that round reached
  RFT again).
"""

from __future__ import annotations

import re

QUIET_MINUTES = 5  # gates.qa_fix auto: start this long after the last new bug
_ITEM = re.compile(r"^\s*(?:#\s*)?(?:\d+\s*[.):-]|[-*•]|#)\s+(.+?)\s*$")


# ---------- reading the list ----------


def items(bugs: list[dict] | None, kind: str = "bug") -> list[dict]:
    return [b for b in bugs or [] if b.get("kind") == kind]


def open_bugs(bugs: list[dict] | None) -> list[dict]:
    return [b for b in items(bugs) if b.get("status") == "open"]


def in_round(bugs: list[dict] | None, round_: int) -> list[dict]:
    return [b for b in items(bugs) if b.get("round") == round_]


def numbers_words(ns: list[int]) -> str:
    from pipeline.chat.summaries import join_words

    return join_words([f"#{n}" for n in ns]) or "none"


def list_md(bugs: list[dict]) -> str:
    """ "- #4 text (Priya)": explicit numbers, never a Markdown ordered list (Jira renumbers
    those from 1, and `fix 4` must match what people read)."""
    return "\n".join(f"- #{b['n']} {b.get('label') or b['text']}"
                     + (f" ({b['by']})" if b.get("by") else "") for b in bugs)  # fmt: skip


# ---------- splitting a typed list ----------

_INLINE_NUMBER = re.compile(r"(?:^|(?<=\s))\d+[.)]\s+")


def split_items(text: str) -> list[str]:
    """The items of a list, one per line ("1. …", "- …") or on one line ("1) … 2) …"); []
    when the text isn't a list."""
    lines = [m.group(1) for line in text.splitlines() if (m := _ITEM.match(line))]
    if len(lines) >= 2:
        return lines
    marks = list(_INLINE_NUMBER.finditer(text))
    if len(marks) >= 2:
        ends = [m.start() for m in marks[1:]] + [len(text)]
        parts = [text[m.end() : e].strip(" ;,\n") for m, e in zip(marks, ends, strict=True)]
        return [" ".join(p.split()) for p in parts if p.strip()]
    return lines


def add_tickets(bugs: list[dict], issues: list[dict], at: str,
                by: str) -> tuple[list[dict], list[dict]]:  # fmt: skip
    """One numbered bug per bug ticket (`issues`: Jira issues, in the order given). A ticket that
    is still open or being fixed keeps its number; one already fixed and reported again gets a
    new one. Returns (the whole list, the bugs of these tickets in order)."""
    out = list(bugs)
    n = max((b.get("n") or 0 for b in items(out)), default=0)
    chosen = []
    for issue in issues:
        key = issue["key"]
        same = [b for b in items(out) if b.get("ticket") == key
                and b.get("status") in ("open", "fixing")]  # fmt: skip
        if same:
            chosen.append(same[-1])
            continue
        n += 1
        summary = " ".join(str(issue.get("summary") or "").split())
        body = str(issue.get("description_md") or issue.get("description") or "").strip()
        bug = {"kind": "bug", "n": n, "text": f"{summary}\n\n{body}".strip(),
               "label": f"{key}: {summary}", "ticket": key, "by": by, "at": at,
               "source": "ticket", "status": "open", "round": None}  # fmt: skip
        out.append(bug)
        chosen.append(bug)
    return out, chosen


def add_typed(bugs: list[dict], text: str, by: str, at: str,
              source: str) -> tuple[list[dict], list[dict]]:  # fmt: skip
    """`bug KEY: …` typed in chat: one bug per numbered / bulleted item, else one bug."""
    parts = split_items(text)
    out = list(bugs)
    n = max((b.get("n") or 0 for b in items(out)), default=0)
    added = []
    for t in parts or [text.strip()]:
        n += 1
        text_ = " ".join(t.split())
        bug = {"kind": "bug", "n": n, "text": text_, "by": by, "at": at, "source": source,
               "ticket": None, "status": "open", "round": None}  # fmt: skip
        out.append(bug)
        added.append(bug)
    return out, added


# ---------- choosing ----------


def choose(bugs: list[dict], scope: str | None,
           numbers: list[int]) -> tuple[list[int], list[int]]:  # fmt: skip
    """(chosen bug numbers, unknown numbers). No numbers = every open bug."""
    open_ns = [b["n"] for b in open_bugs(bugs)]
    if not numbers:
        return open_ns, []
    wanted = list(dict.fromkeys(numbers))
    return [n for n in wanted if n in open_ns], [n for n in wanted if n not in open_ns]


def mark(bugs: list[dict], ns: list[int], status: str, round_: int | None = None) -> list[dict]:
    out = []
    for b in bugs:
        if b.get("kind") == "bug" and b.get("n") in ns:
            b = {**b, "status": status, **({"round": round_} if round_ is not None else {})}
        out.append(b)
    return out
