"""Jira Cloud client (`atlassian-python-api`). Implements `JiraPort`.

Issues and comments use REST API v2 (wiki-markup strings, not ADF); search uses the
Cloud `/rest/api/3/search/jql` endpoint (`enhanced_jql`), which replaced `/search`.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar

from atlassian import Jira
from markdownify import markdownify

from pipeline.config import JiraConfig
from pipeline.formatting import md_to_jira, split_md
from pipeline.integrations.base import IntegrationError
from pipeline.integrations.retry import with_retry
from pipeline.logging import mask_text
from pipeline.markers import JIRA_PROPERTY, jira_comment_markers

T = TypeVar("T")

URL_RE = re.compile(r"https?://[^\s|\]\[<>\"')]+")
PAGE = 100
PART_CHARS = 28_000  # markdown per Jira comment: under its 32,767-character limit after conversion


def parse_jira_datetime(value: str) -> datetime:
    """Jira timestamps look like 2026-09-26T10:00:00.000+0000."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return datetime.fromisoformat(value)


def field_values(value: Any) -> list[str]:
    """Checkbox / multi-select → list of option values. Also accepts a single option or str."""
    if value is None:
        return []
    if isinstance(value, dict):
        value = [value]
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    out = []
    for v in value:
        if isinstance(v, dict):
            v = v.get("value") or v.get("name")
        if v:
            out.append(str(v).strip())
    return out


def extract_urls(value: Any) -> list[str]:
    """URLs from a URL / text / rich field value, in order, de-duplicated."""
    if value is None:
        return []
    text = value if isinstance(value, str) else str(value)
    seen: list[str] = []
    for url in URL_RE.findall(text):
        url = url.rstrip(".,;")
        if url not in seen:
            seen.append(url)
    return seen


