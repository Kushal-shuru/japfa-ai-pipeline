"""Agent roles: provider, model, limits, permissions, prompt and result file (CLAUDE.md §3.7)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pipeline.config import AGENT_ROLES, PipelineConfig

PROMPTS_DIR = Path(__file__).resolve().parents[2] / "prompts"
GUIDANCE_FILE = "guidance.md"  # human guidance (`guide`, P4b) for the planner / coder


def guidance_md(guidance: list[dict] | None) -> str | None:
    """`TicketState.guidance` as `.agent/guidance.md` (None when there is none). Newest last;
    each entry names the step it was given for."""
    if not guidance:
        return None
    lines = [
        "# Human guidance",
        "",
        "People gave this guidance after a step failed or asked a question. Follow it; it",
        "overrides earlier assumptions (not the interface contract, unless it says so).",
        "",
    ]
    for n, g in enumerate(guidance, 1):
        where = f"{g.get('step') or '?'}" + (f", {g['repo']}" if g.get("repo") else "")
        lines.append(f"{n}. ({where}; {g.get('by') or 'someone'}, {g.get('at') or ''}) "
                     f"{str(g.get('text') or '').strip()}")  # fmt: skip
    return "\n".join(lines) + "\n"


READ_ONLY_ROLES = frozenset(
    {"planner", "reviewer", "reviewer_ticket", "contract_check", "deployment", "qa_notes",
     "tester", "summary", "criteria"}
)
WRITABLE_ROLES = frozenset({"coder", "coder_fix", "coder_rebase", "coder_ticket",
                            "coder_ticket_fix"})

PROMPT_FILES: dict[str, str | tuple[str, ...]] = {
    "planner": "planner.md",
    "coder": "coder_backend.md",  # P7: coder_frontend.md for frontend repos
    "coder_fix": "coder_fix.md",
    "coder_rebase": "coder_rebase.md",
    "coder_ticket": "coder_ticket.md",
    "coder_ticket_fix": "coder_ticket_fix.md",
    # the preface, then the per-repo review rules and the contract check rules, one source each
    "reviewer_ticket": ("reviewer_ticket.md", "reviewer.md", "contract_check.md"),
    "reviewer": "reviewer.md",
    "contract_check": "contract_check.md",
    "deployment": "deployment.md",
    "qa_notes": "qa_notes_backend.md",
    "tester": "tester.md",  # P10: the live acceptance cases
    "summary": "chat_summary.md",  # D209: short plain texts
    "criteria": "acceptance_criteria.md",  # D218: the criteria, copied word for word
}
RESULT_FILES: dict[str, str] = {
    "planner": "plan.json",
    "coder": "result.json",
    "coder_fix": "result.json",
    "coder_rebase": "result.json",
    "coder_ticket": "result.json",
    "coder_ticket_fix": "result.json",
    "reviewer_ticket": "review.json",
    "reviewer": "review.json",
    "contract_check": "contract_review.json",
    "deployment": "deploy.json",
    "qa_notes": "qa_notes.json",
    "tester": "acceptance_plan.json",
    "summary": "summary.json",
    "criteria": "criteria.json",
}
ROLE_POOL: dict[str, str | None] = {
    "planner": "coder",
    "coder": "coder",
    "coder_fix": "coder",
    "coder_rebase": "coder",
    "coder_ticket": "coder",
    "coder_ticket_fix": "coder",
    "reviewer_ticket": "reviewer",
    "reviewer": "reviewer",
    "contract_check": "reviewer",
    "deployment": "deploy",
    "qa_notes": "deploy",
    "tester": "reviewer",  # P10: independent of the coder that wrote the code under test
    "summary": None,  # a tiny run outside the pool
    "criteria": None,  # a small run outside the pool
}

# Claude tool sets. Edit rules also cover Write (Claude Code permissions docs).
READ_TOOLS = ("Read", "Glob", "Grep")
# `cd` is for a ticket-level coder (single_session), which runs each repo's checks from its folder
CODER_BASH = ("Bash(git *)", "Bash(npm *)", "Bash(npx *)", "Bash(docker compose *)", "Bash(cd *)")
# git stash / reset --hard hide or drop the coder's own uncommitted work (DEMO-4 stashed it all)
CODER_DENY = ("Bash(git push *)", "Bash(git push)", "Bash(git remote *)", "Bash(git stash *)",
              "Bash(git stash)", "Bash(git reset --hard *)", "Bash(git reset --hard)",
              # migrations and seeds run only through DevOps (deployment note); writing a
              # migration file (`migration:generate`) stays allowed
              "Bash(npx sequelize-cli db:*)", "Bash(npx sequelize db:*)",
              "Bash(npm run migrate*)", "Bash(npm run seed*)")  # fmt: skip


def protected_rules(root: str, paths: tuple[str, ...]) -> list[str]:
    """Deny rules (they beat the allow rule for the worktree) for a repo's protected files: a
    file or glob as given, a directory ("src/x/") with everything under it. Edit rules cover
    Write too. Claude only: Codex coders get the prompt rule and the pipeline's own undo."""
    rules = []
    for p in paths:
        p = p.lstrip("./")
        rules.append(f"Edit({root}/{p}**)" if p.endswith("/") else f"Edit({root}/{p})")
    return rules


