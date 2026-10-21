"""Multi-repo workspaces: base clones + one git worktree per repo per ticket.

$PIPELINE_HOME/repos/<repo>/            base clone (origin = GitLab over HTTPS)
$PIPELINE_HOME/workspaces/<KEY>/        ticket dir (.agent/ ticket.md, spec.md, …)
$PIPELINE_HOME/workspaces/<KEY>/<repo>/ worktree on branch ai/<KEY>-<slug>

The coder token is never written to disk: network commands get it through GIT_ASKPASS
from an environment variable.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from pipeline.config import PipelineConfig, RepoConfig
from pipeline.integrations.base import IntegrationError
from pipeline.safe_env import without_credentials
from pipeline.schemas import TicketContext

MAX_BRANCH_LEN = 50
PROTECTED_BRANCHES = frozenset({"main", "master", "develop"})
AGENT_DIR = ".agent"
_TOKEN_ENV = "PIPELINE_GIT_TOKEN"
_ASKPASS = """#!/bin/sh
case "$1" in
  Username*) echo "oauth2" ;;
  *) echo "$PIPELINE_GIT_TOKEN" ;;
esac
"""


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def branch_name(key: str, summary: str, suffix: str = "") -> str:
    """`ai/DEMO-12-short-slug[-qa1]`, at most 50 chars; same for every repo of a ticket."""
    prefix = f"ai/{key}"
    tail = f"-{slugify(suffix)}" if suffix else ""
    room = MAX_BRANCH_LEN - len(prefix) - len(tail) - 1
    slug = slugify(summary)
    if len(slug) > room:
        cut = slug[:room]
        slug = cut.rsplit("-", 1)[0] if "-" in cut and slug[room : room + 1] != "-" else cut
        slug = slug.strip("-")
    name = f"{prefix}-{slug}{tail}" if slug and room > 0 else f"{prefix}{tail}"
    return name[:MAX_BRANCH_LEN]


GIT_ERROR_LINES = 15


def git_error_tail(stdout: str | None, stderr: str | None, lines: int = GIT_ERROR_LINES) -> str:
    """Last lines of a failed git command's stdout + stderr. Hooks (husky pre-push) print
    their failure to stdout while git's stderr only says "failed to push some refs"."""
    parts = [(stdout or "").strip(), (stderr or "").strip()]
    text = [line for line in "\n".join(p for p in parts if p).splitlines() if line.strip()]
    tail = "\n".join(text[-lines:])
    return f"…\n{tail}" if len(text) > lines else tail


def parse_author(author: str) -> tuple[str, str]:
    m = re.match(r"^\s*(.+?)\s*<([^>]+)>\s*$", author)
    if not m:
        raise ValueError(f"author must look like 'Name <email>', got {author!r}")
    return m.group(1), m.group(2)


_LOCK_FILES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml")


def lock_digest(path: Path) -> str | None:
    """Short hash of the dependency lock file (+ package.json) in `path`."""
    lock = next((path / n for n in _LOCK_FILES if (path / n).is_file()), None)
    if lock is None:
        return None
    h = hashlib.sha256(lock.read_bytes())
    if (path / "package.json").is_file():
        h.update((path / "package.json").read_bytes())
    return h.hexdigest()[:16]


def clone_tree(src: Path, dst: Path) -> None:
    """Copy a directory tree, copy-on-write when possible (APFS `cp -c`, Linux reflink);
    symlinks (node_modules/.bin) are kept as links."""
    for cmd in (["cp", "-c", "-R", str(src), str(dst)],
                ["cp", "-R", "--reflink=auto", str(src), str(dst)]):  # fmt: skip
        if subprocess.run(cmd, capture_output=True, check=False).returncode == 0:
            return
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(src, dst, symlinks=True)


