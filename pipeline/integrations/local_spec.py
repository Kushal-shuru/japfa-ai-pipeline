"""Tech specs kept as files in the pipeline's `tech_spec/` folder (`spec.source: local`).

`implement KEY` lists the folder and the person picks one by number or file name; the ticket
keeps the choice as a `file://` link, so everything that reads a spec (`context.build_context`)
goes through the same `SpecPort.fetch(url)`. `SpecRouter` sends `file://` links to `LocalSpec` and
every other link to the Google Docs reader, so tickets that carry a Google link keep working.

Only files inside the configured folder are ever read, whatever link is given.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from pipeline.integrations.base import IntegrationError

SPEC_SUFFIXES = (".docx", ".md", ".markdown", ".txt")  # Word, or Markdown / plain text
FILE_SCHEME = "file"


def project_root() -> Path:
    """The pipeline repository's folder (where `tech_spec/` lives by default)."""
    return Path(__file__).resolve().parents[2]


def spec_dir(configured: str) -> Path:
    """`spec.local_dir`: a relative path is relative to the pipeline repository."""
    path = Path(configured).expanduser()
    return path if path.is_absolute() else project_root() / path


def list_specs(directory: Path) -> list[Path]:
    """The documents in the folder (not hidden, not in sub-folders), by name."""
    if not directory.is_dir():
        return []
    files = [p for p in directory.iterdir()
             if p.is_file() and not p.name.startswith((".", "~$"))  # `~$x.docx` = Word's lock file
             and p.suffix.lower() in SPEC_SUFFIXES]
    return sorted(files, key=lambda p: p.name.lower())


def is_local_url(url: str) -> bool:
    return urlparse((url or "").strip()).scheme == FILE_SCHEME


def path_of(url: str) -> Path:
    parsed = urlparse(url.strip())
    return Path(url2pathname(unquote(parsed.path)))


def _runs_md(paragraph: Any) -> str:
    """The paragraph's text with bold / italic and links as Markdown."""
    from docx.text.hyperlink import Hyperlink

    out = []
    for item in paragraph.iter_inner_content():
        if isinstance(item, Hyperlink):
            out.append(f"[{item.text}]({item.url})" if item.url and item.text else item.text)
            continue
        text = item.text
        if not text.strip():
            out.append(text)
        elif item.bold and item.italic:
            out.append(f"***{text.strip()}***" + (" " if text.endswith(" ") else ""))
        elif item.bold:
            out.append(f"**{text.strip()}**" + (" " if text.endswith(" ") else ""))
        elif item.italic:
            out.append(f"*{text.strip()}*" + (" " if text.endswith(" ") else ""))
        else:
            out.append(text)
    return "".join(out).strip()


def _cell_md(cell: Any) -> str:
    parts = [_runs_md(p) for p in cell.paragraphs]
    return " ".join(p for p in parts if p).replace("|", "\\|")


def _table_md(table: Any) -> str:
    rows = [[_cell_md(c) for c in row.cells] for row in table.rows]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def _heading_level(style: str) -> int:
    low = style.lower()
    if low == "title":
        return 1
    if low.startswith("heading ") and low.split()[-1].isdigit():
        return min(int(low.split()[-1]), 6)
    return 0


def docx_to_markdown(path: Path) -> str:
    """A Word document as Markdown, in document order: headings (Heading 1-6 / Title), lists,
    tables, bold / italic and links. The planner finds a ticket's section by its headings and
    reads tables as tables, so those survive; images and text boxes do not."""
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(str(path))
    blocks: list[str] = []
    for child in doc.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "tbl":
            md = _table_md(Table(child, doc))
            if md:
                blocks.append(md)
        elif tag == "p":
            para = Paragraph(child, doc)
            text = _runs_md(para)
            if not text:
                continue
            level = _heading_level(para.style.name if para.style is not None else "")
            ppr = child.pPr
            numbered = ppr is not None and ppr.numPr is not None
            if level:
                blocks.append(f"{'#' * level} {text.replace('**', '')}")
            elif numbered or (para.style is not None and para.style.name.startswith("List")):
                depth = ppr.numPr.ilvl.val if numbered and ppr.numPr.ilvl is not None else 0
                blocks.append(f"{'  ' * int(depth or 0)}- {text}")
            else:
                blocks.append(text)
    # list items stay together; everything else is a paragraph of its own
    out: list[str] = []
    for i, b in enumerate(blocks):
        if i and b.lstrip().startswith("- ") and blocks[i - 1].lstrip().startswith("- "):
            out[-1] += "\n" + b
        else:
            out.append(b)
    return "\n\n".join(out) + "\n"


class LocalSpec:
    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def fetch(self, url: str) -> tuple[str, str]:
        path = path_of(url).resolve()
        root = self.directory.resolve()
        if root not in path.parents or path.suffix.lower() not in SPEC_SUFFIXES:
            raise IntegrationError(f"spec: {path.name} is not a document in {root}",
                                   service="local")
        try:
            md = docx_to_markdown(path) if path.suffix.lower() == ".docx" \
                else path.read_text(encoding="utf-8")
        except Exception as err:  # noqa: BLE001 (unreadable / corrupt file: a reason, not a crash)
            raise IntegrationError(f"spec: cannot read {path.name}: {err}",
                                   service="local") from err
        return md, hashlib.sha256(md.encode()).hexdigest()


class SpecRouter:
    """`file://` links → the local folder; anything else → the Google Docs reader."""

    def __init__(self, google: Any, local: LocalSpec) -> None:
        self.google = google
        self.local = local

    def fetch(self, url: str) -> tuple[str, str]:
        return (self.local if is_local_url(url) else self.google).fetch(url)

    def __getattr__(self, name: str) -> Any:  # fetch_document / fetch_section … stay Google's
        return getattr(self.google, name)
