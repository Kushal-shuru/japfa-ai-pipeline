"""Tech specs from Google Docs. Implements `SpecPort`.

- OAuth desktop flow (first run opens a browser), token cached at GOOGLE_TOKEN_PATH.
- `fetch_document(url)`: Drive export as `text/markdown` (fallback: HTML → markdownify), cached
  in `$PIPELINE_HOME/specs/<docId>.md` and refreshed only when Drive `modifiedTime` changes.
- `heading_title(url)`: resolves `#heading=h.xxx` to the heading text via the Docs API;
  `extract_section(md, title)` then cuts that section out of the markdown (`fetch_section`).
- `fetch(url)` (SpecPort): always the **whole** document, even if the URL has `#heading=` (D66):
  sections reference each other, so agents locate the feature themselves from the ticket.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from markdownify import markdownify

from pipeline.integrations.base import IntegrationError

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
]

DOC_ID_RE = re.compile(r"docs\.google\.com/document/(?:u/\d+/)?d/([A-Za-z0-9_-]+)")
HEADING_ID_RE = re.compile(r"[#&?]heading=(h\.[A-Za-z0-9_-]+)")
MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


def doc_id(url: str) -> str:
    m = DOC_ID_RE.search(url)
    if not m:
        raise IntegrationError(f"google: not a Google Docs URL: {url}", service="google")
    return m.group(1)


def heading_id(url: str) -> str | None:
    m = HEADING_ID_RE.search(url)
    return m.group(1) if m else None


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_MD_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+.!<>|~-])")


def unescape_markdown(md: str) -> str:
    """Drop markdown backslash escapes (`config\\_id` → `config_id`) that Google's export adds,
    so agents don't copy them into code. Fenced blocks and inline code spans are untouched."""
    out: list[str] = []
    in_fence = False
    for line in md.split("\n"):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        parts = line.split("`")
        # even indexes are outside inline code spans
        parts = [_MD_ESCAPE_RE.sub(r"\1", p) if i % 2 == 0 else p for i, p in enumerate(parts)]
        out.append("`".join(parts))
    return "\n".join(out)


def _norm(title: str) -> str:
    title = re.sub(r"\{#[^}]*\}\s*$", "", title)  # {#anchor}
    title = re.sub(r"[*_`\\]", "", title)
    return re.sub(r"\s+", " ", title).strip().casefold()


def extract_section(md: str, heading: str) -> str | None:
    """The heading line and its content up to the next heading of the same or higher level."""
    lines = md.splitlines()
    target = _norm(heading)
    in_fence = False
    start = level = None
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = MD_HEADING_RE.match(line)
        if not m:
            continue
        this_level = len(m.group(1))
        if start is None:
            if _norm(m.group(2)) == target:
                start, level = i, this_level
        elif this_level <= level:  # type: ignore[operator]
            return "\n".join(lines[start:i]).strip()
    return "\n".join(lines[start:]).strip() if start is not None else None


def _paragraph_text(paragraph: dict) -> str:
    return "".join(
        (el.get("textRun") or {}).get("content", "") for el in paragraph.get("elements", [])
    ).strip()


def headings_from_doc(document: dict) -> dict[str, str]:
    """Docs API document → {headingId: heading text} (first tab or legacy body)."""
    bodies = []
    for tab in document.get("tabs") or []:
        bodies.append(((tab.get("documentTab") or {}).get("body")) or {})
    if not bodies:
        bodies.append(document.get("body") or {})
    out: dict[str, str] = {}
    for body in bodies:
        for el in body.get("content", []):
            para = el.get("paragraph")
            if not para:
                continue
            hid = (para.get("paragraphStyle") or {}).get("headingId")
            if hid:
                out[hid] = _paragraph_text(para)
    return out


