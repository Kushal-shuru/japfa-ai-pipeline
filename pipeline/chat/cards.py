"""Chat cards. ConsoleChat renders them as text with the commands to type.
`ChatPort.post(card=...)` takes `to_dict()`."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Action:
    label: str
    command: str  # exact command text, e.g. "approve plan DEMO-1"
    input_hint: str | None = None  # the command needs text appended, e.g. "<feedback>"
    style: str = "default"  # default | positive | destructive

    def example(self) -> str:
        """The command a console user types (with the input placeholder)."""
        return f"{self.command} {self.input_hint}" if self.input_hint else self.command


@dataclass
class Card:
    title: str
    sections: list[str] = field(default_factory=list)  # markdown blocks
    facts: list[tuple[str, str]] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    author: str = ""  # the agent posting it ("Reviewer"), shown above the title (D111)

    def to_dict(self) -> dict:
        return {
            "type": "pipeline_card",
            **({"author": self.author} if self.author else {}),
            "title": self.title,
            "sections": list(self.sections),
            "facts": [list(f) for f in self.facts],
            "actions": [
                {"label": a.label, "command": a.command, "input_hint": a.input_hint,
                 "style": a.style}
                for a in self.actions
            ],
        }  # fmt: skip

    @classmethod
    def from_dict(cls, data: dict) -> Card:
        return cls(
            author=data.get("author", ""),
            title=data.get("title", ""),
            sections=list(data.get("sections", [])),
            facts=[(str(k), str(v)) for k, v in data.get("facts", [])],
            actions=[
                Action(a["label"], a["command"], a.get("input_hint"), a.get("style", "default"))
                for a in data.get("actions", [])
            ],
        )


def render_text(card: Card, command_prefix: str = 'pipeline say "') -> str:
    """Console rendering: title, sections, facts, then ``1) Label → `pipeline say "…"` ``
    (a code span, so `<feedback>` survives markdown rendering)."""
    lines = [f"**{card.title}**"] if card.title else []
    if card.author:
        lines = [f"**{card.author}:**", *lines]
    for s in card.sections:
        lines += ["", s.rstrip()]
    if card.facts:
        lines.append("")
        lines += [f"- {k}: {v}" for k, v in card.facts]
    if card.actions:
        lines.append("")
        suffix = '"' if command_prefix.endswith('"') else ""
        for n, a in enumerate(card.actions, 1):
            lines.append(f"{n}) {a.label} → `{command_prefix}{a.example()}{suffix}`")
    return "\n".join(lines)
