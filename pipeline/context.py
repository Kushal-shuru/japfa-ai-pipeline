"""Ticket context: Jira issue + repos + spec + acceptance criteria → `TicketContext`."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable

from pipeline.config import PipelineConfig
from pipeline.integrations.base import IntegrationError, JiraPort, SpecPort
from pipeline.integrations.jira_client import extract_urls
from pipeline.schemas import TicketContext

_LABEL_LINE = r"^\s*(?:[-*]\s*)?(?:\*\*|__)?\s*{label}\s*(?:\*\*|__)?\s*:\s*(?:\*\*|__)?\s*(.+)$"
REPOS_LINE_RE = re.compile(_LABEL_LINE.format(label="repos?"), re.I | re.M)
SPEC_LINE_RE = re.compile(_LABEL_LINE.format(label=r"(?:tech(?:nical)?\s+)?spec"), re.I | re.M)


def repos_from_description(text: str) -> list[str]:
    """`Repos: a, b` line → ["a", "b"] (first matching line only)."""
    m = REPOS_LINE_RE.search(text or "")
    if not m:
        return []
    raw = m.group(1).replace("\\", "").replace("`", "")
    return [r.strip(" *_") for r in re.split(r"[,;]", raw) if r.strip(" *_")]


def spec_urls_from_description(text: str) -> list[str]:
    urls: list[str] = []
    for m in SPEC_LINE_RE.finditer(text or ""):
        for u in extract_urls(m.group(1)):
            if u not in urls:
                urls.append(u)
    return urls


def build_context(
    key: str,
    jira: JiraPort,
    spec: SpecPort,
    config: PipelineConfig,
    problems: list[str] | None = None,
    intake: dict | None = None,
    extract: Callable[[str], list[str] | None] | None = None,
) -> TicketContext:
    """Collect everything the planner needs. Never guesses: anything missing stays empty,
    unknown repo names are kept (so `context_ok` can report them), and fetch failures are
    appended to `problems` if a list is given. `intake` (D202): what the person who started the
    ticket gave (`spec_url`, `repos`); it comes before anything Jira has. `extract` reads the
    acceptance criteria out of the description (`pipeline.criteria.extractor`, an agent run): it
    returns the list, or None when the run failed (appended to `problems`)."""
    problems = problems if problems is not None else []
    intake = intake or {}
    issue = jira.get_issue(key)
    description = issue.get("description_md") or issue.get("description") or ""

    # repos: what the person chose (D202) → the Repos field → a Repos: line
    repos = (list(intake.get("repos") or []) or list(issue.get("repos") or [])
             or repos_from_description(description))  # fmt: skip
    known = [r for r in repos if r in config.repo_names]
    unknown = [r for r in repos if r not in config.repo_names]
    repos = config.repo_order(known) + unknown

    # spec URLs: the link the person pasted (D202) → the Tech Spec field → Spec: lines → epic
    spec_urls = [intake["spec_url"]] if intake.get("spec_url") else []
    spec_urls = (spec_urls or list(issue.get("spec_urls") or [])
                 or spec_urls_from_description(description))  # fmt: skip
    parent_key = issue.get("parent_key")
    if not spec_urls and parent_key:
        try:
            parent = jira.get_issue(parent_key)
            spec_urls = list(parent.get("spec_urls") or []) or spec_urls_from_description(
                parent.get("description_md") or parent.get("description") or ""
            )
        except IntegrationError as err:
            problems.append(f"Could not read parent {parent_key}: {err}")

    sections: list[str] = []
    fetched: set[str] = set()
    for url in spec_urls:
        doc = url.split("#", 1)[0]  # whole documents (D66): one copy per document
        if doc in fetched:
            continue
        fetched.add(doc)
        try:
            md, _ = spec.fetch(url)
        except IntegrationError as err:
            problems.append(f"Could not fetch spec {url}: {err}")
            continue
        sections.append(f"<!-- source: {url} -->\n{md.strip()}")
    spec_md = "\n\n---\n\n".join(sections)

    criteria = criteria_of(description, extract, problems)
    return TicketContext(
        key=issue.get("key", key),
        summary=issue.get("summary") or "",
        description_md=description,
        acceptance_criteria=criteria,
        repos=repos,
        spec_urls=spec_urls,
        spec_md=spec_md,
        spec_hash=hashlib.sha256(spec_md.encode()).hexdigest() if spec_md else "",
        epic_key=issue.get("epic_key") or parent_key,
    )


def criteria_of(description: str, extract: Callable[[str], list[str] | None] | None,
                problems: list[str]) -> list[str]:
    """The acceptance criteria of `description` through `extract` (an agent run). None from it =
    the run failed: said in `problems`; no `extract` = none (a caller that doesn't need them)."""
    if extract is None:
        return []
    found = extract(description)
    if found is None:
        why = getattr(extract, "error", "") or "the agent run failed"
        problems.append(f"I couldn't read the acceptance criteria from the description ({why}). "
                        "Tell me to try again.")  # fmt: skip
        return []
    return found


def with_description(ctx: TicketContext, description: str,
                     extract: Callable[[str], list[str] | None] | None = None) -> TicketContext:
    """The context with the ticket's description (and so its criteria) as Jira has it now; the
    criteria stay as they were when they can't be read again."""
    found = extract(description) if extract is not None else None
    return ctx.model_copy(update={
        "description_md": description,
        "acceptance_criteria": found if found is not None else ctx.acceptance_criteria,
    })


def context_ok(ctx: TicketContext, config: PipelineConfig) -> tuple[bool, list[str]]:
    """Human-readable problems that stop the pipeline from starting."""
    valid = ", ".join(config.repo_names)
    problems: list[str] = []
    unknown = [r for r in ctx.repos if r not in config.repo_names]
    known = [r for r in ctx.repos if r in config.repo_names]
    if unknown:
        problems.append(f"Unknown repo(s): {', '.join(unknown)}. Valid repo names: {valid}.")
    if not known and not unknown:
        problems.append(
            "No repositories chosen. Start the ticket again and pick the ones in scope (in the "
            "terminal `implement KEY` asks; elsewhere add `--repos a,b`). "
            f"Valid repo names: {valid}."
        )
    if not ctx.spec_urls:
        problems.append(
            "No tech spec. Start the ticket again and give it (in the terminal `implement KEY` "
            + ("asks which document of the tech_spec folder; elsewhere add `--spec <number or "
               "file name>`)." if config.spec.source == "local" else
               "asks for the Google Doc link; elsewhere add `--spec <link>`).")
        )
    elif not ctx.spec_md.strip():
        problems.append("The linked tech spec could not be read (or is empty).")
    if not ctx.acceptance_criteria:
        problems.append(
            "I found no acceptance criteria in the description. Add them (any format: a list, "
            "or one heading per criterion) and tell me to try again."
        )
    return (not problems, problems)