class JiraClient:
    """Jira as the pipeline's service account. All writes are idempotent."""

    def __init__(
        self,
        base_url: str,
        email: str,
        api_token: str,
        config: JiraConfig,
        store: Any = None,
        *,
        api: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.store = store
        self._sleep = sleep
        self.api = api or Jira(
            url=base_url, username=email, password=api_token, cloud=True, api_version="2"
        )

    # ---------- helpers ----------

    def _call(self, what: str, fn: Callable[[], T]) -> T:
        return with_retry(fn, service="jira", what=what, sleep=self._sleep)

    def _raw_issue(self, key: str, fields: str, expand: str | None = None) -> dict:
        issue = self._call(f"get {key}", lambda: self.api.issue(key, fields=fields, expand=expand))
        if not issue or "fields" not in issue:
            raise IntegrationError(f"jira: issue {key} not found", service="jira")
        return issue

    def _record(self, key: str, type: str, data: dict) -> None:
        if self.store is not None:
            self.store.record_event(key, type, node=data.pop("node", None), data=data)

    # ---------- reads ----------

    def get_issue(self, key: str) -> dict:
        """Normalised issue: key, summary, description (+ _md), labels, status, parent/epic,
        repos (Repos field values), spec (raw Tech Spec value) and spec_urls."""
        c = self.config
        fields = ",".join(["summary", "description", "labels", "status", "parent", "issuetype",
                           *[f for f in (c.repos_field, c.spec_field) if f]])  # fmt: skip
        issue = self._raw_issue(key, fields, expand="renderedFields")
        f = issue["fields"]
        rendered = (issue.get("renderedFields") or {}).get("description") or ""
        description = f.get("description") or ""
        parent = f.get("parent") or {}
        parent_type = ((parent.get("fields") or {}).get("issuetype") or {}).get("name")
        spec = f.get(c.spec_field) if c.spec_field else None
        return {
            "key": issue.get("key", key),
            "summary": f.get("summary") or "",
            "description": description,
            "description_md": markdownify(
                rendered, heading_style="ATX", escape_underscores=False, escape_asterisks=False
            ).strip()
            if rendered
            else description,
            "labels": list(f.get("labels") or []),
            "status": ((f.get("status") or {}).get("name")) or "",
            "issuetype": ((f.get("issuetype") or {}).get("name")) or "",
            "parent_key": parent.get("key"),
            "parent_type": parent_type,
            "epic_key": parent.get("key") if parent_type == "Epic" else None,
            "repos": field_values(f.get(c.repos_field)) if c.repos_field else [],
            "spec": spec,
            "spec_urls": extract_urls(spec),
        }

    def current_status(self, key: str) -> str:
        return self._raw_issue(key, "status")["fields"]["status"]["name"]

    def labels(self, key: str) -> list[str]:
        return list(self._raw_issue(key, "labels")["fields"].get("labels") or [])

    def search(self, jql: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            page = self._call(
                "search",
                lambda t=token: self.api.enhanced_jql(
                    jql, fields="key", nextPageToken=t, limit=PAGE
                ),
            )
            page = page or {}
            keys += [i["key"] for i in page.get("issues", [])]
            token = page.get("nextPageToken")
            if page.get("isLast", True) or not token:
                return keys

    def comments(self, key: str) -> list[dict]:
        """All comments, oldest first: {id, author, author_id, body, created, updated, marker}
        (`marker`: the pipeline's hidden comment property, None on other comments)."""
        url = self.api.resource_url(f"issue/{key}/comment")
        out: list[dict] = []
        start = 0
        while True:
            page = self._call(
                f"comments {key}",
                lambda s=start: self.api.get(
                    url,
                    params={
                        "startAt": s,
                        "maxResults": PAGE,
                        "orderBy": "created",
                        "expand": "properties",
                    },
                ),
            )
            page = page or {}
            items = page.get("comments", [])
            for c in items:
                author = c.get("author") or {}
                props = {p.get("key"): p.get("value") for p in c.get("properties") or []}
                out.append(
                    {
                        "id": c.get("id"),
                        "author": author.get("displayName"),
                        "author_id": author.get("accountId"),
                        "body": c.get("body") or "",
                        "created": parse_jira_datetime(c["created"]),
                        "updated": parse_jira_datetime(c.get("updated") or c["created"]),
                        "marker": (props.get(JIRA_PROPERTY) or {}).get("marker"),
                    }
                )
            start += len(items)
            if not items or start >= int(page.get("total", start)):
                return out

    def comments_since(self, key: str, since: datetime) -> list[dict]:
        return [c for c in self.comments(key) if c["created"] > since]

    # ---------- writes ----------

    def transition(self, key: str, status_name: str) -> None:
        """Move to `status_name` (by target status, then transition name). No-op if there."""
        current = self.current_status(key)
        if current.casefold() == status_name.casefold():
            return
        transitions = self._call(f"transitions {key}", lambda: self.api.get_issue_transitions(key))
        match = next(
            (t for t in transitions if t["to"].casefold() == status_name.casefold()), None
        ) or next((t for t in transitions if t["name"].casefold() == status_name.casefold()), None)
        if match is None:
            reachable = sorted({t["to"] for t in transitions})
            raise IntegrationError(
                f"jira: cannot move {key} from {current!r} to {status_name!r}; "
                f"reachable from here: {reachable}",
                service="jira",
            )
        url = self.api.resource_url(f"issue/{key}/transitions")
        self._call(
            f"transition {key}",
            lambda: self.api.post(url, data={"transition": {"id": str(match["id"])}}),
        )
        self._record(key, "transition", {"from": current, "to": match["to"]})

    def comment(self, key: str, body_md: str, marker: str) -> None:
        """Post markdown as wiki markup with the marker as a hidden comment property; skip if
        a comment already carries the marker (property or old text trailer)."""
        issue_key = marker.split(":", 2)[1]
        if any(marker in jira_comment_markers(c, issue_key) for c in self.comments(key)):
            return
        url = self.api.resource_url(f"issue/{key}/comment")
        # a text over Jira's comment limit is never cut: it goes as several comments (parts)
        parts = split_md(mask_text(body_md), PART_CHARS)
        for n, part in enumerate(parts, 1):
            mark = marker if n == 1 else f"{marker}:part{n}"
            text = part if n == 1 else f"_(continued: part {n} of {len(parts)})_\n\n{part}"
            data = {"body": md_to_jira(text),
                    "properties": [{"key": JIRA_PROPERTY, "value": {"marker": mark}}]}  # fmt: skip
            self._call(f"comment {key}", lambda d=data: self.api.post(url, data=d))
            self._record(key, "comment", {"marker": mark})

    def upsert_comment(self, key: str, body_md: str, marker: str) -> None:
        """The one comment with `marker`, edited in place (P9: the delivery summary), or
        posted when it isn't there yet. Unchanged text → no call."""
        issue_key = marker.split(":", 2)[1]
        mine = [c for c in self.comments(key) if marker in jira_comment_markers(c, issue_key)]
        if not mine:
            self.comment(key, body_md, marker)
            return
        body = md_to_jira(mask_text(body_md))
        if mine[-1].get("body") == body:
            return
        url = self.api.resource_url(f"issue/{key}/comment/{mine[-1]['id']}")
        self._call(f"edit comment {key}", lambda: self.api.put(url, data={"body": body}))
        self._record(key, "comment", {"marker": marker, "edited": True})

    def attach(self, key: str, filename: str, content: bytes) -> None:
        """Attach a file (P9, audit F11): masked like every comment; an attachment with the
        same name and the **same content** is kept (idempotent); otherwise the new file is
        uploaded first and only then the old same-name files are removed, so a failed upload
        never leaves the ticket without its note."""
        with contextlib.suppress(UnicodeDecodeError):  # not text: nothing to mask
            content = mask_text(content.decode("utf-8")).encode("utf-8")
        existing = self._raw_issue(key, "attachment")["fields"].get("attachment") or []
        same = [a for a in existing if a.get("filename") == filename]
        for a in same:
            if int(a.get("size") or -1) == len(content) and self._content(a["id"]) == content:
                return
        upload = (filename, content, "text/markdown")  # requests' (name, data, type) file tuple
        self._call(f"attach {filename} to {key}",
                   lambda: self.api.add_attachment_object(key, upload))  # fmt: skip
        for a in same:
            self._call(
                f"remove attachment {a['id']}", lambda i=a["id"]: self.api.remove_attachment(i)
            )
        self._record(key, "comment", {"attachment": filename})

    def _content(self, attachment_id: str) -> bytes:
        data = self._call(f"read attachment {attachment_id}",
                          lambda: self.api.get_attachment_content(attachment_id))  # fmt: skip
        return data if isinstance(data, bytes) else str(data or "").encode("utf-8")

    # ---------- demo-reset (P8 4.1) ----------

    def delete_bot_comments(self, key: str) -> int:
        """Delete the pipeline's own comments (those with a marker); people's stay."""
        issue_key = key
        n = 0
        for c in self.comments(key):
            if not jira_comment_markers(c, issue_key):
                continue
            url = self.api.resource_url(f"issue/{key}/comment/{c['id']}")
            self._call(f"delete comment {c['id']}", lambda u=url: self.api.delete(u))
            n += 1
        return n

    def delete_attachments(self, key: str, prefix: str) -> int:
        existing = self._raw_issue(key, "attachment")["fields"].get("attachment") or []
        mine = [a for a in existing if str(a.get("filename") or "").startswith(prefix)]
        for a in mine:
            self._call(f"remove attachment {a['id']}",
                       lambda i=a["id"]: self.api.remove_attachment(i))  # fmt: skip
        return len(mine)

    # ---------- checks (pipeline check) ----------

    def whoami(self) -> str:
        me = self._call("myself", self.api.myself) or {}
        return me.get("displayName") or me.get("emailAddress") or "?"

    def project_name(self, project: str) -> str:
        p = self._call(f"project {project}", lambda: self.api.get_project(project)) or {}
        return p.get("name", project)

    def field_names(self) -> dict[str, str]:
        """Field id → name for all fields (to confirm the custom fields exist)."""
        fields = self._call("fields", self.api.get_all_fields) or []
        return {f["id"]: f.get("name", "") for f in fields}