class Workspaces:
    def __init__(
        self,
        home: Path,
        config: PipelineConfig,
        gitlab_base_url: str = "https://gitlab.com",
        coder_token: str | None = None,
        *,
        remote_url: Callable[[RepoConfig], str] | None = None,
    ) -> None:
        self.home = home
        self.config = config
        self.gitlab_base_url = gitlab_base_url.rstrip("/")
        self._token = coder_token
        self._remote_url = remote_url or self._https_url

    # ---------- git plumbing ----------

    def _https_url(self, repo: RepoConfig) -> str:
        return f"{self.gitlab_base_url}/{quote(repo.gitlab_project)}.git"

    def _askpass(self) -> Path:
        path = self.home / "bin" / "git-askpass.sh"
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_ASKPASS)
            path.chmod(0o700)
        return path

    def _git(self, cwd: Path, *args: str, network: bool = False) -> str:
        # P9 (audit F4): no pipeline credentials in git's environment, and no repo hooks: the
        # orchestrator's own git never runs code from the repo (the checks run in verify)
        env = {**without_credentials(), "GIT_TERMINAL_PROMPT": "0"}
        if network and self._token:
            env.update({"GIT_ASKPASS": str(self._askpass()), _TOKEN_ENV: self._token})
        proc = subprocess.run(
            ["git", "-c", f"core.hooksPath={os.devnull}", *args], cwd=cwd, env=env,
            capture_output=True, text=True, check=False,
        )  # fmt: skip
        if proc.returncode != 0:
            msg = git_error_tail(proc.stdout, proc.stderr)
            if self._token:
                msg = msg.replace(self._token, "***")
            raise IntegrationError(
                f"git {' '.join(args[:2])} failed in {cwd}: {msg}", service="git"
            )
        return proc.stdout

    # ---------- paths ----------

    def base_clone(self, repo: str) -> Path:
        return self.home / "repos" / repo

    def ticket_dir(self, key: str) -> Path:
        return self.home / "workspaces" / key

    def worktree_path(self, key: str, repo: str) -> Path:
        return self.ticket_dir(key) / repo

    # ---------- clones & worktrees ----------

    def ensure_clone(self, repo: str) -> Path:
        """Clone once (HTTPS with the coder token), then fetch on every call."""
        cfg = self.config.repo(repo)
        path = self.base_clone(repo)
        if not (path / ".git").exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            self._git(
                path.parent, "clone", "--no-checkout", self._remote_url(cfg), repo, network=True
            )
        self._git(path, "fetch", "origin", "--prune", network=True)
        return path

    def _branch_exists(self, base: Path, ref: str) -> bool:
        try:
            self._git(base, "rev-parse", "--verify", "--quiet", ref)
            return True
        except IntegrationError:
            return False

    def create_worktree(self, key: str, repo: str, branch: str) -> Path:
        """Worktree for `repo` on `branch`, from origin/<default>. Reuses an existing one."""
        cfg = self.config.repo(repo)
        path = self.worktree_path(key, repo)
        if (path / ".git").exists():
            current = self._git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
            if current != branch:
                raise IntegrationError(
                    f"worktree {path} is on {current!r}, expected {branch!r}", service="git"
                )
            return path
        base = self.ensure_clone(repo)
        self._git(base, "worktree", "prune")
        path.parent.mkdir(parents=True, exist_ok=True)
        if self._branch_exists(base, f"refs/heads/{branch}"):
            self._git(base, "worktree", "add", str(path), branch)
        elif self._branch_exists(base, f"refs/remotes/origin/{branch}"):
            self._git(
                base, "worktree", "add", "--track", "-b", branch, str(path), f"origin/{branch}"
            )
        else:
            start = f"origin/{cfg.default_branch}"
            if not self._branch_exists(base, f"refs/remotes/{start}"):
                raise IntegrationError(f"{repo}: remote branch {start} not found", service="git")
            self._git(base, "worktree", "add", "--no-track", "-b", branch, str(path), start)
        return path

    def switch_worktree(self, key: str, repo: str, branch: str) -> Path:
        """A fresh worktree for `repo` on a new `branch` from the latest origin/<default> (a QA
        round, D126): the old worktree (its branch is merged) is removed first. Local branches
        are kept."""
        path = self.worktree_path(key, repo)
        if (path / ".git").exists():
            current = self._git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
            if current == branch:
                return path
            base = self.base_clone(repo)
            self._git(base, "worktree", "remove", "--force", str(path))
            self._git(base, "worktree", "prune")
        return self.create_worktree(key, repo, branch)

    # ---------- node_modules cache (K19, Phase 8 4.3) ----------

    def deps_cache(self, repo: str, path: Path) -> Path | None:
        """Cached `node_modules` for the lock file in `path` (None: no lock file)."""
        digest = lock_digest(path)
        return self.home / "deps_cache" / repo / digest / "node_modules" if digest else None

    def seed_node_modules(self, key: str, repo: str) -> bool:
        """Give a worktree without `node_modules` a copy of the cached one for its lock file
        (copy-on-write where the file system can: seconds instead of an `npm ci`)."""
        wt = self.worktree_path(key, repo)
        cache = self.deps_cache(repo, wt)
        if (wt / "node_modules").exists() or cache is None or not cache.is_dir():
            return False
        clone_tree(cache, wt / "node_modules")
        return True

    def save_node_modules(self, repo: str, path: Path) -> bool:
        """Keep `path/node_modules` (just installed from its lock file) for later worktrees."""
        cache = self.deps_cache(repo, path)
        if cache is None or cache.exists() or not (path / "node_modules").is_dir():
            return False
        tmp = cache.parent / f"tmp-{os.getpid()}"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        clone_tree(path / "node_modules", tmp / "node_modules")
        with contextlib.suppress(OSError):  # another run saved it first
            (tmp / "node_modules").rename(cache)
        shutil.rmtree(tmp, ignore_errors=True)
        return cache.is_dir()

    def warm_checkout(self, repo: str) -> Path:
        """A detached worktree of origin/<default> under `$PIPELINE_HOME/warm/` (for
        `pipeline warm`); `drop_warm_checkout` removes it."""
        cfg = self.config.repo(repo)
        base = self.ensure_clone(repo)
        path = self.home / "warm" / repo
        if path.exists():
            self.drop_warm_checkout(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._git(base, "worktree", "add", "--detach", str(path), f"origin/{cfg.default_branch}")
        return path

    def drop_warm_checkout(self, repo: str) -> None:
        path = self.home / "warm" / repo
        base = self.base_clone(repo)
        if (path / ".git").is_file() and (base / ".git").exists():
            self._git(base, "worktree", "remove", "--force", str(path))
        shutil.rmtree(path, ignore_errors=True)
        if (base / ".git").exists():
            self._git(base, "worktree", "prune")

    def remove_ticket_workspace(self, key: str) -> None:
        """Remove all worktrees of a ticket and its directory (local branches are kept)."""
        tdir = self.ticket_dir(key)
        if not tdir.exists():
            return
        for child in tdir.iterdir():
            base = self.base_clone(child.name)
            if (child / ".git").is_file() and (base / ".git").exists():
                self._git(base, "worktree", "remove", "--force", str(child))
        shutil.rmtree(tdir, ignore_errors=True)
        for repo in self.config.repo_names:
            base = self.base_clone(repo)
            if (base / ".git").exists():
                self._git(base, "worktree", "prune")

    def delete_local_branches(self, repo: str, prefix: str) -> list[str]:
        """Delete the base clone's local branches starting with `prefix` (demo-reset)."""
        base = self.base_clone(repo)
        if not (base / ".git").exists() or not prefix.startswith("ai/"):
            return []
        out = self._git(base, "for-each-ref", "--format=%(refname:short)", f"refs/heads/{prefix}*")
        names = [n for n in out.split() if n.startswith(prefix)]
        for name in names:
            self._git(base, "branch", "-D", name)
        return names

    def set_identity(self, repo: str, author: str) -> None:
        """Git author for commits in every worktree of `repo` (set on the base clone)."""
        base = self.base_clone(repo)
        if not (base / ".git").exists():
            return
        name, email = parse_author(author)
        self._git(base, "config", "user.name", name)
        self._git(base, "config", "user.email", email)

    # ---------- agent files ----------

    def write_ticket_files(self, ctx: TicketContext) -> Path:
        """`workspaces/<KEY>/.agent/ticket.md` and `spec.md`."""
        agent = self.ticket_dir(ctx.key) / AGENT_DIR
        agent.mkdir(parents=True, exist_ok=True)
        ac = "\n".join(f"- {a}" for a in ctx.acceptance_criteria) or "- (none)"
        repos = "\n".join(
            f"- {r} ({self.config.repo(r).role})" if r in self.config.repo_names else f"- {r}"
            for r in ctx.repos
        )
        ticket = (
            f"# {ctx.key}: {ctx.summary}\n\n"
            f"## Repos (coding order)\n\n{repos}\n\n"
            f"## Acceptance criteria\n\n{ac}\n\n"
            f"## Spec links\n\n"
            + ("\n".join(f"- {u}" for u in ctx.spec_urls) or "- (none)")
            + (f"\n\n## Epic\n\n{ctx.epic_key}" if ctx.epic_key else "")
            + f"\n\n## Description\n\n{ctx.description_md.strip()}\n"
        )
        (agent / "ticket.md").write_text(ticket)
        (agent / "spec.md").write_text(ctx.spec_md.strip() + "\n")
        return agent

    def ensure_env(self, key: str, repo: str) -> str:
        """Create `<worktree>/.env` for tests/tools (D69): from `repos[].env_file`, else the
        repo's `.env.example`. Only if git ignores `.env` (so it can never be committed) and
        none exists. Returns what happened, for logs/messages."""
        wt = self.worktree_path(key, repo)
        target = wt / ".env"
        if target.exists():
            return "exists"
        configured = self.config.repo(repo).env_file
        if configured:
            source = Path(
                os.path.expandvars(configured.replace("$PIPELINE_HOME", str(self.home)))
            ).expanduser()
            if not source.is_file():
                return f"skipped: env_file {source} not found"
        else:
            source = wt / ".env.example"
            if not source.is_file():
                return "none: no .env.example"
        ignored = subprocess.run(
            ["git", "check-ignore", "-q", ".env"], cwd=wt, capture_output=True, check=False
        )
        if ignored.returncode != 0:
            return "skipped: .env is not gitignored in this repo"
        shutil.copyfile(source, target)
        return f"created from {source.name if not configured else source}"

    def write_repo_file(self, key: str, repo: str, name: str, content: str) -> Path:
        """`workspaces/<KEY>/<repo>/.agent/<name>`."""
        if "/" in name or name.startswith("."):
            raise ValueError(f"bad agent file name {name!r}")
        agent = self.worktree_path(key, repo) / AGENT_DIR
        agent.mkdir(parents=True, exist_ok=True)
        path = agent / name
        path.write_text(content)
        return path

    # ---------- commit / push / diff ----------

    def changed_files(self, path: Path) -> list[str]:
        """Every path with uncommitted changes (staged or not, untracked, deleted, renamed),
        relative to the worktree; `.agent/` left out."""
        out = self._git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        parts = out.split("\0")
        files: list[str] = []
        i = 0
        while i < len(parts):
            entry = parts[i]
            i += 1
            if len(entry) < 4:
                continue
            if entry[0] in "RC":  # a rename / copy: the original path follows
                i += 1
            name = entry[3:]
            if not name.startswith(f"{AGENT_DIR}/"):
                files.append(name)
        return files

    def commit_plan(self, path: Path, plan: list[tuple[str, list[str]]], author: str,
                    message_for: Callable[[str], str] | None = None) -> list[str]:  # fmt: skip
        """The coder's commit plan as small commits, in order (D169): each entry stages only
        its own changed files (a directory takes what changed under it) and commits without
        hooks (verify runs every check before the push). A file named twice goes in its first
        commit; entries with nothing changed are skipped. `message_for(message)` normalises
        the message. Returns the new shas; what's left is for `commit_all`.

        The index is reset first (the working tree is untouched): a coder's own `git add` / `git
        rm` would otherwise land in the wrong commit, and a path it removed with `git rm` is in
        neither the tree nor the index, so `git add` on it fails (DEMO-10)."""
        name, email = parse_author(author)
        self._git(path, "reset", "-q")
        left = self.changed_files(path)
        shas: list[str] = []
        for message, wanted in plan:
            prefixes = [w.strip().lstrip("./").rstrip("/") for w in wanted]
            files = [f for f in left if any(f == p or f.startswith(f"{p}/") for p in prefixes if p)]
            if not files:
                continue
            self._git(path, "add", "-A", "--", *files)
            if not self._git(path, "diff", "--cached", "--name-only").strip():
                continue
            text = message_for(message) if message_for else message
            self._git(path, "-c", f"user.name={name}", "-c", f"user.email={email}",
                      "commit", "--no-verify", "-m", text)  # fmt: skip
            shas.append(self._git(path, "rev-parse", "HEAD").strip())
            left = [f for f in left if f not in files]
        return shas

    def commit_all(self, path: Path, message: str, author: str) -> str | None:
        """Stage everything except `.agent/` and commit. Returns the sha, or None if clean."""
        name, email = parse_author(author)
        self._git(path, "add", "-A")
        self._git(path, "reset", "-q", "--", AGENT_DIR)  # in case .agent/ isn't gitignored
        if not self._git(path, "diff", "--cached", "--name-only").strip():
            return None
        self._git(
            path,
            "-c", f"user.name={name}",
            "-c", f"user.email={email}",
            "commit", "--no-verify", "-m", message,
        )  # fmt: skip
        return self._git(path, "rev-parse", "HEAD").strip()

    def refresh(self, path: Path, branch: str) -> bool:
        """Bring the worktree up to date with `origin/<branch>` when someone pushed to the MR
        branch in GitLab (K35): fast-forward only. True if it moved. Local commits that aren't
        pushed yet are kept; a branch that diverged raises IntegrationError."""
        self._git(path, "fetch", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}",
                  network=True)  # fmt: skip
        before = self._git(path, "rev-parse", "HEAD").strip()
        self._git(path, "merge", "--ff-only", "-q", f"origin/{branch}")
        return self._git(path, "rev-parse", "HEAD").strip() != before

    def has_commit(self, path: Path, sha: str) -> bool:
        """Is commit `sha` part of the worktree's HEAD history? (False for unknown objects.)"""
        if not sha:
            return False
        try:
            self._git(path, "merge-base", "--is-ancestor", sha, "HEAD")
        except IntegrationError:
            return False
        return True

    # ---------- rebase before every push (D125) ----------

    def head(self, path: Path) -> str:
        return self._git(path, "rev-parse", "HEAD").strip()

    def current_branch(self, path: Path) -> str | None:
        """The worktree's branch (None outside a branch or a repo)."""
        try:
            name = self._git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
        except IntegrationError:
            return None
        return None if name == "HEAD" else name

    def changed_between(self, path: Path, old: str, new: str) -> list[str] | None:
        """Files changed from commit `old` to `new` (None when `old` is unknown here, e.g. after
        a rebase rewrote it: then everything counts as changed)."""
        try:
            out = self._git(path, "diff", "--name-only", f"{old}..{new}")
        except IntegrationError:
            return None
        return [line for line in out.splitlines() if line.strip()]

    def remote_head(self, path: Path, branch: str) -> str:
        """The MR branch's sha on the remote ('' if it isn't pushed): what GitLab's MR head is."""
        remote = self._git(path, "ls-remote", "origin", f"refs/heads/{branch}", network=True)
        return remote.split()[0] if remote.strip() else ""

    def fetch_for_sync(self, path: Path, branch: str, default_branch: str) -> str:
        """Fetch `origin/<default>` and the MR branch. Returns the MR branch's remote sha ('' if
        it isn't pushed yet): the lease for the push that follows."""
        self._git(path, "fetch", "origin",
                  f"refs/heads/{default_branch}:refs/remotes/origin/{default_branch}",
                  network=True)  # fmt: skip
        sha = self.remote_head(path, branch)
        if sha:
            self._git(path, "fetch", "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}",
                      network=True)  # fmt: skip
        return sha

    def needs_rebase(self, path: Path, onto: str) -> bool:
        """True when `onto` (e.g. `origin/develop`) has commits HEAD doesn't contain."""
        return not self.has_commit(path, self._git(path, "rev-parse", onto).strip())

    def conflicted_files(self, path: Path) -> list[str]:
        out = self._git(path, "diff", "--name-only", "--diff-filter=U")
        return sorted(p for p in out.splitlines() if p.strip())

    def rebasing(self, path: Path) -> bool:
        git_dir = Path(self._git(path, "rev-parse", "--git-dir").strip())
        git_dir = git_dir if git_dir.is_absolute() else path / git_dir
        return (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists()

    def rebase_onto(self, path: Path, onto: str) -> list[str]:
        """`git rebase <onto>`. [] = done; else the conflicted files, with the rebase left in
        progress for someone to resolve (then `continue_rebase`, or `abort_rebase`)."""
        try:
            self._git(path, "-c", "core.editor=true", "rebase", onto)
            return []
        except IntegrationError:
            if not self.rebasing(path):
                raise
            return self.conflicted_files(path) or ["(unknown file)"]

    def continue_rebase(self, path: Path) -> list[str]:
        """After the conflicts are resolved and staged: continue. [] = done; else the next
        commit's conflicted files."""
        try:
            self._git(path, "-c", "core.editor=true", "rebase", "--continue")
            return []
        except IntegrationError:
            if not self.rebasing(path):
                raise
            return self.conflicted_files(path) or ["(unknown file)"]

    def abort_rebase(self, path: Path) -> None:
        if self.rebasing(path):
            self._git(path, "rebase", "--abort")

    def push(self, path: Path, branch: str, default_branch: str | None = None,
             lease: str | None = None) -> None:  # fmt: skip
        """Push `branch` to origin. Never a default/protected branch (§9.1).

        `--no-verify`: the orchestrator has already re-verified the commit (§9.5, verify);
        repo pre-push hooks (husky `coverage:check`) would only repeat that and need local
        services such as Redis (D73). Coder commits still run the repo's pre-commit hook.

        `lease` (D125): the branch's remote sha seen before the rebase; a rebased branch is
        pushed with `--force-with-lease` pinned to it, so commits pushed in the meantime by
        someone else are never overwritten (the push fails instead)."""
        protected = set(PROTECTED_BRANCHES)
        if default_branch:
            protected.add(default_branch)
        if branch in protected:
            raise IntegrationError(
                f"refusing to push to protected branch {branch!r}", service="git"
            )
        current = self._git(path, "rev-parse", "--abbrev-ref", "HEAD").strip()
        if current != branch:
            raise IntegrationError(
                f"refusing to push: worktree is on {current!r}, not {branch!r}", service="git"
            )
        force = [f"--force-with-lease=refs/heads/{branch}:{lease}"] if lease else []
        self._git(
            path, "push", "--no-verify", *force, "-u", "origin", f"{branch}:refs/heads/{branch}",
            network=True,
        )  # fmt: skip

    def diff_vs_default(self, path: Path, default_branch: str, numstat: bool = False) -> str:
        """Unified diff (or `--numstat`) of the worktree (committed + uncommitted tracked
        changes) against its merge base with origin/<default>. Local only."""
        base = self._git(path, "merge-base", f"origin/{default_branch}", "HEAD").strip()
        args = ["diff", "--no-color", "--no-ext-diff"]
        if numstat:
            args.append("--numstat")
        return self._git(path, *args, base, "--", ".", f":(exclude){AGENT_DIR}")

    def existing_on_default(self, path: Path, default_branch: str, files: list[str]) -> list[str]:
        """Those of `files` that exist on origin/<default> (i.e. were edited, not added)."""
        if not files:
            return []
        out = self._git(path, "ls-tree", "-r", "--name-only", f"origin/{default_branch}", "--",
                        *files)  # fmt: skip
        return sorted(set(out.split()) & set(files))

    def restore_from_default(self, path: Path, default_branch: str, files: list[str]) -> list[str]:
        """Put `files` back to what origin/<default> has (edits and deletions undone), in the
        working tree and the index. Only files that exist there; returns the ones restored."""
        files = self.existing_on_default(path, default_branch, files)
        if files:
            self._git(path, "checkout", f"origin/{default_branch}", "--", *files)
        return files

    def diff_between(self, path: Path, old: str, new: str = "HEAD") -> str:
        """Unified diff from commit `old` to `new` (what changed since a review), `.agent/` left
        out. '' when `old` isn't known to this worktree."""
        try:
            return self._git(path, "diff", "--no-color", "--no-ext-diff", old, new, "--", ".",
                             f":(exclude){AGENT_DIR}")  # fmt: skip
        except IntegrationError:
            return ""

    def changed_paths(self, path: Path, default_branch: str | None = None) -> dict[str, list[str]]:
        """{"vs_head": uncommitted incl. untracked, "vs_default": everything vs origin/<default>}.
        `.agent/` is excluded from both."""
        status = self._git(path, "status", "--porcelain", "--untracked-files=all")
        vs_head: list[str] = []
        for line in status.splitlines():
            entry = line[3:]
            if " -> " in entry:
                entry = entry.split(" -> ", 1)[1]
            entry = entry.strip('"')
            if not entry.startswith(f"{AGENT_DIR}/"):
                vs_head.append(entry)
        result = {"vs_head": sorted(set(vs_head)), "vs_default": []}
        if default_branch:
            committed = self._git(
                path, "diff", "--name-only", f"origin/{default_branch}...HEAD"
            ).split()
            both = {p for p in committed if not p.startswith(f"{AGENT_DIR}/")} | set(vs_head)
            result["vs_default"] = sorted(both)
        return result
