"""The ticket's acceptance criteria, read by an agent (the `criteria` role).

Tickets write them in many shapes, so no parser: a small read-only agent run copies them out of
the description (`prompts/acceptance_criteria.md`: copy, never reword). The pipeline does not
trust the copy: every criterion must be a quote from the description (letters and digits
compared, formatting and spacing ignored), else the run goes back to the agent once and then
fails. The answer is cached per ticket by a hash of the description (ticket note `criteria`), so
a retry, a re-plan or a restart does not run the agent again; an edited description does.

`extractor(deps, key)` is what `build_context` takes: description → criteria, or None when the
agent run failed (the reason is in `Extractor.error`).
"""

from __future__ import annotations

import hashlib
import re
import tempfile
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from pipeline.logging import get_logger, mask_text

NOTE = "criteria"  # ticket note: {"hash", "criteria"}
MIN_LETTERS = 6  # a shorter "criterion" is not one


class CriteriaOut(BaseModel):
    """The `criteria` role's result."""

    criteria: list[str] = []


def _letters(text: str) -> str:
    """Only letters and digits, lower case: formatting, punctuation and spacing don't count."""
    return re.sub(r"[\W_]+", "", (text or "").casefold())


def problems_with(criteria: list[str], description: str) -> list[str]:
    """What is wrong with the agent's copy: entries that are not quotes from the description."""
    source = _letters(description)
    out = []
    for n, item in enumerate(criteria, 1):
        text = _letters(item)
        if len(text) < MIN_LETTERS or text not in source:
            out.append(f"criterion {n} is not copied from the ticket description, word for "
                       f"word: {item.strip()[:120]!r}")  # fmt: skip
    return out


def clean(criteria: list[str]) -> list[str]:
    """Whitespace folded, empties and repeats dropped, order kept."""
    seen: set[str] = set()
    out = []
    for item in criteria:
        text = " ".join(item.split())
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _hash(description: str) -> str:
    return hashlib.sha256(description.encode()).hexdigest()[:20]


class Extractor:
    """description → the criteria (a list, [] = the ticket has none) or None when the run failed;
    `error` then says why, in words."""

    def __init__(self, deps: Any, key: str) -> None:
        self.deps = deps
        self.key = key
        self.error = ""

    def __call__(self, description: str) -> list[str] | None:
        self.error = ""
        if not description.strip():
            return []
        store = self.deps.store
        cached = store.get_note(self.key, NOTE) if store is not None else None
        if isinstance(cached, dict) and cached.get("hash") == _hash(description):
            return list(cached.get("criteria") or [])
        found = self._ask(description)
        if found is not None and store is not None:
            store.set_note(self.key, NOTE, {"hash": _hash(description), "criteria": found})
        return found

    def _ask(self, description: str) -> list[str] | None:
        deps, key = self.deps, self.key
        runs = Path(deps.home) / "runs" / key
        runs.mkdir(parents=True, exist_ok=True)
        prompt = ("<ticket_description>\n" + mask_text(description).strip()
                  + "\n</ticket_description>")  # fmt: skip
        with tempfile.TemporaryDirectory(prefix="criteria-", dir=runs) as tmp:
            result, run = deps.runner.run(
                key, "criteria", Path(tmp).resolve(), prompt, CriteriaOut,
                check=lambda r: problems_with(clean(r.criteria), description),
                check_error="criteria_not_quoted",
            )  # fmt: skip
        if result is None:
            from pipeline.chat.summaries import error_words

            self.error = error_words(getattr(run, "error", None))
            get_logger(ticket=key).warning("criteria_failed", error=getattr(run, "error", None))
            return None
        return clean(result.criteria)


def extractor(deps: Any, key: str) -> Extractor:
    return Extractor(deps, key)
