"""Plain-language wording (Phase 4b §3): personas, Chat LLM summaries with template fallback,
two-part Jira comments, and the data guardrail for what a summary may see.

Nothing here decides anything: it only turns structured pipeline events into words.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pipeline.logging import get_logger

PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "chat_summary.md"

# The team inside the one bot (D111): every ticket message is spoken by an agent, named in
# `chat.agents`. Roles map to agents; a role whose agent isn't configured speaks as the Coder.
DEFAULT_AGENT_NAMES: dict[str, str] = {"coder": "Coder", "reviewer": "Reviewer", "devops": "DevOps"}
PERSONA_AGENT: dict[str, str] = {
    "planner": "coder",  # planning is part of the Coder's work
    "coder": "coder",
    "bot": "coder",  # "I've started on …", progress, stops
    "reviewer": "reviewer",
    "deployment": "devops",  # the deployment note is DevOps work (D113)
    "qa_notes": "coder",  # P7: the Coder knows the endpoints it built
    "devops": "devops",
}


def agent_of(persona: str, names: dict[str, str] | None = None) -> str:
    agent = PERSONA_AGENT.get(persona, persona)
    return agent if agent in (names or DEFAULT_AGENT_NAMES) else "coder"


def agent_name(persona: str, names: dict[str, str] | None = None) -> str:
    """ "Reviewer" for `reviewer`; the configured name of the agent that speaks for a role."""
    names = names or DEFAULT_AGENT_NAMES
    return names.get(agent_of(persona, names)) or names.get("coder") or "Coder"


# graph step → words people read (never show node names in chat, D86)
STEP_WORDS: dict[str, str] = {
    "load_context": "reading the ticket", "plan": "planning", "plan_gate": "plan approval",
    "plan_gate_wait": "plan approval", "replan": "updating the plan", "code_next_repo": "coding",
    "verify": "the automated checks", "open_mrs": "opening the merge requests",
    "review_gate": "waiting for the review", "review_gate_wait": "waiting for the review",
    "review": "the code review", "fix_gate": "choosing what to fix",
    "fix_gate_wait": "choosing what to fix", "fix": "fixing the review findings",
    "push_fixes": "pushing the fixes", "re_review_gate": "waiting for the second review",
    "re_review_gate_wait": "waiting for the second review",
    "approve": "approving the merge requests", "change": "the change request",
    "deploy_doc_gate": "waiting for the deployment note",
    "deploy_doc_gate_wait": "waiting for the deployment note",
    "deploy_doc": "writing the deployment note", "wait_merge": "waiting for the merges",
    "post_qa_notes": "handing over to QA", "wait_qa": "testing",
    "qa_fix": "starting a QA round", "qa_done": "closing the ticket",
    "env_check": "checking the live test environment", "acceptance": "the live acceptance tests",
    "migration_wait": "waiting for a migration on the dev database",
}  # fmt: skip


def step_words(step: str | None) -> str:
    return STEP_WORDS.get(step or "", (step or "an unknown step").replace("_", " "))


# agent run errors (runner codes) → words; the code stays in the logs and agent_runs
ERROR_WORDS: dict[str, str] = {
    "timeout": "it took too long and was stopped",
    "invalid_result": "it returned a result I couldn't read",
    "read_only_violation": "it tried to change files it isn't allowed to change",
    "provider_limit": "the AI service's usage limit was reached",
    "provider_unavailable": "the AI service isn't available",
    "provider_error": "the AI service reported an error",
    "paused": "the pipeline is paused",
    "max_turns": "it needed more steps than it is allowed",
    "invalid_plan": "the plan it wrote was incomplete",
    "internal": "something went wrong inside the pipeline",
}


def error_words(error: str | None) -> str:
    """ "provider_error: boom" → "the AI service reported an error (boom)"."""
    if not error:
        return "an unknown error"
    code, _, rest = error.partition(":")
    words = ERROR_WORDS.get(code.strip()) or next(
        (w for c, w in ERROR_WORDS.items() if code.startswith(c)), None
    )
    if words is None:
        return error
    return f"{words} ({rest.strip()})" if rest.strip() else words


# the failed graph step → who explains it
STEP_PERSONA: dict[str, str] = {
    "plan": "planner", "plan_gate": "planner", "plan_gate_wait": "planner", "replan": "planner",
    "code_next_repo": "coder", "verify": "coder", "open_mrs": "coder",
    "review_gate": "reviewer", "review_gate_wait": "reviewer", "review": "reviewer",
    "fix_gate": "reviewer", "fix_gate_wait": "reviewer", "re_review_gate": "reviewer",
    "re_review_gate_wait": "reviewer", "approve": "reviewer",
    "fix": "coder", "push_fixes": "coder", "change": "planner",
    "env_check": "coder", "acceptance": "reviewer", "migration_wait": "devops",
}  # fmt: skip

JIRA_MAX = 32_767  # Jira Cloud's comment limit
JIRA_BUDGET = 30_000  # markdown before wiki conversion + marker: stay well under the limit
DETAILS_FOR_LLM = 2_500  # characters of details a summary may see
MAX_SUMMARY_CHARS = 600
ERROR_LINE = re.compile(
    r"error|exception|fail|refused|timed? ?out|cannot|can't|not found|missing|denied|"
    r"unauthori[sz]ed|forbidden|ECONN|ENOENT|EADDRINUSE|\b[45]\d\d\b|assert",
    re.I,
)
FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
HYPHENS = str.maketrans({"\u2010": "-", "\u2011": "-", "\u2012": "-", "\u00ad": ""})
EMOJI = re.compile(
    "[\U0001f000-\U0001faff\u2600-\u27bf\u2b00-\u2bff\u2300-\u23ff\ufe0f\u200d\u20e3]"
)


def plain_text(text: str) -> str:
    """LLM wording → our house style (D98): no emojis; plain hyphens so keys and commands
    stay copyable (models like DEMO‑1 with U+2011)."""
    text = EMOJI.sub("", text.translate(HYPHENS))
    text = re.sub(r"[ \t]{2,}", " ", text)
    return re.sub(r"[ \t]+([.,!?;:])", r"\1", text).strip()


def persona_label(persona: str, names: dict[str, str] | None = None) -> str:
    """ "**Coder:**" (the agent that speaks for the role)."""
    return f"**{agent_name(persona, names)}:**"


def with_persona(persona: str, text: str, names: dict[str, str] | None = None) -> str:
    """ "**Reviewer:** <text>"."""
    return f"{persona_label(persona, names)} {text}" if text else text


def plural(n: int, word: str, many: str | None = None) -> str:
    """ "1 repository" / "2 repositories"."""
    return f"{n} {word if n == 1 else (many or word + 's')}"


def join_words(items: list[str]) -> str:
    """ "a", "a and b", "a, b and c"."""
    items = [str(i) for i in items]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def error_lines(text: str, limit: int = 10) -> list[str]:
    """Error-looking lines (from logs / check output), newest last, each ≤ 200 chars."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and ERROR_LINE.search(ln)]
    return [ln[:200] for ln in lines[-limit:]]


