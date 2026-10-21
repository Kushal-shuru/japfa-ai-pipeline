"""A short map of one repo for the planner (D128): folder sizes, the files whose paths match the
ticket's words (the feature's neighbourhood: the nearest similar feature to copy from) with their
top-level functions, and the routes those files register. Deterministic; no model call."""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

from pipeline.analysis.express_routes import extract_routes

_SKIP_DIRS = {"node_modules", ".git", ".agent", "coverage", "dist", "build", ".nyc_output"}
_CODE_EXT = {".js", ".ts", ".mjs", ".cjs", ".vue", ".json", ".sql"}
_EXPORTS = [
    re.compile(r"^\s*exports\.(\w+)\s*=", re.M),
    re.compile(r"^\s*module\.exports\.(\w+)\s*=", re.M),
    re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)", re.M),
    re.compile(r"^(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?(?:\([^)]*\)|\w+)\s*=>", re.M),
]
_MODULE_EXPORTS = re.compile(r"module\.exports\s*=\s*\{([^}]*)\}", re.S)
_STOP = {"the", "and", "for", "with", "api", "apis", "v1", "v2", "src", "index", "backend",
         "business", "get", "post", "put", "patch", "delete", "new", "add", "list", "js", "ts",
         "test", "spec", "unit", "create", "seed"}  # fmt: skip


def words(text: str) -> set[str]:
    """Lowercase stems of `text`: camelCase / kebab / snake split, plural 's' dropped."""
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    out = set()
    for w in re.findall(r"[a-z0-9]+", text.lower()):
        if w in _STOP:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        if len(w) > 2 and not w.isdigit() and w not in _STOP:
            out.add(w)
    return out


def _files(root: Path) -> list[Path]:
    out = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix in _CODE_EXT and not (_SKIP_DIRS & set(p.parts)):
            out.append(p)
    return sorted(out)


def functions(path: Path, limit: int = 10) -> list[str]:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    names: list[str] = []
    for rx in _EXPORTS:
        names += rx.findall(text)
    for block in _MODULE_EXPORTS.findall(text):
        if re.fullmatch(r"[\s\w,]*", block):  # shorthand `{ a, b }` only, not an object literal
            names += re.findall(r"\w+", block)
    seen: list[str] = []
    for n in names:
        if n not in seen:
            seen.append(n)
    return seen[:limit]


def neighbourhood(root: Path, keywords: set[str], limit: int = 40) -> list[tuple[str, float]]:
    """Repo-relative paths ranked by how many (rare) ticket words their path contains."""
    files = _files(root)
    rel = [str(p.relative_to(root)) for p in files]
    stems = [words(r) for r in rel]
    df = Counter(w for s in stems for w in s & keywords)
    n = max(len(rel), 1)
    scored = []
    for r, s in zip(rel, stems, strict=True):
        hit = s & keywords
        if hit:
            scored.append((r, sum(math.log(1 + n / df[w]) for w in hit)))
    scored.sort(key=lambda x: (-x[1], x[0]))
    return scored[:limit]


def repo_map(root: Path, name: str, role: str, ticket_text: str, limit: int = 40) -> str:
    root = root.resolve()
    keywords = words(ticket_text)
    files = _files(root)
    shown = ("src", "test", "tests", "locales")
    dirs = Counter(str(p.relative_to(root).parent) for p in files
                   if p.relative_to(root).parts[0] in shown)  # fmt: skip
    top: Counter[str] = Counter()
    for d, c in dirs.items():
        top["/".join(d.split("/")[:2])] += c
    near = neighbourhood(root, keywords, limit)
    out = [f"## {name} ({role})", "", "**Folders** (files): "
           + ", ".join(f"`{d}` {c}" for d, c in sorted(top.items()))]  # fmt: skip
    if near:
        out += ["", "**Files near this ticket** (paths matching its words; the closest similar "
                "feature is usually the one to copy):", ""]  # fmt: skip
        for rel, _ in near:
            fns = functions(root / rel)
            out.append(f"- `{rel}`" + (f": {', '.join(fns)}" if fns else ""))
    try:
        routes = extract_routes(root, [r for r, _ in near if "route" in r.lower()])
    except Exception:  # a scanner problem must never stop planning
        routes = []
    if routes:
        out += ["", "**Routes in those files:**", ""]
        out += [f"- {r.method} {r.path} ({r.file}:{r.line})" for r in routes[:40]]
    return "\n".join(out) + "\n"
