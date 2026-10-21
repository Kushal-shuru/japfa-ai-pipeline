"""Gateway route extraction (Phase 7 §1): static, best effort, no Node needed.

Finds `router.<method>('/path', …)` / `app.<method>(…)` calls in the files an MR changed and
builds each route's full public path from the mounts that lead to its file:
`app.use(routes)` → `router.use('/api', incentivesRoutes)` → `router.use('/incentives', x)` →
`router.patch('/v1/payout-configs/:config_id/status', validateSchema('…'), …)` gives
`PATCH /api/incentives/v1/payout-configs/:config_id/status`. Mount targets are followed through
local `require` / `import` statements (any depth, cycles ignored). The first validator-looking
middleware (`validate…(…)`, `celebrate(…)`) is recorded for each route.

`cross_check` compares the result with the contract's `public_endpoints` and the coders'
`api_changes`; disagreements become plain notes (for developers, in the QA notes).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

METHODS = ("get", "post", "put", "patch", "delete")
SKIP_DIRS = {"node_modules", ".git", ".agent", "coverage", "dist", "build", "test", "tests"}
MAX_DEPTH = 8

_REQUIRE = re.compile(
    r"""(?:const|let|var)\s+(\w+)\s*=\s*require\(\s*['"](\.{1,2}/[^'"]+)['"]\s*\)"""
)
_IMPORT = re.compile(r"""import\s+(\w+)\s+from\s+['"](\.{1,2}/[^'"]+)['"]""")
_ROUTER_VAR = re.compile(r"(?:const|let|var)\s+(\w+)\s*=\s*(?:express\s*\.\s*)?Router\s*\(")
_APP_VAR = re.compile(r"(?:const|let|var)\s+(\w+)\s*=\s*express\s*\(\s*\)")
_USE = re.compile(
    r"""\b(\w+)\s*\.\s*use\(\s*(?:(['"`])([^'"`]*)\2\s*,\s*)?"""
    r"""(?:require\(\s*['"](\.{1,2}/[^'"]+)['"]\s*\)|(\w+))"""
)
_ROUTE = re.compile(r"""\b(\w+)\s*\.\s*(get|post|put|patch|delete)\(\s*(['"`])([^'"`]+)\3""")
_VALIDATOR = re.compile(r"""\b((?:validate\w*|celebrate))\s*\(\s*([^)]*)\)""")
_PARAM = re.compile(r":\w+|\{\w+\}")


@dataclass(frozen=True)
class Route:
    method: str  # GET, POST, …
    path: str  # full public path with mount prefixes
    file: str  # relative to the repo root
    line: int
    validator: str | None = None  # e.g. "validateSchema('schemas/v1/…')"

    def dump(self) -> dict[str, Any]:
        return asdict(self)


def _js_files(root: Path) -> list[Path]:
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.suffix in (".js", ".mjs", ".cjs", ".ts") and not (set(rel.parts[:-1]) & SKIP_DIRS):
            out.append(p)
    return out


def _resolve(base: Path, spec: str, root: Path) -> Path | None:
    target = (base.parent / spec).resolve()
    for cand in (target, *(target.with_name(target.name + e) for e in (".js", ".ts", ".mjs")),
                 target / "index.js", target / "index.ts"):  # fmt: skip
        if cand.is_file() and root in cand.parents:
            return cand
    return None


def _join(*parts: str) -> str:
    path = "/" + "/".join(p.strip("/") for p in parts if p and p.strip("/"))
    return path


def _mounts(root: Path, files: list[Path]) -> dict[Path, list[tuple[str, Path]]]:
    """parent file → [(prefix, child file)] from `x.use('/prefix', <local module>)`."""
    edges: dict[Path, list[tuple[str, Path]]] = {}
    for f in files:
        text = f.read_text(errors="replace")
        local = {m.group(1): m.group(2) for rx in (_REQUIRE, _IMPORT) for m in rx.finditer(text)}
        for m in _USE.finditer(text):
            spec = m.group(4) or local.get(m.group(5) or "")
            if not spec:
                continue  # middleware, not a router module
            child = _resolve(f, spec, root)
            if child is not None and child != f:
                edges.setdefault(f, []).append((m.group(3) or "", child))
    return edges


def _prefixes(files: list[Path], edges: dict[Path, list[tuple[str, Path]]]) -> dict[Path, set[str]]:
    """Every full prefix under which each file is mounted, starting from files nobody mounts."""
    children = {c for kids in edges.values() for _, c in kids}
    out: dict[Path, set[str]] = {}

    def walk(f: Path, prefix: str, seen: tuple[Path, ...]) -> None:
        out.setdefault(f, set()).add(prefix)
        if len(seen) > MAX_DEPTH:
            return
        for sub, child in edges.get(f, []):
            if child not in seen:
                walk(child, _join(prefix, sub), (*seen, child))

    for f in files:
        if f not in children:
            walk(f, "", (f,))
    return out


def extract_routes(root: Path, changed: list[str] | None = None) -> list[Route]:
    """Routes defined in `changed` files (all files when None), with full paths."""
    root = root.resolve()
    files = _js_files(root)
    prefixes = _prefixes(files, _mounts(root, files))
    wanted = None if changed is None else {(root / c).resolve() for c in changed}
    routes: list[Route] = []
    for f in files:
        if wanted is not None and f not in wanted:
            continue
        text = f.read_text(errors="replace")
        owners = {m.group(1) for rx in (_ROUTER_VAR, _APP_VAR) for m in rx.finditer(text)}
        owners |= {"app", "router"}
        calls = [m for m in _ROUTE.finditer(text) if m.group(1) in owners]
        for i, m in enumerate(calls):
            end = calls[i + 1].start() if i + 1 < len(calls) else min(len(text), m.end() + 1500)
            v = _VALIDATOR.search(text, m.end(), end)
            validator = f"{v.group(1)}({v.group(2).strip()})" if v else None
            line = text.count("\n", 0, m.start()) + 1
            for prefix in sorted(prefixes.get(f) or {""}):
                routes.append(Route(m.group(2).upper(), _join(prefix, m.group(4)),
                                    str(f.relative_to(root)), line, validator))  # fmt: skip
    return routes


# ---------- cross-check (1.2) ----------


def norm_path(path: str) -> str:
    """`/a/:id/` and `/a/{id}` compare equal."""
    return _PARAM.sub(":p", "/" + path.strip().strip("/"))


def _same(a_method: str, a_path: str, b_method: str, b_path: str) -> bool:
    if a_method.upper() != b_method.upper():
        return False
    a, b = norm_path(a_path), norm_path(b_path)
    # the contract may give the path without the gateway's mount prefix
    return a == b or a.endswith(b) or b.endswith(a)


def cross_check(routes: list[Route], plan: Any, api_changes: list[Any]) -> list[str]:
    """Plain notes where the code, the contract and the coders' reports disagree."""
    notes: list[str] = []
    public = list(plan.contract.public_endpoints) if plan is not None else []
    for pe in public:
        if not any(_same(r.method, r.path, pe.method, pe.path) for r in routes):
            notes.append(f"The contract's public endpoint {pe.id} ({pe.method} {pe.path}) was "
                         "not found in the gateway's changed route files.")  # fmt: skip
    for r in routes:
        in_contract = any(_same(r.method, r.path, pe.method, pe.path) for pe in public)
        reported = any(_same(r.method, r.path, c.method, c.path) for c in api_changes)
        if public and not in_contract:
            notes.append(f"{r.method} {r.path} ({r.file}:{r.line}) is in the changed code but "
                         "not in the contract's public endpoints.")  # fmt: skip
        elif not public and not reported:
            notes.append(f"{r.method} {r.path} ({r.file}:{r.line}) changed but the coder didn't "
                         "report it as an API change.")  # fmt: skip
    for c in api_changes:
        if not any(_same(r.method, r.path, c.method, c.path) for r in routes):
            notes.append(f"The coder reported {c.method} {c.path}, but no such route was found "
                         "in the changed files.")  # fmt: skip
    return notes


def changed_files_from_diff(diff: str) -> list[str]:
    """New-side paths of a unified diff (`+++ b/<path>`), deleted files left out."""
    out = []
    for line in diff.splitlines():
        if line.startswith("+++ ") and not line.startswith("+++ /dev/null"):
            path = line[4:].strip()
            out.append(path[2:] if path.startswith("b/") else path)
    return list(dict.fromkeys(out))