class GoogleDocsSpec:
    def __init__(
        self,
        client_path: Path | None,
        token_path: Path | None,
        cache_dir: Path,
        *,
        drive: Any = None,
        docs: Any = None,
        credentials_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.client_path = client_path
        self.token_path = token_path
        self.cache_dir = cache_dir
        self._drive = drive
        self._docs = docs
        self._credentials_factory = credentials_factory or self._credentials

    # ---------- auth ----------

    def _credentials(self) -> Any:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow

        if not self.client_path or not self.token_path:
            raise IntegrationError(
                "google: GOOGLE_OAUTH_CLIENT_PATH / GOOGLE_TOKEN_PATH not set", service="google"
            )
        creds = None
        if self.token_path.exists():
            creds = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
        if creds and creds.valid:
            return creds
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
        if not creds or not creds.valid:
            if not self.client_path.exists():
                raise IntegrationError(
                    f"google: OAuth client file {self.client_path} not found", service="google"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(self.client_path), SCOPES)
            creds = flow.run_local_server(port=0)
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(creds.to_json())
        self.token_path.chmod(0o600)
        return creds

    def _services(self) -> tuple[Any, Any]:
        if self._drive is None or self._docs is None:
            from googleapiclient.discovery import build

            creds = self._credentials_factory()
            self._drive = self._drive or build(
                "drive", "v3", credentials=creds, cache_discovery=False
            )
            self._docs = self._docs or build("docs", "v1", credentials=creds, cache_discovery=False)
        return self._drive, self._docs

    @staticmethod
    def _execute(what: str, request: Any) -> Any:
        from googleapiclient.errors import HttpError

        try:
            return request.execute(num_retries=3)
        except HttpError as err:
            status = getattr(err.resp, "status", None)
            raise IntegrationError(
                f"google: {what} failed (HTTP {status}): {err.reason}", service="google"
            ) from err

    # ---------- SpecPort ----------

    def fetch(self, url: str) -> tuple[str, str]:
        """SpecPort: the whole document (unescaped markdown) + its sha256. A `#heading=` anchor
        is ignored (D66)."""
        md, _ = self.fetch_document(url)
        md = unescape_markdown(md)
        return md, sha256(md)

    def fetch_document(self, url: str) -> tuple[str, str]:
        """Whole document as markdown + its sha256. Cached until Drive modifiedTime changes."""
        did = doc_id(url)
        drive, _ = self._services()
        meta = self._execute(
            f"metadata {did}",
            drive.files().get(fileId=did, fields="id,name,modifiedTime", supportsAllDrives=True),
        )
        modified = meta.get("modifiedTime", "")
        md_path = self.cache_dir / f"{did}.md"
        meta_path = self.cache_dir / f"{did}.hash"
        if md_path.exists() and meta_path.exists():
            cached = json.loads(meta_path.read_text())
            if cached.get("modifiedTime") == modified:
                return md_path.read_text(), cached["sha256"]

        md = self._export_markdown(drive, did)
        digest = sha256(md)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        md_path.write_text(md)
        meta_path.write_text(
            json.dumps({"modifiedTime": modified, "sha256": digest, "name": meta.get("name")})
        )
        return md, digest

    def _export_markdown(self, drive: Any, did: str) -> str:
        try:
            data = self._execute(
                f"export {did} as markdown",
                drive.files().export(fileId=did, mimeType="text/markdown"),
            )
            return _decode(data)
        except IntegrationError:
            html = _decode(
                self._execute(
                    f"export {did} as html", drive.files().export(fileId=did, mimeType="text/html")
                )
            )
            return (
                markdownify(
                    html, heading_style="ATX", escape_underscores=False, escape_asterisks=False
                ).strip()
                + "\n"
            )

    def heading_title(self, url: str) -> str | None:
        """Text of the heading `#heading=h.xxx` points at (None if the URL has no heading)."""
        hid = heading_id(url)
        if not hid:
            return None
        _, docs = self._services()
        document = self._execute(
            f"document {doc_id(url)}",
            docs.documents().get(documentId=doc_id(url), includeTabsContent=True),
        )
        title = headings_from_doc(document).get(hid)
        if title is None:
            raise IntegrationError(
                f"google: heading {hid} not found in document {doc_id(url)}", service="google"
            )
        return title

    def fetch_section(self, url: str) -> tuple[str, str, str | None]:
        """(markdown, sha256, heading) — the linked section if the URL has `#heading=`."""
        md, _ = self.fetch_document(url)
        title = self.heading_title(url)
        if title:
            section = extract_section(md, title)
            if section is None:
                raise IntegrationError(
                    f"google: heading {title!r} not found in exported markdown", service="google"
                )
            md = section
        md = unescape_markdown(md)
        return md, sha256(md), title


def _decode(data: Any) -> str:
    return data.decode("utf-8") if isinstance(data, bytes | bytearray) else str(data)