@dataclass(frozen=True)
class RoleSpec:
    name: str
    provider: str
    model: str | None
    writable: bool
    max_turns: int
    timeout_min: float
    prompt_file: str | tuple[str, ...] | None
    result_file: str
    auth_mode: str = "subscription"
    effort: str | None = None  # Claude reasoning effort (D70)
    protected: tuple[str, ...] = ()  # the repo's shared_paths: a writable role may not edit them

    @property
    def timeout_s(self) -> float:
        return self.timeout_min * 60

    def prompt_text(self, variables: dict[str, str] | None = None) -> str:
        """The role's prompt file ('' if it doesn't exist yet), templated with `variables`."""
        if not self.prompt_file:
            return ""
        files = (self.prompt_file,) if isinstance(self.prompt_file, str) else self.prompt_file
        parts = [(PROMPTS_DIR / f).read_text() for f in files if (PROMPTS_DIR / f).exists()]
        text = "\n\n---\n\n".join(parts)
        return render_prompt(text, variables) if variables else text

    def claude_permissions(self, cwd: Path) -> dict[str, list[str]]:
        """`tools` (what exists), `allowed_tools` (auto-approved), `disallowed_tools`.
        Runs use permission_mode=dontAsk: anything not allowed is denied."""
        root = "/" + str(cwd.resolve())  # `//abs/path` = absolute in permission rules
        if self.writable:
            return {
                "tools": [*READ_TOOLS, "Write", "Edit", "Bash"],
                "allowed_tools": [*READ_TOOLS, f"Edit({root}/**)", *CODER_BASH],
                "disallowed_tools": [*CODER_DENY, *protected_rules(root, self.protected)],
            }
        return {
            "tools": [*READ_TOOLS, "Write", "Edit"],
            "allowed_tools": [*READ_TOOLS, f"Edit({root}/.agent/**)"],
            "disallowed_tools": ["Bash"],
        }

    @property
    def codex_sandbox(self) -> str:
        return "workspace-write" if self.writable else "read-only"


PROMPT_VARS = ("KEY", "REPO", "ROLE", "COMMANDS", "TICKET_DIR", "ROUND")
_ROLE_BLOCK_RE = re.compile(r"<!-- role:([\w|]+) -->\n(.*?)<!-- /role -->\n?", re.S)


def render_prompt(text: str, variables: dict[str, str]) -> str:
    """Replace `{KEY}`-style placeholders (only the known names, so JSON braces survive) and keep
    only the `<!-- role:x -->…<!-- /role -->` blocks matching `variables["ROLE"]`."""
    role = variables.get("ROLE")
    if role is not None:
        text = _ROLE_BLOCK_RE.sub(
            lambda m: m.group(2) if role in m.group(1).split("|") else "", text
        )
    for name in PROMPT_VARS:
        if name in variables:
            text = text.replace("{" + name + "}", str(variables[name]))
    return text


def slot_provider(config: PipelineConfig, slot: str | None) -> str | None:
    if not slot:
        return None
    for slots in config.agent_pool.values():
        for s in slots:
            if s.name == slot:
                return s.provider
    raise KeyError(f"unknown agent slot {slot!r}")


def resolve_provider(
    config: PipelineConfig, role: str, override: str | None = None, slot: str | None = None
) -> str:
    """Command `--provider` → the slot's provider → role config → default (§3.7)."""
    if role not in AGENT_ROLES:
        raise KeyError(f"unknown agent role {role!r}")
    return config.resolve_provider(role, override or slot_provider(config, slot))


def role_spec(
    config: PipelineConfig,
    role: str,
    provider_override: str | None = None,
    slot: str | None = None,
) -> RoleSpec:
    provider = resolve_provider(config, role, provider_override, slot)
    rc = config.agents.role_config(role)
    auth = config.agents.providers.get(provider)  # type: ignore[call-overload]
    return RoleSpec(
        name=role,
        provider=provider,
        # The configured model belongs to the role's configured provider (not to an override).
        model=rc.model if provider == config.agents.provider_for(role) else None,
        effort=rc.effort if provider == config.agents.provider_for(role) else None,
        writable=role in WRITABLE_ROLES,
        max_turns=rc.max_turns,
        timeout_min=rc.timeout_min,
        prompt_file=PROMPT_FILES.get(role),
        result_file=RESULT_FILES[role],
        auth_mode=auth.auth if auth else "subscription",
    )
