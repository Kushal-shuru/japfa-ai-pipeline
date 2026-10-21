"""Review findings (Phase 5): numbering, fix scopes, the fixed format, diff positions.

Pure functions over `TicketState` values, shared by the review / fix nodes, the fix gate and
the command bus, so the `#n` people see on the card is the `#n` the bus resolves.

- A finding keeps its id for life: `R<round>-<repo>-F<k>` (repo review) or `R<round>-X-F<k>`
  (contract check); its GitLab thread carries the marker `aipl:<KEY>:review:<round>:<id>`.
- `#n` numbers are per round: the round's open findings across all repos, by severity, then
  merge order, then id (`numbered`). The ticket is paused while people choose, so the numbers
  don't move between the card and the command.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from pipeline.markers import find_markers
from pipeline.schemas import Finding, ReviewResult

SEVERITY_ORDER = ("blocker", "human", "major", "minor", "nit")
# must be fixed before approval (a person's own change request counts as must-fix)
BLOCKING = frozenset({"blocker", "major", "human"})
SCOPE_SEVERITIES: dict[str, frozenset[str]] = {
    "blockers": frozenset({"blocker", "human"}),
    "major+": frozenset({"blocker", "major", "human"}),
    "minor": frozenset({"blocker", "major", "human", "minor"}),  # all but nits (D168)
    "all": frozenset(SEVERITY_ORDER),
}
SCOPE_WORDS = {"blockers": "the blockers", "major+": "the blockers and major findings",
               "minor": "every finding except the nits", "all": "all findings",
               "ids": "the chosen findings"}  # fmt: skip
CONTRACT = "contract"
# R<round>-<repo|X>-F<k> (review), H<n>-<repo>-F<k> (a person's change request, §4b),
# A-<repo|X>-<AC4|T1|V1> (the orchestrator's own, P9: an unmet criterion or a bare veto)
FINDING_ID_RE = re.compile(r"^(?:[RH]\d+-[\w.-]+-F\d+|A-[\w.-]+-[A-Z]+\d+)$", re.I)
CONTRACT_TAG = "X"
REVIEW_NODE = "review"
DERIVED_PREFIX = "A-"


# ---------- ids ----------


def finding_id(round_: int, repo_or_x: str, k: int) -> str:
    return f"R{round_}-{repo_or_x}-F{k}"


def derived_id(repo_or_x: str, ref: str) -> str:
    """The id of a finding the orchestrator owns (P9): one per repo and criterion (or veto),
    the same every round, so it stays open until the criterion is met."""
    return f"{DERIVED_PREFIX}{repo_or_x}-{ref}"


def is_derived_id(fid: str) -> bool:
    return fid.startswith(DERIVED_PREFIX)


def is_contract_id(fid: str) -> bool:
    return f"-{CONTRACT_TAG}-F" in fid or fid.startswith(f"{DERIVED_PREFIX}{CONTRACT_TAG}-")


def ids_from_markers(bodies: Iterable[str], key: str) -> list[str]:
    """Finding ids in the review markers of a thread's notes (`aipl:KEY:review:<r>:<id>`)."""
    out: list[str] = []
    for body in bodies:
        for m in find_markers(body):
            parts = m.split(":", 4)
            fid = parts[4] if len(parts) == 5 else ""
            ours = len(parts) == 5 and parts[1] == key and parts[2] == REVIEW_NODE
            if ours and FINDING_ID_RE.match(fid) and fid not in out:
                out.append(fid)
    return out


# ---------- the round's open findings ----------


def _repo_rank(state: Any) -> dict[str, int]:
    repos = state.get("repos") or {}
    return {r: w.get("order", 99) for r, w in repos.items()}


def open_findings(state: Any) -> list[Finding]:
    """Every open finding of the latest round: each repo's review + the contract check. A
    finding on a merged repo is left out: that MR can't change any more (K40)."""
    repos = state.get("repos") or {}
    merged = {r for r, w in repos.items() if w.get("status") == "merged"}
    out: list[Finding] = []
    for work in repos.values():
        res: ReviewResult | None = work.get("review_result")
        if res is not None:
            out += res.findings
    contract: ReviewResult | None = state.get("contract_review")
    if contract is not None:
        out += contract.findings
    return list({f.id: f for f in out if f.repo not in merged}.values())


