"""Deterministic DevOps facts per repo (Phase 6 §1): what a deployment of this branch needs.

`collect(repo_cfg, worktree, base_ref)` reads the branch diff against its merge base with
`base_ref` plus the base and head versions of `.env.example` / `package.json`, and hands them
to the pure `facts_from_diff()` (tested with fixture diffs):

- env: `.env.example` base vs head → added / removed; `process.env.X` / `import.meta.env.X` in
  added lines but declared nowhere → undeclared; frontend `VITE_*` / `VUE_APP_*` → build-time
- gateway: new `*_SERVICE_URL` / internal-token style vars → "gateway → new service dependency"
- migrations under `paths.migrations` (or any `migrations/` folder) and, after them, seeders under
  `paths.seeders` (or any `seeders/` folder; P9, DEMO-9: a seeder was missing from the note),
  both in `migration_files` (database scripts DevOps runs, in that order); infra files (Dockerfiles,
  compose, CI, k8s, helm, nginx, pm2 ecosystem, Procfile); new or changed runtime dependencies
- notes: new queues / workers / cron jobs / outbound hosts in added lines

`needs_devops()` decides from these facts only (never from the deployment agent).
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any

from pipeline.schemas import DevopsFacts

ENV_FILE = ".env.example"
PACKAGE_JSON = "package.json"
RUNTIME_DEP_FIELDS = ("dependencies", "optionalDependencies", "peerDependencies")
TEST_ROOTS = frozenset({"test", "tests", "__tests__", "spec"})  # a test of a migration isn't one
IGNORED_ENV = frozenset({"NODE_ENV", "PORT", "CI", "HOME", "PATH", "TZ"})
BUILD_TIME_PREFIXES = ("VITE_", "VUE_APP_")
# note prefixes that make a DevOps step necessary (queue / cron / worker)
DEVOPS_NOTE_KINDS = ("Queue:", "Cron:", "Worker:")

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
_ENV_USE = re.compile(
    r"(?:process\.env|import\.meta\.env)"
    r"(?:\.([A-Za-z_][A-Za-z0-9_]*)|\[\s*['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\s*\])"
)
_ENV_DESTRUCTURE = re.compile(r"\{([^}]*)\}\s*=\s*process\.env\b")
_SERVICE_DEP_VAR = re.compile(r"(_SERVICE_URL|_SERVICE_HOST|_BASE_URL|INTERNAL_TOKEN|"
                              r"_SERVICE_TOKEN|_CLIENT_SECRET|_API_KEY)$")  # fmt: skip
_INFRA = re.compile(
    r"(^|/)(Dockerfile[^/]*|docker-compose[^/]*\.ya?ml|compose[^/]*\.ya?ml|\.gitlab-ci\.yml|"
    r"nginx[^/]*|ecosystem\.config\.[^/]+|Procfile|\.dockerignore)$"
    r"|(^|/)(k8s|kubernetes|helm|charts|deploy|\.gitlab)/"
)
_QUEUE = re.compile(r"\bnew\s+(Queue|Worker|QueueScheduler)\s*\(|\b(amqplib|kafkajs|bullmq?)\b"
                    r"|\.(assertQueue|consume|sendToQueue)\s*\(")  # fmt: skip
_CRON = re.compile(r"\bcron\.schedule\s*\(|\bnew\s+CronJob\s*\(|\bnode-cron\b|\bschedule"
                   r"\.scheduleJob\s*\(|\brepeat\s*:\s*\{\s*(cron|pattern)\b")  # fmt: skip
_URL = re.compile(r"https?://([A-Za-z0-9.-]+\.[A-Za-z]{2,})(?::\d+)?")
_LOCAL_HOSTS = (
    "localhost",
    "127.0.0.1",
    "example.com",
    "example.org",
    "example.test",
    # identifiers, not hosts the code calls: JSON Schema `$schema`, XML namespaces
    "json-schema.org",
    "w3.org",
    "schema.org",
)
_NOT_A_CALL = re.compile(r"^\s*(//|/?\*|#)|[\"']?\$(schema|id)[\"']?\s*:")  # comments, $schema


# ---------- diff parsing ----------


def split_diff(diff: str) -> dict[str, dict[str, Any]]:
    """Unified diff → {path: {"added": [lines], "status": "added"|"deleted"|"modified"}}."""
    files: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.+?) b/(.+)$", line)
            path = m.group(2) if m else line.split(" b/", 1)[-1]
            current = files.setdefault(path, {"added": [], "status": "modified"})
            continue
        if current is None:
            continue
        if line.startswith("new file mode"):
            current["status"] = "added"
        elif line.startswith("deleted file mode"):
            current["status"] = "deleted"
        elif line.startswith("+") and not line.startswith("+++"):
            current["added"].append(line[1:])
    return files


def env_keys(text: str | None) -> list[str]:
    keys: list[str] = []
    for line in (text or "").splitlines():
        m = _ENV_LINE.match(line)
        if m and m.group(1) not in keys:
            keys.append(m.group(1))
    return keys


def env_uses(lines: list[str]) -> list[str]:
    found: list[str] = []
    for line in lines:
        names = [a or b for a, b in _ENV_USE.findall(line)]
        for group in _ENV_DESTRUCTURE.findall(line):
            names += [n.split(":")[0].split("=")[0].strip() for n in group.split(",")]
        for n in names:
            if n and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n) and n not in found:
                found.append(n)
    return found


def runtime_deps(package_json: str | None) -> dict[str, str]:
    try:
        data = json.loads(package_json or "{}")
    except json.JSONDecodeError:
        return {}
    out: dict[str, str] = {}
    for field in RUNTIME_DEP_FIELDS:
        out.update({k: str(v) for k, v in (data.get(field) or {}).items()})
    return out


def _is_local(host: str) -> bool:
    return any(host == x or host.endswith("." + x) for x in _LOCAL_HOSTS)


def _in_dir(path: str, configured: str | None, folder: str) -> bool:
    if configured and path.startswith(configured.rstrip("/") + "/"):
        return True
    parts = PurePosixPath(path).parts
    return folder in parts[:-1] and parts[0] not in TEST_ROOTS


def _is_migration(path: str, migrations_dir: str | None) -> bool:
    return _in_dir(path, migrations_dir, "migrations")


def is_seeder(path: str, seeders_dir: str | None = None) -> bool:
    return _in_dir(path, seeders_dir, "seeders")


# ---------- facts ----------


def facts_from_diff(
    repo: str,
    role: str,
    diff: str,
    env_base: str | None = None,
    env_head: str | None = None,
    package_base: str | None = None,
    package_head: str | None = None,
    migrations_dir: str | None = None,
    seeders_dir: str | None = None,
) -> DevopsFacts:
    files = split_diff(diff)
    base_keys, head_keys = env_keys(env_base), env_keys(env_head)
    new_env = [k for k in head_keys if k not in base_keys]
    removed_env = [k for k in base_keys if k not in head_keys]

    added_code: list[str] = []
    for path, f in files.items():
        name = PurePosixPath(path).name
        if name.startswith(".env") or f["status"] == "deleted":
            continue
        if path.endswith((".md", ".json", ".lock")) or "/test" in f"/{path}":
            continue  # docs, lockfiles and tests don't run in production
        added_code += f["added"]
    declared = set(head_keys) | set(base_keys)
    undeclared = [n for n in env_uses(added_code) if n not in declared and n not in IGNORED_ENV]

    build_time: list[str] = []
    if role == "frontend":
        build_time = [n for n in [*new_env, *undeclared] if n.startswith(BUILD_TIME_PREFIXES)]

    kept = [p for p, f in files.items() if f["status"] != "deleted"]
    migrations = sorted(p for p in kept if _is_migration(p, migrations_dir))
    seeders = sorted(p for p in kept if is_seeder(p, seeders_dir) and p not in migrations)
    infra = sorted(p for p in files if _INFRA.search(p))

    base_deps, head_deps = runtime_deps(package_base), runtime_deps(package_head)
    new_deps = [f"{n}@{v}" for n, v in sorted(head_deps.items()) if base_deps.get(n) != v]

    notes: list[str] = []
    if seeders:
        notes.append("Seeder: " + ", ".join(PurePosixPath(p).name for p in seeders)
                     + " (data the code needs; run after the migrations)")  # fmt: skip
    if role == "gateway":
        service_vars = [n for n in [*new_env, *undeclared] if _SERVICE_DEP_VAR.search(n)]
        if service_vars:
            notes.append("Gateway → new service dependency: " + ", ".join(service_vars))
    for path, f in sorted(files.items()):
        if f["status"] == "deleted" or path.endswith(".md") or "/test" in f"/{path}":
            continue
        text = "\n".join(f["added"])
        calls = "\n".join(line for line in f["added"] if not _NOT_A_CALL.search(line))
        if f["status"] == "added" and re.search(r"(^|/)(workers?|jobs|consumers)/", path):
            notes.append(f"Worker: new file {path}")
        if _QUEUE.search(text):
            notes.append(f"Queue: queue code added in {path}")
        if _CRON.search(text):
            notes.append(f"Cron: scheduled job added in {path}")
        hosts = sorted({h for h in _URL.findall(calls) if not _is_local(h)})
        if hosts:
            notes.append(f"Outbound host: {', '.join(hosts)} (in {path})")
    return DevopsFacts(
        repo=repo,
        new_env_vars=new_env,
        removed_env_vars=removed_env,
        build_time_env_vars=build_time,
        undeclared_env_vars=undeclared,
        migration_files=migrations + seeders,  # database scripts, migrations first
        infra_files_changed=infra,
        new_dependencies=new_deps,
        notes=notes,
    )


def _git(cwd: Path, *args: str) -> str | None:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    return proc.stdout if proc.returncode == 0 else None


def collect(repo_cfg: Any, worktree: Path, base_ref: str) -> DevopsFacts:
    """Facts for the branch checked out in `worktree` vs its merge base with `base_ref`
    (e.g. `origin/develop`). `.agent/` is ignored."""
    worktree = Path(worktree)
    base = (_git(worktree, "merge-base", base_ref, "HEAD") or base_ref).strip()
    diff = _git(worktree, "diff", "--no-color", "--no-ext-diff", f"{base}...HEAD", "--", ".",
                ":(exclude).agent") or ""  # fmt: skip

    def at_base(name: str) -> str | None:
        return _git(worktree, "show", f"{base}:{name}")

    def at_head(name: str) -> str | None:
        return _git(worktree, "show", f"HEAD:{name}")

    return facts_from_diff(
        repo_cfg.name, repo_cfg.role, diff,
        env_base=at_base(ENV_FILE), env_head=at_head(ENV_FILE),
        package_base=at_base(PACKAGE_JSON), package_head=at_head(PACKAGE_JSON),
        migrations_dir=(repo_cfg.paths or {}).get("migrations"),
        seeders_dir=(repo_cfg.paths or {}).get("seeders"),
    )  # fmt: skip


# ---------- decisions & checks ----------


def devops_reasons(facts: DevopsFacts) -> list[str]:
    """Why this repo needs DevOps, in words (empty = nothing for DevOps)."""
    out: list[str] = []
    if facts.new_env_vars or facts.removed_env_vars or facts.undeclared_env_vars \
            or facts.build_time_env_vars:  # fmt: skip
        out.append("environment variables change")
    if facts.migration_files:
        out.append("database migrations" if not any(is_seeder(m) for m in facts.migration_files)
                   else "database migrations and seed data")  # fmt: skip
    if facts.infra_files_changed:
        out.append("infrastructure files change")
    if any(n.startswith(DEVOPS_NOTE_KINDS) for n in facts.notes):
        out.append("new queues, workers or scheduled jobs")
    return out


def needs_devops(all_facts: list[DevopsFacts]) -> bool:
    """Any env change, migration, infra change or queue / cron / worker note (§1.2)."""
    return any(devops_reasons(f) for f in all_facts)


def fact_items(facts: DevopsFacts) -> list[tuple[str, str]]:
    """(kind, item) pairs a deployment note must mention: the search term is the item."""
    items: list[tuple[str, str]] = []
    for kind, values in (
        ("New environment variable", facts.new_env_vars),
        ("Removed environment variable", facts.removed_env_vars),
        ("Build-time environment variable", facts.build_time_env_vars),
        ("Environment variable used but not in .env.example", facts.undeclared_env_vars),
        ("Migration", facts.migration_files),
        ("Infrastructure file", facts.infra_files_changed),
        ("New dependency", facts.new_dependencies),
    ):
        items += [(kind, v) for v in values if (kind, v) not in items]
    return items


def _mentioned(doc: str, kind: str, item: str) -> bool:
    if kind in ("Migration", "Infrastructure file"):
        return item in doc or PurePosixPath(item).name in doc
    if kind == "New dependency":
        return item.rsplit("@", 1)[0] in doc
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(item)}(?![A-Za-z0-9_])", doc) is not None


def missing_facts(doc: str, all_facts: list[DevopsFacts]) -> list[tuple[str, str, str]]:
    """(repo, kind, item) for every detected fact the note doesn't mention."""
    return [(f.repo, kind, item) for f in all_facts for kind, item in fact_items(f)
            if not _mentioned(doc, kind, item)]  # fmt: skip


