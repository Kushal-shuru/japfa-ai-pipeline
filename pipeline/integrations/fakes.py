"""In-memory fakes for Jira, GitLab and specs. Same idempotency rules as the real clients;
every call is recorded in `.calls` as `(method, args...)`. Used by --dry-run and tests."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any, Literal

from pipeline.formatting import md_to_jira
from pipeline.integrations.base import IntegrationError
from pipeline.markers import gitlab_marker, has_marker, jira_comment_markers


def _now() -> datetime:
    return datetime.now(UTC)


class FakeJira:
    def __init__(self, store: Any = None, clock: Any = _now) -> None:
        self.issues: dict[str, dict] = {}
        self.comments_by_key: dict[str, list[dict]] = {}
        self.attachments: dict[str, dict[str, bytes]] = {}  # key → filename → content
        self.transitions_allowed: dict[str, list[str]] | None = None  # status → reachable
        self.search_results: dict[str, list[str]] = {}
        self.calls: list[tuple] = []
        self.store = store
        self.clock = clock

    # ---------- setup ----------

    def add_issue(
        self,
        key: str,
        summary: str = "",
        description: str = "",
        *,
        repos: list[str] | None = None,
        spec: str | None = None,
        labels: list[str] | None = None,
        status: str = "To Do",
        parent_key: str | None = None,
        parent_type: str | None = None,
    ) -> dict:
        from pipeline.integrations.jira_client import extract_urls

        issue = {
            "key": key,
            "summary": summary,
            "description": description,
            "description_md": description,
            "labels": list(labels or []),
            "status": status,
            "issuetype": "Story",
            "parent_key": parent_key,
            "parent_type": parent_type,
            "epic_key": parent_key if parent_type == "Epic" else None,
            "repos": list(repos or []),
            "spec": spec,
            "spec_urls": extract_urls(spec),
        }
        self.issues[key] = issue
        self.comments_by_key.setdefault(key, [])
        return issue

    def _issue(self, key: str) -> dict:
        if key not in self.issues:
            raise IntegrationError(f"jira: issue {key} not found", service="jira")
        return self.issues[key]

    # ---------- JiraPort ----------

    def search(self, jql: str) -> list[str]:
        self.calls.append(("search", jql))
        return list(self.search_results.get(jql, self.issues))

    def get_issue(self, key: str) -> dict:
        self.calls.append(("get_issue", key))
        return dict(self._issue(key))

    def current_status(self, key: str) -> str:
        return self._issue(key)["status"]

    def transition(self, key: str, status_name: str) -> None:
        self.calls.append(("transition", key, status_name))
        issue = self._issue(key)
        current = issue["status"]
        if current.casefold() == status_name.casefold():
            return
        if self.transitions_allowed is not None:
            reachable = self.transitions_allowed.get(current, [])
            if status_name not in reachable:
                raise IntegrationError(
                    f"jira: cannot move {key} from {current!r} to {status_name!r}; "
                    f"reachable from here: {sorted(reachable)}",
                    service="jira",
                )
        issue["status"] = status_name
        if self.store is not None:
            self.store.record_event(key, "transition", data={"from": current, "to": status_name})

    def comment(self, key: str, body_md: str, marker: str) -> None:
        self.calls.append(("comment", key, marker))
        comments = self.comments_by_key.setdefault(key, [])
        issue_key = marker.split(":", 2)[1]
        if any(marker in jira_comment_markers(c, issue_key) for c in comments):
            return
        c = self.add_comment(key, md_to_jira(body_md), author="pipeline")
        c["marker"] = marker
        if self.store is not None:
            self.store.record_event(key, "comment", data={"marker": marker})

    def upsert_comment(self, key: str, body_md: str, marker: str) -> None:
        self.calls.append(("upsert_comment", key, marker))
        issue_key = marker.split(":", 2)[1]
        mine = [c for c in self.comments_by_key.get(key, [])
                if marker in jira_comment_markers(c, issue_key)]  # fmt: skip
        if not mine:
            self.comment(key, body_md, marker)
            return
        mine[-1].update(body=md_to_jira(body_md), updated=self.clock())
        if self.store is not None:
            self.store.record_event(key, "comment", data={"marker": marker, "edited": True})

    def add_comment(self, key: str, body: str, author: str = "human") -> dict:
        comments = self.comments_by_key.setdefault(key, [])
        c = {
            "id": str(len(comments) + 1),
            "author": author,
            "author_id": author,
            "body": body,
            "created": self.clock(),
            "updated": self.clock(),
        }
        comments.append(c)
        return c

    def comments(self, key: str) -> list[dict]:
        return list(self.comments_by_key.get(key, []))

    def comments_since(self, key: str, since: datetime) -> list[dict]:
        return [c for c in self.comments(key) if c["created"] > since]

    def delete_bot_comments(self, key: str) -> int:
        comments = self.comments_by_key.get(key, [])
        keep = [c for c in comments if not jira_comment_markers(c, key)]
        self.comments_by_key[key] = keep
        return len(comments) - len(keep)

    def delete_attachments(self, key: str, prefix: str) -> int:
        files = self.attachments.get(key, {})
        gone = [n for n in files if n.startswith(prefix)]
        for n in gone:
            del files[n]
        return len(gone)

    def labels(self, key: str) -> list[str]:
        return list(self._issue(key)["labels"])

    def attach(self, key: str, filename: str, content: bytes) -> None:
        self.calls.append(("attach", key, filename))
        self._issue(key)
        files = self.attachments.setdefault(key, {})
        if files.get(filename) == content:
            return
        files[filename] = content


class FakeGitLab:
    def __init__(self, reviewer_username: str = "reviewer") -> None:
        self.projects: dict[str, dict[int, dict]] = {}
        self.diffs: dict[tuple[str, int], str] = {}
        self.commits: dict[tuple[str, int], list[dict]] = {}
        self.calls: list[tuple] = []
        self.deleted_branches: list[tuple[str, str]] = []
        self.reviewer_username = reviewer_username

    def _mr(self, project: str, iid: int) -> dict:
        try:
            return self.projects[project][iid]
        except KeyError:
            raise IntegrationError(
                f"gitlab: MR {project}!{iid} not found", service="gitlab"
            ) from None

    @staticmethod
    def _public(mr: dict) -> dict:
        keys = ("iid", "web_url", "state", "title", "description", "source_branch",
                "target_branch", "sha")  # fmt: skip
        return {k: mr.get(k) for k in keys}

    def find_mr(self, project: str, source_branch: str) -> dict | None:
        mrs = [
            m
            for m in self.projects.get(project, {}).values()
            if m["source_branch"] == source_branch
        ]
        if not mrs:
            return None
        mrs.sort(key=lambda m: m["iid"], reverse=True)
        opened = [m for m in mrs if m["state"] == "opened"]
        return self._public((opened or mrs)[0])

    def create_mr(self, project: str, source: str, target: str, title: str, body_md: str) -> dict:
        self.calls.append(("create_mr", project, source, target))
        existing = self.find_mr(project, source)
        if existing and existing["state"] == "opened":
            return existing
        mrs = self.projects.setdefault(project, {})
        iid = max(mrs, default=0) + 1
        mrs[iid] = {
            "iid": iid,
            "web_url": f"https://gitlab.example/{project}/-/merge_requests/{iid}",
            "state": "opened",
            "title": title,
            "description": body_md,
            "source_branch": source,
            "target_branch": target,
            "sha": f"sha{iid}",
            "notes": [],  # {"body", "author", "role"}
            "threads": [],  # {"id", "path", "line", "resolved", "notes": [...]}
            "approved_by": [],
        }
        return self._public(mrs[iid])

    def update_mr_description(self, project: str, iid: int, body_md: str) -> None:
        self.calls.append(("update_mr_description", project, iid))
        self._mr(project, iid)["description"] = body_md

    def mr_diff(self, project: str, iid: int) -> str:
        self._mr(project, iid)
        return self.diffs.get((project, iid), "")

    def mr_state(self, project: str, iid: int) -> Literal["opened", "merged", "closed"]:
        return self._mr(project, iid)["state"]

    def set_state(self, project: str, iid: int, state: str) -> None:
        self._mr(project, iid)["state"] = state

    def mr_commits(self, project: str, iid: int) -> list[dict]:
        self._mr(project, iid)
        return list(self.commits.get((project, iid), []))

    def _bodies(self, mr: dict) -> list[str]:
        return [n["body"] for n in mr["notes"]] + [
            n["body"] for t in mr["threads"] for n in t["notes"]
        ]

    def add_inline(
        self, project: str, iid: int, path: str, line: int, body_md: str, marker: str
    ) -> None:
        self.calls.append(("add_inline", project, iid, path, line, marker))
        mr = self._mr(project, iid)
        if any(has_marker(b, marker) for b in self._bodies(mr)):
            return
        mr["threads"].append(
            {
                "id": f"d{len(mr['threads']) + 1}",
                "path": path or None,
                "line": line or None,
                "resolved": False,
                "notes": [{"body": body_md + gitlab_marker(marker), "author": "reviewer"}],
            }
        )

    def add_note(
        self, project: str, iid: int, body_md: str, marker: str, as_role: str = "reviewer"
    ) -> None:
        if as_role not in ("coder", "reviewer"):
            raise ValueError(f"unknown GitLab identity {as_role!r}")
        self.calls.append(("add_note", project, iid, marker, as_role))
        mr = self._mr(project, iid)
        if any(has_marker(b, marker) for b in self._bodies(mr)):
            return
        mr["notes"].append({"body": body_md + gitlab_marker(marker), "author": as_role})

    def unresolved_threads(self, project: str, iid: int) -> list[dict]:
        mr = self._mr(project, iid)
        return [
            {"id": t["id"], "path": t["path"], "line": t["line"], "notes": list(t["notes"])}
            for t in mr["threads"]
            if not t["resolved"]
        ]

    def _thread(self, project: str, iid: int, discussion_id: str) -> dict:
        for t in self._mr(project, iid)["threads"]:
            if t["id"] == discussion_id:
                return t
        raise IntegrationError(f"gitlab: discussion {discussion_id} not found", service="gitlab")

    def reply(
        self, project: str, iid: int, discussion_id: str, body_md: str, as_role: str = "coder"
    ) -> None:
        if as_role not in ("coder", "reviewer"):
            raise ValueError(f"unknown GitLab identity {as_role!r}")
        self.calls.append(("reply", project, iid, discussion_id, as_role))
        self._thread(project, iid, discussion_id)["notes"].append(
            {"body": body_md, "author": as_role}
        )

    def resolve(self, project: str, iid: int, discussion_id: str) -> None:
        self.calls.append(("resolve", project, iid, discussion_id))
        self._thread(project, iid, discussion_id)["resolved"] = True

    def list_mrs(self, project: str, source_prefix: str) -> list[dict]:
        return [self._public(m) for m in self.projects.get(project, {}).values()
                if m["source_branch"].startswith(source_prefix)]  # fmt: skip

    def close_mr(self, project: str, iid: int) -> None:
        self._mr(project, iid)["state"] = "closed"

    def delete_branch(self, project: str, branch: str) -> None:
        self.deleted_branches.append((project, branch))

    def approve(self, project: str, iid: int, sha: str | None = None) -> None:
        self.calls.append(("approve", project, iid))
        mr = self._mr(project, iid)
        mr["approved_sha"] = sha
        if self.reviewer_username not in mr["approved_by"]:
            mr["approved_by"].append(self.reviewer_username)


class FakeSpec:
    """URL → markdown. A URL with `#heading=` falls back to the same URL without it."""

    def __init__(self, docs: dict[str, str] | None = None) -> None:
        self.docs: dict[str, str] = dict(docs or {})
        self.calls: list[tuple] = []

    def fetch(self, url: str) -> tuple[str, str]:
        self.calls.append(("fetch", url))
        md = self.docs.get(url)
        if md is None:
            md = self.docs.get(url.split("#", 1)[0])
        if md is None:
            raise IntegrationError(f"spec: cannot fetch {url}", service="google")
        return md, hashlib.sha256(md.encode()).hexdigest()


class FakeChat:
    """ChatPort fake: records threads, posts and updates; returns ids `m1`, `m2`, …"""

    def __init__(self, store: Any = None) -> None:
        self.threads: dict[str, dict | None] = {}
        self.posts: list[dict] = []  # {"id", "key", "text", "card"}
        self.updates: list[dict] = []  # {"id", "text", "card"}
        self.calls: list[tuple] = []
        self.store = store

    def ensure_thread(self, key: str, origin: dict | None = None) -> None:
        self.calls.append(("ensure_thread", key))
        self.threads.setdefault(key, origin)

    def post(self, key: str | None, text_md: str, card: dict | None = None) -> str:
        msg_id = f"m{len(self.posts) + 1}"
        self.calls.append(("post", key, msg_id))
        self.posts.append({"id": msg_id, "key": key, "text": text_md, "card": card})
        if self.store is not None and key:
            self.store.record_event(key, "chat_post", data={"message_id": msg_id})
        return msg_id

    def update(self, message_id: str, text_md: str | None = None, card: dict | None = None) -> None:
        self.calls.append(("update", message_id))
        self.updates.append({"id": message_id, "text": text_md, "card": card})

    # ---------- test helpers ----------

    def texts(self, key: str | None = None) -> list[str]:
        return [p["text"] for p in self.posts if key is None or p["key"] == key]

    def cards(self, key: str | None = None) -> list[dict]:
        return [p["card"] for p in self.posts if p["card"] and (key is None or p["key"] == key)]