def sort_findings(findings: list[Finding], state: Any) -> list[Finding]:
    rank = _repo_rank(state)

    def k(f: Finding) -> tuple:
        return (SEVERITY_ORDER.index(f.severity), is_contract_id(f.id), rank.get(f.repo, 99),
                f.repo, _natural(f.id))  # fmt: skip

    return sorted(findings, key=k)


def _natural(fid: str) -> list:
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", fid)]


def numbered(state: Any) -> list[tuple[int, Finding]]:
    return list(enumerate(sort_findings(open_findings(state), state), 1))


def numbers_by_id(state: Any) -> dict[str, int]:
    return {f.id: n for n, f in numbered(state)}


# ---------- choosing what to fix ----------


def in_scope(mode: str, findings: list[Finding], ids: Iterable[str] = ()) -> list[Finding]:
    if mode == "ids":
        chosen = set(ids)
        return [f for f in findings if f.id in chosen]
    severities = SCOPE_SEVERITIES.get(mode, SCOPE_SEVERITIES["major+"])
    return [f for f in findings if f.severity in severities]


def resolve_choice(state: Any, numbers: Iterable[int] = (), ids: Iterable[str] = ()) -> tuple[
        list[str], list[str]]:  # fmt: skip
    """`#n` numbers and/or finding ids → (open finding ids, unknown references)."""
    by_n = dict(numbered(state))
    open_ids = {f.id.upper(): f.id for f in by_n.values()}
    out: list[str] = []
    unknown: list[str] = []
    for n in numbers:
        if n in by_n:
            out.append(by_n[n].id)
        else:
            unknown.append(f"#{n}")
    for i in ids:
        if i.upper() in open_ids:
            out.append(open_ids[i.upper()])
        else:
            unknown.append(i)
    return list(dict.fromkeys(out)), unknown


NUMBER_RE = re.compile(r"#\s?(\d+)")


def mentioned_numbers(text: str | None) -> list[int]:
    """`#2` references in people's notes ("#2 is fine because …")."""
    return [int(n) for n in NUMBER_RE.findall(text or "")]


_DISMISS_RE = re.compile(r"\bdismiss\s+(#?\s?\d+(?:\s*(?:,|and|&)?\s*#?\s?\d+)*)", re.I)


def dismissed_in(text: str | None) -> list[int]:
    """Numbers after the word `dismiss` ("dismiss #2 and #4: …"): the only way the exact grammar
    marks a finding as fine (P9; a number merely mentioned in the notes is not a decision)."""
    out: list[int] = []
    for m in _DISMISS_RE.finditer(text or ""):
        out += [int(n) for n in re.findall(r"\d+", m.group(1))]
    return out


def note_for(number: int, notes: str | None) -> str:
    """The sentences of `notes` that mention `#number` (what the person said about it)."""
    if not notes:
        return ""
    parts = re.split(r"(?<=[.;!?])\s+|\s+[—–]\s+|,\s+(?=#\d)", notes)
    return " ".join(p.strip() for p in parts if number in mentioned_numbers(p)).strip()


_SEL_ITEM = r"(?:#?\s?\d+|[RH]\d+-[\w.-]+-F\d+)"
_SEL_RE = re.compile(rf"^\s*({_SEL_ITEM}(?:\s*(?:,|and|&|\s)\s*{_SEL_ITEM})*)\s*(?:[—–:;-]\s*|$)",
                     re.I)  # fmt: skip


def parse_selection(text: str) -> tuple[list[int], list[str], str]:
    """ "1, 3 — #2 is fine because …" → ([1, 3], [], "#2 is fine because …"). A leading
    list of numbers / finding ids, then the notes. No leading list → everything is notes."""
    m = _SEL_RE.match(text or "")
    if not m:
        return [], [], (text or "").strip()
    numbers: list[int] = []
    ids: list[str] = []
    for item in re.findall(_SEL_ITEM, m.group(1), re.I):
        item = item.strip()
        if item[:1].upper() in "RH" and "-F" in item.upper():
            ids.append(item.upper())
        else:
            numbers.append(int(item.lstrip("#").strip()))
    return numbers, ids, text[m.end() :].strip()


# ---------- the fixed format (D87) ----------

WHY_RE = re.compile(r"\n*\s*\**why it matters:?\**:?\s*", re.I)


def location(f: Finding) -> str:
    if f.file and f.line:
        return f"{f.file}:{f.line}"
    return f.file or "general"