def repo_section(doc: str, repo: str) -> str:
    """The note's `## <repo> …` block (up to the next `## ` heading); the whole note when the
    repo has no block of its own."""
    lines = doc.splitlines()
    start = next((i for i, ln in enumerate(lines)
                  if ln.startswith("## ") and repo in ln), None)  # fmt: skip
    if start is None:
        return doc
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
               len(lines))  # fmt: skip
    return "\n".join(lines[start:end])


def missing_rollbacks(doc: str, all_facts: list[DevopsFacts]) -> list[tuple[str, str, str]]:
    """A repo with migrations must have a Rollback (undo) in its section of the note (D123)."""
    out = []
    for f in all_facts:
        if f.migration_files and not re.search(r"rollback|undo", repo_section(doc, f.repo), re.I):
            out += [(f.repo, "Rollback (undo) not described for migration",
                     PurePosixPath(m).name) for m in f.migration_files]  # fmt: skip
    return out


def append_missing(doc: str, missing: list[tuple[str, str, str]]) -> str:
    """The consistency check's addendum (§2.3): facts the agent left out are listed, not lost."""
    if not missing:
        return doc
    lines = [f"- **{repo}** · {kind}: `{item}`" for repo, kind, item in missing]
    return (doc.rstrip() + "\n\n## Auto-detected but not described\n\n"
            "The pipeline found these in the change set, but the note above doesn't describe "
            "them. Please check them before deploying.\n\n" + "\n".join(lines) + "\n")  # fmt: skip
