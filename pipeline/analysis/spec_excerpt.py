"""The part of a long tech spec one ticket needs (D128): the sections that specify the ticket's
endpoints or match its title, what they refer to (tables, `§n` references, error sections with
their codes), their parent intros, and a table of contents. Deterministic; no model call.

Tables (D170): an endpoint's section often describes behaviour without naming the tables it
writes, so the definitions (`CREATE TABLE`) of the endpoint's resource are added too: the tables
whose name contains the resource (`ageing-deduction-configs` → `ar_ageing_deduction_configs`)
and their siblings with the same name prefix (`ar_ageing_deduction_rules`). DEMO-10's planner
saw neither and invented three tables the spec doesn't have."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_NUMBER = re.compile(r"^(\d+(?:\.\d+)*)\.?\s")
_ENDPOINT = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/[\w\-/:{}.]+)")
_REF = re.compile(r"(?:§|\bsection\s+|\bsee\s+)(\d+(?:\.\d+)+)", re.I)
_CODE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_SNAKE = re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b")
_WORD = re.compile(r"[a-z0-9]+")
_CREATE = re.compile(
    r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:\w+\.)?([a-z][a-z0-9]*(?:_[a-z0-9]+)+)", re.I
)
MAX_TABLE_SECTIONS = 8
_STOP = {"a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "by", "with", "backend",
         "frontend", "api", "apis", "business", "1", "2", "v1", "get", "post", "put", "patch",
         "delete", "list", "new", "add", "update"}  # fmt: skip


@dataclass
class Section:
    index: int
    level: int
    title: str  # markdown emphasis and escapes removed
    number: str  # "14.12", or "" when the heading has no number
    body: str
    parent: int | None = None
    children: list[int] = field(default_factory=list)

    @property
    def text(self) -> str:
        return f"{'#' * self.level} {self.title}\n\n{self.body.strip()}\n"


def _clean(text: str) -> str:
    return re.sub(r"\\(.)", r"\1", text).replace("**", "").replace("__", "").strip()


def parse_sections(spec_md: str) -> list[Section]:
    sections: list[Section] = []
    body: list[str] = []
    stack: list[Section] = []
    for line in spec_md.splitlines():
        m = _HEADING.match(line)
        if not m:
            body.append(line)
            continue
        if sections:
            sections[-1].body = "\n".join(body)
        body = []
        title = _clean(m.group(2))
        num = _NUMBER.match(title)
        s = Section(len(sections), len(m.group(1)), title, num.group(1) if num else "", "")
        while stack and stack[-1].level >= s.level:
            stack.pop()
        if stack:
            s.parent = stack[-1].index
            stack[-1].children.append(s.index)
        stack.append(s)
        sections.append(s)
    if sections:
        sections[-1].body = "\n".join(body)
    return sections


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(_clean(text).lower().replace("_", " ")) if w not in _STOP}


def _endpoints(text: str) -> set[tuple[str, str]]:
    return {(m.group(1), m.group(2).rstrip("/.")) for m in _ENDPOINT.finditer(_clean(text))}


def table_definitions(spec_md: str) -> dict[str, Section]:
    """Table name → the section whose own text has its `CREATE TABLE` (the first one)."""
    out: dict[str, Section] = {}
    for s in parse_sections(spec_md):
        for name in _CREATE.findall(_clean(s.body)):
            out.setdefault(name.lower(), s)
    return out


def _tokens(name: str) -> list[str]:
    return [t for t in name.lower().split("_") if t]


def _singular(tokens: list[str]) -> list[str]:
    return [*tokens[:-1], tokens[-1][:-1] if tokens[-1].endswith("s") else tokens[-1]]


def _contains(table: list[str], part: list[str]) -> bool:
    """`part` appears as consecutive tokens in `table` (plural-insensitive on the last one)."""
    table, part = _singular(table), _singular(part)
    n = len(part)
    return any(table[i : i + n] == part for i in range(len(table) - n + 1))


def resource_names(ticket_text: str) -> list[str]:
    """Snake-case resource names from the ticket's endpoint paths (`/ageing-deduction-configs`
    → `ageing_deduction_configs`); only names of two or more words."""
    out: list[str] = []
    for _, path in _endpoints(ticket_text):
        for seg in path.strip("/").split("/"):
            name = seg.replace("-", "_").lower()
            if seg.startswith((":", "{")) or len(_tokens(name)) < 2 or name in out:
                continue
            out.append(name)
    return out


def related_tables(defined: list[str], resources: list[str], limit: int = MAX_TABLE_SECTIONS
                   ) -> list[str]:  # fmt: skip
    """The tables of the ticket's resources: those whose name contains a resource name, then
    their siblings (the same name without its last word as prefix, at least two words)."""
    primary = [t for t in defined if any(_contains(_tokens(t), _tokens(r)) for r in resources)]
    out = list(primary)
    for t in primary:
        prefix = _tokens(t)[:-1]
        if len(prefix) < 2:
            continue
        out += [o for o in defined if o not in out and _tokens(o)[: len(prefix)] == prefix]
    return out[:limit]


def _anchors(sections: list[Section], summary: str, ticket_text: str) -> list[int]:
    """Sections that specify one of the ticket's endpoints exactly; else the best title match."""
    wanted = _endpoints(ticket_text)
    exact = [s.index for s in sections if wanted & _endpoints(s.body)]
    if exact:
        # a section whose own heading is the endpoint's (not a parent that lists all routes)
        leaves = [i for i in exact if not any(c in exact for c in sections[i].children)]
        return leaves or exact
    words = _words(summary)
    if not words:
        return []
    scored = []
    for s in sections:
        title = _words(s.title)
        if title:
            overlap = len(words & title)
            scored.append((overlap / len(words | title), overlap, s.index))
    best = max(scored, default=(0.0, 0, -1))
    return [best[2]] if best[1] >= 2 else []