def split_comment(comment: str) -> tuple[str, str]:
    """ "What's wrong … Why it matters: …" → (what, why)."""
    parts = WHY_RE.split(comment.strip(), maxsplit=1)
    return parts[0].strip(), (parts[1].strip() if len(parts) > 1 else "")


def short(text: str) -> str:
    """`text` on one line. It used to cut at a length; nothing people read is cut now."""
    return " ".join(text.split())


def plain_comment(f: Finding) -> str:
    """What's wrong and why it matters as one short paragraph (an old-style "Why it matters:"
    part is folded in)."""
    what, why = split_comment(f.comment)
    why = why[:1].upper() + why[1:]
    return " ".join(p for p in (what, why) if p)


def heading(n: int | None, f: Finding, with_location: bool = True) -> str:
    """ "#1 · major · validation" (+ `file:line` when the comment isn't on the line itself)."""
    parts = [f"#{n}"] if n is not None else []
    parts += [f.severity, "contract" if is_contract_id(f.id) else f.category]
    head = "**" + " · ".join(parts) + "**"
    if with_location and f.file:
        head += f" `{location(f)}`"
    return head


def finding_md(n: int | None, f: Finding, inline: bool = True, contract_links: str = "") -> str:
    """One finding on GitLab (D109): a heading line, one short paragraph (what's wrong and why),
    then the suggestion (a ```` ```suggestion ```` block only inline, where GitLab can apply
    it). Inline comments sit on the line, so their heading has no location."""
    out = [heading(n, f, with_location=not inline), plain_comment(f)]
    if f.suggestion and f.suggestion.strip():
        sug = f.suggestion.strip()
        if not inline:
            sug = sug.replace("```suggestion", "```")
        out.append(f"**Suggestion:**\n{sug}" if sug.startswith("```") else
                   f"**Suggestion:** {sug}")  # fmt: skip
    if contract_links:
        out.append(contract_links)
    return "\n\n".join(out)


def one_line(n: int, f: Finding, with_location: bool = True) -> str:
    """ "#1 major · demo-ws1-closio `src/a.js:12`: The body schema allows any status. It …" (Jira,
    the chat), the whole comment; `with_location=False` for the card."""
    text = plain_comment(f) if f.comment.strip() else f.category
    where = f"{CONTRACT} (change in {f.repo})" if is_contract_id(f.id) else f.repo
    loc = f" `{location(f)}`" if with_location and f.file else ""
    return f"#{n} {f.severity} · {where}{loc}: {short(text)}"


def counts(findings: list[Finding]) -> dict[str, int]:
    out = {s: 0 for s in SEVERITY_ORDER}
    for f in findings:
        out[f.severity] += 1
    return {s: c for s, c in out.items() if c}


def counts_words(findings: list[Finding]) -> str:
    """ "2 must-fix issues, 1 suggestion" (must-fix = blocker / major / human)."""
    must = sum(1 for f in findings if f.severity in BLOCKING)
    rest = len(findings) - must
    parts = []
    if must:
        parts.append(f"{must} must-fix issue" + ("" if must == 1 else "s"))
    if rest:
        parts.append(f"{rest} suggestion" + ("" if rest == 1 else "s"))
    return ", ".join(parts) or "no findings"


# ---------- diff positions ----------

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def diff_new_lines(diff: str) -> dict[str, set[int]]:
    """New-file line numbers GitLab can anchor a thread to (added + context lines), per path."""
    out: dict[str, set[int]] = {}
    path: str | None = None
    line = 0
    for raw in diff.splitlines():
        if raw.startswith("diff --git"):  # next file: nothing counts until its first hunk
            path, line = None, 0
            continue
        if raw.startswith("+++ ") and not line:  # a header only outside a hunk
            target = raw[4:].strip()
            path = None if target == "/dev/null" else target.removeprefix("b/")
            line = 0
            if path is not None:
                out.setdefault(path, set())
            continue
        if raw.startswith("--- ") and not line:
            continue
        m = _HUNK_RE.match(raw)
        if m:
            line = int(m.group(1))
            continue
        if path is None or not line:
            continue
        if raw.startswith("+"):
            out[path].add(line)
            line += 1
        elif raw.startswith("-") or raw.startswith("\\"):
            continue
        else:
            out[path].add(line)
            line += 1
    return out


def position_ok(f: Finding, lines: dict[str, set[int]]) -> bool:
    return bool(f.file and f.line and f.line in lines.get(f.file, set()))
