"""Markdown → Jira wiki markup (Jira Cloud comments, API v2, don't render markdown)."""

from __future__ import annotations

import re

_LANG_ALIASES = {
    "sh": "bash",
    "shell": "bash",
    "console": "bash",
    "curl": "bash",
    "zsh": "bash",
    "js": "javascript",
    "ts": "typescript",
    "yml": "yaml",
    "jsonc": "json",
}

_FENCE_RE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([\w+-]*)\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
_HR_RE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")


def _escape_braces(text: str) -> str:
    return text.replace("{", r"\{").replace("}", r"\}")


def _inline(text: str) -> str:
    """Convert inline markdown in a single line (outside code blocks)."""
    slots: list[str] = []

    def keep(s: str) -> str:
        slots.append(s)
        return f"\x00{len(slots) - 1}\x00"

    # inline code first, so nothing else touches it; its braces are escaped, else `{ page }`
    # becomes `{{{ page }}}`, which Jira reads as a macro (K21)
    text = re.sub(r"`([^`]+)`", lambda m: keep("{{" + _escape_braces(m.group(1)) + "}}"), text)
    # images, then links
    text = re.sub(r"!\[([^\]]*)\]\(([^)\s]+)[^)]*\)", lambda m: keep(f"!{m.group(2)}!"), text)
    text = re.sub(
        r"\[([^\]]+)\]\(([^)\s]+)[^)]*\)",
        lambda m: keep(f"[{_inline(m.group(1))}|{m.group(2)}]"),
        text,
    )
    text = re.sub(r"<(https?://[^>]+)>", lambda m: keep(f"[{m.group(1)}]"), text)
    # escape Jira macro braces in plain text
    text = text.replace("{", r"\{").replace("}", r"\}")
    # bold before italic
    text = re.sub(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1", lambda m: keep(f"*{m.group(2)}*"), text)
    text = re.sub(r"(?<![\w*])\*(?=\S)([^*]+?)(?<=\S)\*(?![\w*])", r"_\1_", text)
    text = re.sub(r"(?<![\w_])_(?=\S)([^_]+?)(?<=\S)_(?![\w_])", r"_\1_", text)
    text = re.sub(r"~~(?=\S)(.+?)(?<=\S)~~", r"-\1-", text)

    while "\x00" in text:
        text = re.sub(r"\x00(\d+)\x00", lambda m: slots[int(m.group(1))], text)
    return text


def _table_cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith(r"\|"):
        line = line[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", line)]


def split_md(text: str, limit: int) -> list[str]:
    """`text` as parts of at most `limit` characters, cut between paragraphs (blank lines) and
    never inside a fenced code block; one paragraph or block longer than `limit` is cut at line
    ends. A text within the limit is returned as it is."""
    if len(text) <= limit:
        return [text]
    blocks: list[str] = []
    current: list[str] = []
    fenced = False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        current.append(line)
        if not fenced and not line.strip():
            blocks.append("\n".join(current))
            current = []
    if current:
        blocks.append("\n".join(current))
    parts: list[str] = []
    part = ""
    for block in blocks:
        pieces = [block]
        if len(block) > limit:  # one huge block: at line ends
            pieces, chunk = [], ""
            for line in block.split("\n"):
                if chunk and len(chunk) + len(line) + 1 > limit:
                    pieces.append(chunk)
                    chunk = ""
                chunk += line + "\n"
            pieces.append(chunk)
        for piece in pieces:
            if part and len(part) + len(piece) + 1 > limit:
                parts.append(part.rstrip())
                part = ""
            part += piece + "\n"
    if part.strip():
        parts.append(part.rstrip())
    return parts


def md_to_jira(md: str) -> str:
    """Headings, bold/italic, inline code, fenced code, lists (nested), links, tables, quotes."""
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    list_stack: list[tuple[int, str]] = []  # (indent, "*" | "#")
    i = 0
    while i < len(lines):
        line = lines[i]

        fence = _FENCE_RE.match(line)
        if fence:
            marker = fence.group(2)
            lang = fence.group(3).lower()
            lang = _LANG_ALIASES.get(lang, lang)
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(marker):
                body.append(lines[i])
                i += 1
            i += 1  # closing fence
            list_stack.clear()
            out.append("{code:" + lang + "}" if lang else "{code}")
            out.extend(body)
            out.append("{code}")
            continue

        # tables: header row followed by a separator row
        if "|" in line and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1]):
            list_stack.clear()
            out.append("||" + "||".join(_inline(c) for c in _table_cells(line)) + "||")
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                cells = [_inline(c) or " " for c in _table_cells(lines[i])]
                out.append("|" + "|".join(cells) + "|")
                i += 1
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            list_stack.clear()
            out.append(f"h{len(heading.group(1))}. {_inline(heading.group(2))}")
            i += 1
            continue

        if _HR_RE.match(line):
            list_stack.clear()
            out.append("----")
            i += 1
            continue

        item = _LIST_RE.match(line)
        if item:
            indent = len(item.group(1).replace("\t", "    "))
            kind = "#" if item.group(2)[0].isdigit() else "*"
            while list_stack and list_stack[-1][0] > indent:
                list_stack.pop()
            if list_stack and list_stack[-1][0] == indent:
                list_stack[-1] = (indent, kind)
            else:
                list_stack.append((indent, kind))
            prefix = "".join(k for _, k in list_stack)
            out.append(f"{prefix} {_inline(item.group(3))}")
            i += 1
            continue

        if line.lstrip().startswith(">"):
            list_stack.clear()
            out.append("bq. " + _inline(line.lstrip()[1:].strip()))
            i += 1
            continue

        if not line.strip():
            list_stack.clear()
            out.append("")
        elif list_stack and line.startswith(" "):
            # continuation of the previous list item
            out[-1] += " " + _inline(line.strip())
        else:
            list_stack.clear()
            out.append(_inline(line))
        i += 1

    return "\n".join(out).strip("\n")