def excerpt(spec_md: str, summary: str, ticket_text: str, budget: int = 50_000) -> str:
    """Markdown: a note, the chosen sections (whole bodies, in spec order, within `budget`
    characters), then the table of contents. '' when the spec is empty."""
    if not spec_md.strip():
        return ""
    sections = parse_sections(spec_md)
    if not sections:
        return spec_md[:budget]
    anchors = _anchors(sections, summary, ticket_text)
    by_number = {s.number: s.index for s in sections if s.number}
    by_name = {}
    for s in sections:
        for name in _SNAKE.findall(s.title.replace(" ", " ")):
            by_name.setdefault(name, s.index)

    picked: list[int] = []

    def add(i: int | None) -> None:
        if i is not None and i not in picked:
            picked.append(i)

    for a in anchors:
        add(a)
    # the definitions of the tables this ticket works on, right after its own sections (D170)
    defined = {}
    for s in sections:
        for name in _CREATE.findall(_clean(s.body)):
            defined.setdefault(name.lower(), s.index)
    named = set(_SNAKE.findall(_clean(ticket_text)))
    for a in anchors:
        named |= set(_SNAKE.findall(_clean(sections[a].body)))
    tables = [t for t in defined if t in named]
    tables += [t for t in related_tables(list(defined), resource_names(ticket_text))
               if t not in tables]  # fmt: skip
    for t in tables[:MAX_TABLE_SECTIONS]:
        i = defined[t]
        parent = sections[i].parent  # "10.8 buckets and rules": the intro both share
        if parent is not None and _SNAKE.search(sections[parent].title):
            add(parent)
        add(i)
    ticket_codes = set(_CODE.findall(_clean(ticket_text)))
    for a in anchors:
        body = _clean(sections[a].body)
        for ref in _REF.findall(body):
            add(by_number.get(ref))
        for name in _SNAKE.findall(body):  # table / entity sections the anchor names
            add(by_name.get(name))
        codes = ticket_codes | set(_CODE.findall(body))
        errors = [s for s in sections if "error" in s.title.lower() and s.body.strip()]
        ranked = sorted(errors, key=lambda s: -len(codes & set(_CODE.findall(_clean(s.body)))))
        for s in ranked[:2]:
            if codes & set(_CODE.findall(_clean(s.body))):
                add(s.index)
        parent = sections[a].parent
        while parent is not None:  # chapter intros: shared conventions for their endpoints
            add(parent)
            parent = sections[parent].parent
    common = [s.index for s in sections if re.search(r"\bcommon\b.*\b(api|architecture)\b",
                                                     s.title, re.I)]  # fmt: skip
    for i in common[:1]:
        add(i)

    chosen: list[int] = []
    used = 0
    for i in picked:
        s = sections[i]
        text = s.text if i in anchors or not s.children else _intro(s)
        if used + len(text) > budget and chosen:
            continue
        chosen.append(i)
        used += len(text)

    out = [
        "# Spec excerpt for this ticket",
        "",
        "Chosen by the pipeline from `.agent/spec.md` (the whole spec): the sections that specify "
        "this ticket, the definitions of the tables it works on, what they refer to, and their "
        "chapter intros. The table of contents of the whole spec is at the end; Grep "
        "`.agent/spec.md` for anything else you need.",
        "",
    ]
    for i in sorted(chosen):
        s = sections[i]
        out += [s.text if i in anchors or not s.children else _intro(s), ""]
    out += ["# Table of contents (whole spec)", ""]
    out += [f"{'  ' * (s.level - 1)}- {s.title}" for s in sections if s.level <= 3]
    return "\n".join(out).strip() + "\n"


