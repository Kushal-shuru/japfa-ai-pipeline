"""GitLab client (`python-gitlab`). Implements `GitLabPort` with two identities (§9.2):

- **coder**: pushes (via git), opens / updates MRs, replies to threads.
- **reviewer**: inline comments, notes, resolves threads, approves.

Never mix them. Transient errors (429 / 5xx) are retried by python-gitlab.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, TypeVar
from urllib.parse import quote

import gitlab
from gitlab.exceptions import GitlabError

from pipeline.integrations.base import IntegrationError
from pipeline.logging import mask_text
from pipeline.markers import gitlab_marker, has_marker

T = TypeVar("T")
Role = Literal["coder", "reviewer"]


def _mr_dict(mr: Any) -> dict:
    a = mr.attributes if hasattr(mr, "attributes") else mr
    return {
        "iid": a.get("iid"),
        "web_url": a.get("web_url"),
        "state": a.get("state"),
        "title": a.get("title"),
        "description": a.get("description") or "",
        "source_branch": a.get("source_branch"),
        "target_branch": a.get("target_branch"),
        "sha": a.get("sha"),
    }


def unified_diff(entries: list[dict]) -> str:
    """GitLab `/diffs` entries → one unified diff text."""
    parts = []
    for d in entries:
        old, new = d.get("old_path"), d.get("new_path")
        body = d.get("diff") or ""
        head = [f"diff --git a/{old} b/{new}"]
        if d.get("new_file"):
            head.append("new file mode " + str(d.get("b_mode") or "100644"))
        elif d.get("deleted_file"):
            head.append("deleted file mode " + str(d.get("a_mode") or "100644"))
        elif d.get("renamed_file"):
            head += [f"rename from {old}", f"rename to {new}"]
        if not body.startswith("---"):
            a = "/dev/null" if d.get("new_file") else f"a/{old}"
            b = "/dev/null" if d.get("deleted_file") else f"b/{new}"
            head += [f"--- {a}", f"+++ {b}"]
        if d.get("too_large") or d.get("collapsed"):
            body = body or "(diff too large; not shown by GitLab)\n"
        parts.append("\n".join(head) + "\n" + body)
    return "".join(p if p.endswith("\n") else p + "\n" for p in parts)


class GitLabClient:
    def __init__(
        self,
        base_url: str,
        coder_token: str,
        reviewer_token: str,
        store: Any = None,
        *,
        coder: Any = None,
        reviewer: Any = None,
    ) -> None:
        self.store = store
        self.coder = coder or gitlab.Gitlab(
            base_url, private_token=coder_token, retry_transient_errors=True, timeout=60
        )
        """Coder identity: opens/updates MRs, replies to threads (pushes go through git)."""
        self.reviewer = reviewer or gitlab.Gitlab(
            base_url, private_token=reviewer_token, retry_transient_errors=True, timeout=60
        )
        """Reviewer identity: inline comments, notes, resolves threads, approves."""
        self._reviewer_username: str | None = None

    # ---------- helpers ----------

    @staticmethod
    def _call(what: str, fn: Callable[[], T]) -> T:
        try:
            return fn()
        except GitlabError as err:
            code = getattr(err, "response_code", None)
            detail = f" (HTTP {code})" if code else ""
            raise IntegrationError(
                f"gitlab: {what} failed{detail}: {err.error_message}", service="gitlab"
            ) from err

    def _gl(self, role: Role) -> Any:
        if role not in ("coder", "reviewer"):
            raise ValueError(f"unknown GitLab identity {role!r}")
        return self.coder if role == "coder" else self.reviewer

    def _mr(self, project: str, iid: int, role: Role = "coder") -> Any:
        gl = self._gl(role)
        return self._call(
            f"get MR {project}!{iid}",
            lambda: gl.projects.get(project, lazy=True).mergerequests.get(iid),
        )

    # ---------- MRs (coder) ----------

    def find_mr(self, project: str, source_branch: str) -> dict | None:
        """Open MR for the branch, else the most recent one in any state, else None."""
        proj = self.coder.projects.get(project, lazy=True)
        mrs = self._call(
            f"list MRs {project}",
            lambda: proj.mergerequests.list(
                source_branch=source_branch, order_by="created_at", sort="desc", get_all=True
            ),
        )
        if not mrs:
            return None
        opened = [m for m in mrs if m.attributes.get("state") == "opened"]
        return _mr_dict((opened or mrs)[0])

    def list_mrs(self, project: str, source_prefix: str) -> list[dict]:
        """MRs in any state whose source branch starts with `source_prefix` (demo-reset)."""
        proj = self.coder.projects.get(project, lazy=True)
        mrs = self._call(f"list MRs {project}",
                         lambda: proj.mergerequests.list(get_all=True, state="all"))  # fmt: skip
        return [
            _mr_dict(m)
            for m in mrs
            if str(m.attributes.get("source_branch") or "").startswith(source_prefix)
        ]

    def close_mr(self, project: str, iid: int) -> None:
        proj = self.coder.projects.get(project, lazy=True)
        self._call(f"close MR {project}!{iid}",
                   lambda: proj.mergerequests.update(iid, {"state_event": "close"}))  # fmt: skip

    def delete_branch(self, project: str, branch: str) -> None:
        """Delete a remote AI branch (a missing one is fine)."""
        proj = self.coder.projects.get(project, lazy=True)
        try:
            proj.branches.delete(branch)
        except GitlabError as err:
            if getattr(err, "response_code", None) != 404:
                what = f"gitlab: delete branch {project}:{branch} failed: {err.error_message}"
                raise IntegrationError(what, service="gitlab") from err

    def create_mr(self, project: str, source: str, target: str, title: str, body_md: str) -> dict:
        """Idempotent: returns the existing open MR for `source` if there is one."""
        existing = self.find_mr(project, source)
        if existing and existing["state"] == "opened":
            return existing
        proj = self.coder.projects.get(project, lazy=True)
        mr = self._call(
            f"create MR {project} {source}→{target}",
            lambda: proj.mergerequests.create(
                {
                    "source_branch": source,
                    "target_branch": target,
                    "title": title,
                    "description": mask_text(body_md),
                    "remove_source_branch": True,
                }
            ),
        )
        return _mr_dict(mr)

    def update_mr_description(self, project: str, iid: int, body_md: str) -> None:
        mr = self._mr(project, iid)
        body_md = mask_text(body_md)
        if (mr.attributes.get("description") or "") == body_md:
            return
        mr.description = body_md
        self._call(f"update MR {project}!{iid}", mr.save)

    def mr_state(self, project: str, iid: int) -> Literal["opened", "merged", "closed"]:
        state = self._mr(project, iid).attributes.get("state")
        return "opened" if state == "locked" else state

    def mr_diff(self, project: str, iid: int) -> str:
        path = f"/projects/{quote(project, safe='')}/merge_requests/{iid}/diffs"
        entries = self._call(
            f"diff MR {project}!{iid}", lambda: self.coder.http_list(path, get_all=True)
        )
        return unified_diff(list(entries))

    def mr_commits(self, project: str, iid: int) -> list[dict]:
        mr = self._mr(project, iid)
        commits = self._call(f"commits MR {project}!{iid}", lambda: list(mr.commits()))
        keys = ("id", "short_id", "title", "message", "author_name", "created_at")
        return [{k: c.attributes.get(k) for k in keys} for c in commits]

    # ---------- comments ----------

    def _existing_bodies(self, mr: Any) -> list[str]:
        discussions = self._call("list discussions", lambda: mr.discussions.list(get_all=True))
        return [n.get("body") or "" for d in discussions for n in d.attributes.get("notes", [])]

    def add_note(
        self, project: str, iid: int, body_md: str, marker: str, as_role: str = "reviewer"
    ) -> None:
        """General MR note as `as_role` (coder | reviewer). Skips if the marker exists."""
        mr = self._mr(project, iid, as_role)  # type: ignore[arg-type]
        if any(has_marker(b, marker) for b in self._existing_bodies(mr)):
            return
        body = mask_text(body_md) + gitlab_marker(marker)
        self._call(f"note MR {project}!{iid}", lambda: mr.notes.create({"body": body}))

    def add_inline(
        self, project: str, iid: int, path: str, line: int, body_md: str, marker: str
    ) -> None:
        """Reviewer: thread on `path:line` (new file). Without a usable position (`path` empty,
        `line` 0, or GitLab rejects it because the line is not in the diff) the thread is
        opened on the MR itself with `path:line` on top: still a resolvable thread, so the
        finding can be found by its marker later (P5)."""
        mr = self._mr(project, iid, "reviewer")
        if any(has_marker(b, marker) for b in self._existing_bodies(mr)):
            return
        refs = mr.attributes.get("diff_refs") or {}
        body = mask_text(body_md) + gitlab_marker(marker)
        if path and line and all(refs.get(k) for k in ("base_sha", "start_sha", "head_sha")):
            position = {
                "position_type": "text",
                "base_sha": refs["base_sha"],
                "start_sha": refs["start_sha"],
                "head_sha": refs["head_sha"],
                "old_path": path,
                "new_path": path,
                "new_line": line,
            }
            try:
                mr.discussions.create({"body": body, "position": position})
                return
            except GitlabError:
                pass  # fall back below
        where = f"**`{path}:{line}`**\n\n" if path and line else (f"**`{path}`**\n\n" if path
                                                                 else "")  # fmt: skip
        fallback = where + mask_text(body_md) + gitlab_marker(marker)
        self._call(f"thread MR {project}!{iid}", lambda: mr.discussions.create({"body": fallback}))

    def unresolved_threads(self, project: str, iid: int) -> list[dict]:
        """Resolvable, unresolved threads: {id, path, line, notes: [{id, author, body}]}."""
        mr = self._mr(project, iid, "reviewer")
        discussions = self._call(
            f"list discussions {project}!{iid}", lambda: mr.discussions.list(get_all=True)
        )
        out = []
        for d in discussions:
            notes = d.attributes.get("notes", [])
            if d.attributes.get("individual_note") or not notes:
                continue
            if not any(n.get("resolvable") for n in notes):
                continue
            if all(n.get("resolved") for n in notes if n.get("resolvable")):
                continue
            pos = notes[0].get("position") or {}
            out.append(
                {
                    "id": d.get_id() if hasattr(d, "get_id") else d.attributes.get("id"),
                    "path": pos.get("new_path"),
                    "line": pos.get("new_line"),
                    "notes": [
                        {
                            "id": n.get("id"),
                            "author": (n.get("author") or {}).get("username"),
                            "body": n.get("body") or "",
                        }
                        for n in notes
                    ],
                }
            )
        return out

    def reply(
        self, project: str, iid: int, discussion_id: str, body_md: str, as_role: str = "coder"
    ) -> None:
        """Reply in a thread: the coder (resolutions) or the reviewer ("Still open", P5)."""
        mr = self._mr(project, iid, as_role)  # type: ignore[arg-type]
        disc = mr.discussions.get(discussion_id, lazy=True)
        self._call(
            f"reply {project}!{iid} {discussion_id}",
            lambda: disc.notes.create({"body": mask_text(body_md)}),
        )

    def resolve(self, project: str, iid: int, discussion_id: str) -> None:
        """Reviewer resolves a thread (no-op if already resolved)."""
        mr = self._mr(project, iid, "reviewer")
        disc = self._call(
            f"get discussion {discussion_id}", lambda: mr.discussions.get(discussion_id)
        )
        notes = disc.attributes.get("notes", [])
        if notes and all(n.get("resolved") for n in notes if n.get("resolvable")):
            return
        disc.resolved = True
        self._call(f"resolve {project}!{iid} {discussion_id}", disc.save)

    def reviewer_username(self) -> str:
        if self._reviewer_username is None:
            self._call("reviewer auth", self.reviewer.auth)
            self._reviewer_username = self.reviewer.user.username
        return self._reviewer_username

    def approve(self, project: str, iid: int, sha: str | None = None) -> None:
        """Reviewer approves the MR at head `sha` (P9, audit F3): GitLab refuses (409) when the
        MR head moved since the review. An earlier approval by the reviewer is withdrawn first,
        so it never stands for a commit nobody reviewed."""
        mr = self._mr(project, iid, "reviewer")
        approvals = self._call(f"approvals {project}!{iid}", lambda: mr.approvals.get())
        approved_by = [
            (a.get("user") or {}).get("username")
            for a in approvals.attributes.get("approved_by", [])
        ]
        if self.reviewer_username() in approved_by:
            self._call(f"unapprove {project}!{iid}", mr.unapprove)
        self._call(f"approve {project}!{iid}", lambda: mr.approve(sha=sha))

    # ---------- checks (pipeline check) ----------

    def whoami(self, role: Role) -> str:
        gl = self._gl(role)
        self._call(f"{role} auth", gl.auth)
        return gl.user.username

    def access_level(self, project: str, role: Role) -> int:
        """Max of the identity's project/group access level (30 dev, 40 maintainer)."""
        gl = self._gl(role)
        proj = self._call(f"{role} get {project}", lambda: gl.projects.get(project))
        perms = proj.attributes.get("permissions") or {}
        levels = [
            (perms.get(k) or {}).get("access_level") or 0
            for k in ("project_access", "group_access")
        ]
        level = max(levels)
        if not level:  # access inherited through a shared or parent group isn't in `permissions`
            try:
                uid = gl.user.id if getattr(gl, "user", None) else (gl.auth() or gl.user.id)
                level = gl.projects.get(project, lazy=True).members_all.get(uid).access_level
            except Exception:  # noqa: BLE001 - keep the answer from `permissions`
                level = 0
        return level

    def branch_exists(self, project: str, branch: str) -> bool:
        from gitlab.exceptions import GitlabGetError

        proj = self.coder.projects.get(project, lazy=True)
        try:
            proj.branches.get(branch)
            return True
        except GitlabGetError as err:
            if getattr(err, "response_code", None) == 404:
                return False
            raise IntegrationError(
                f"gitlab: branch {project}:{branch}: {err}", service="gitlab"
            ) from err