def sanitize_details(details_md: str, send_code: bool) -> str:
    """What a summary may see of `details_md` (1.6): code blocks (check output, snippets) are
    reduced to their error-looking lines unless `chat_llm.send_code` is on; capped length.
    `ChatLLMChain` masks secrets on top of this."""
    if not send_code:

        def keep_errors(m: re.Match[str]) -> str:
            kept = error_lines(m.group(1))
            return "```\n" + "\n".join(kept) + "\n```" if kept else "(output omitted)"

        details_md = FENCE.sub(keep_errors, details_md)
    if len(details_md) > DETAILS_FOR_LLM:
        details_md = details_md[:DETAILS_FOR_LLM] + "\n…"
    return details_md


def facts_md(facts: dict[str, Any]) -> str:
    out = []
    for k, v in facts.items():
        if v in (None, "", [], {}):
            continue
        shown = ", ".join(map(str, v)) if isinstance(v, (list, tuple)) else str(v)
        out.append(f"- {k}: {shown}")
    return "\n".join(out)


class Summarizer:
    """The Chat LLM writes 1–3 plain sentences; `fallback` (a template) when it can't."""

    def __init__(self, llm: Any = None, cfg: Any = None,
                 names: dict[str, str] | None = None) -> None:  # fmt: skip
        self.llm = llm
        self.cfg = cfg
        self.names = names or DEFAULT_AGENT_NAMES
        self.log = get_logger(component="summaries")

    @property
    def enabled(self) -> bool:
        return self.llm is not None and bool(getattr(self.cfg, "summaries", True))

    def summary(self, key: str, persona: str, event: str, facts: dict[str, Any],
                details_md: str = "", fallback: str = "") -> str:  # fmt: skip
        if not self.enabled:
            return fallback
        send_code = bool(getattr(self.cfg, "send_code", False))
        speaker = agent_name(persona, self.names)
        parts = [f"## Speaker\n\n{speaker}", f"## Event\n\n{event}",
                 f"## Facts\n\n{facts_md({'ticket': key, **facts})}"]  # fmt: skip
        if details_md.strip():
            parts.append(f"## Details shown below your text\n\n"
                         f"{sanitize_details(details_md, send_code)}")  # fmt: skip
        try:
            reply = self.llm.complete(PROMPT_PATH.read_text(encoding="utf-8"),
                                      [{"role": "user", "content": "\n\n".join(parts)}],
                                      purpose="summary", key=key, max_tokens=300)  # fmt: skip
        except Exception:  # wording must never break a pipeline step
            self.log.exception("summary_failed", ticket=key, summary_event=event)
            return fallback
        text = (reply.text if reply is not None else "").strip()
        text = plain_text(text)
        # the label already names the speaker: drop "Coder:" / "Planner —" openers
        names = {speaker, "SDLC Bot", "Planner", "Coder", "Reviewer", "Deployment",
                 *self.names.values()}  # fmt: skip
        openers = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
        text = re.sub(rf"^\W*(?:{openers})\s*[:—–-]\s*", "", text).strip()
        if not text or len(text) > MAX_SUMMARY_CHARS or text.startswith(("{", "#")):
            return fallback
        return text


MORE_DETAILS = "the merge request descriptions and the ticket workspace"


def jira_two_part(summary: str, details_md: str, persona: str = "bot", more: str = MORE_DETAILS,
                  names: dict[str, str] | None = None) -> str:  # fmt: skip
    """Jira comment: **Summary** (plain, for product owners / QA) + **Details for developers**
    (the structured content). Never cut: one over Jira's comment limit is split into parts when
    it is posted."""
    label = persona_label(persona, names)
    head = f"{label + chr(10) + chr(10) if label else ''}**Summary**\n\n{summary.strip()}"
    if not details_md.strip():
        return head
    body = f"{head}\n\n**Details for developers**\n\n{details_md.strip()}"
    return body  # whole; over Jira's limit it is posted as several comments (JiraClient)