def _intro(s: Section, limit: int = 2500) -> str:
    """A parent's own text before its first subsection, capped."""
    body = s.body.strip()
    if len(body) > limit:
        body = body[:limit].rsplit("\n", 1)[0] + "\n\n(… cut; see the full spec)"
    return f"{'#' * s.level} {s.title}\n\n{body}\n"


_CITE = re.compile(r"§\s*(\d+(?:\.\d+)*)")


def task_excerpt(spec_md: str, cited: str, endpoints: list[tuple[str, str]], codes: set[str],
                 budget: int = 30_000) -> str:  # fmt: skip
    """The spec sections one repo's task relies on (coders and reviewers): the `§n` sections
    the plan cites for it, the sections that specify its endpoints, the definitions of the
    tables its task names, and the error sections with its error codes. A chapter that is only
    cited shows its intro. '' when nothing matches (then the ticket's excerpt is used)."""
    sections = parse_sections(spec_md)
    if not sections:
        return ""
    by_number = {s.number: s.index for s in sections if s.number}
    picked: list[int] = []
    full: set[int] = set()

    def add(i: int | None, whole: bool = False) -> None:
        if i is None:
            return
        if i not in picked:
            picked.append(i)
        if whole:
            full.add(i)

    wanted = {(m.upper(), p.rstrip("/.")) for m, p in endpoints}
    hits = [s.index for s in sections if wanted & _endpoints(s.body)]
    for i in hits:  # the endpoint's own section, not a parent that lists every route
        if not any(c in hits for c in sections[i].children):
            add(i, whole=True)
    for num in _CITE.findall(cited):
        add(by_number.get(num))
    names = set(_SNAKE.findall(_clean(cited)))
    for s in sections:
        for name in _CREATE.findall(_clean(s.body)):
            if name.lower() in names:
                add(s.index, whole=True)
    errors = [s for s in sections if "error" in s.title.lower() and s.body.strip()]
    for s in sorted(errors, key=lambda s: -len(codes & set(_CODE.findall(_clean(s.body)))))[:2]:
        if codes & set(_CODE.findall(_clean(s.body))):
            add(s.index, whole=True)
    if not picked:
        return ""
    note = ("Chosen by the pipeline from the plan: the sections it cites for this task, the ones "
            "that specify its endpoints, the definitions of the tables it names and the matching "
            "errors. `.agent/spec.md` is the whole spec: Grep it for anything else.")  # fmt: skip
    out = ["# Spec sections for this repo's task", "", note, ""]
    used = 0
    for i in sorted(picked):
        s = sections[i]
        text = s.text if (i in full or not s.children) else _intro(s)
        if used + len(text) > budget and used:
            continue
        out += [text, ""]
        used += len(text)
    return "\n".join(out).strip() + "\n"


def defined_tables(md: str) -> set[str]:
    """Names of the tables a markdown text defines (`CREATE TABLE <name>`)."""
    return {n.lower() for n in _CREATE.findall(_clean(md))}


def error_codes(md: str) -> set[str]:
    """UPPER_SNAKE codes in a text (`PARTNER_ID_MISSING`)."""
    return set(_CODE.findall(_clean(md)))
