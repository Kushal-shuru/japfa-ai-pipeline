"""`.env` files and `{{…}}` templates for the live test stack (P10).

The developer clone's `.env` holds the dev DB credentials. It is read here, when the tests run,
and its values only ever go into the environment of the app processes the stack starts: never
into a file, a worktree, a prompt or a message (the values are registered for masking).
"""

from __future__ import annotations

import os
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_TEMPLATE = re.compile(r"\{\{\s*([\w.:-]+)\s*\}\}")


def parse_env(text: str) -> dict[str, str]:
    """KEY=VALUE lines; quotes removed, `#` comments after an unquoted value dropped."""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        m = _LINE.match(raw)
        if not m:
            continue
        key, value = m.group(1), m.group(2).strip()
        if value[:1] in ("'", '"'):
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        out[key] = value
    return out


def expand_path(path: str, home: Path) -> Path:
    return Path(os.path.expandvars(path.replace("$PIPELINE_HOME", str(home)))).expanduser()


def read_env_file(path: str | None, home: Path) -> dict[str, str]:
    """The values of `stack.env_from` ({} when not set; FileNotFoundError when set but missing)."""
    if not path:
        return {}
    file = expand_path(path, home)
    return parse_env(file.read_text())


def date_variables(now: datetime | None = None) -> dict[str, Any]:
    """`{{date.…}}` for cases whose rules depend on today (a period that is past, running or in
    the future): `today`, and per month relative to the current UTC month `m0` (this month),
    `p1`…`p48` (that many months ahead), `n1`…`n36` (that many months back), each as `YYYY-MM`
    with `_first` / `_last` for its first and last day (`{{date.p2}}`, `{{date.p2_first}}`,
    `{{date.p2_last}}`). Cases that use them stay right whatever day the tests run."""
    now = now or datetime.now(UTC)
    out: dict[str, Any] = {"today": now.date().isoformat()}
    base = now.year * 12 + now.month - 1
    for off in range(-36, 49):
        year, month = divmod(base + off, 12)
        month += 1
        name = "m0" if off == 0 else f"p{off}" if off > 0 else f"n{-off}"
        last = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)).day
        out[name] = f"{year:04d}-{month:02d}"
        out[f"{name}_first"] = f"{year:04d}-{month:02d}-01"
        out[f"{name}_last"] = f"{year:04d}-{month:02d}-{last:02d}"
    return out


def lookup(name: str, variables: dict[str, Any]) -> Any:
    """`run.partner_id` / `seed.b1.id` / `saved.cfgId` / `url:ws2` → the value, or KeyError."""
    if ":" in name:
        head, _, rest = name.partition(":")
        return variables[head][rest]
    value: Any = variables
    for part in name.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        else:
            raise KeyError(name)
    return value


def render(text: str, variables: dict[str, Any]) -> str:
    """Replace every `{{name}}` in a string (KeyError for an unknown name)."""
    return _TEMPLATE.sub(lambda m: str(lookup(m.group(1), variables)), text)


def render_value(value: Any, variables: dict[str, Any]) -> Any:
    """Templates inside a JSON-like value. A string that is exactly one template keeps the
    value's type (`"{{seed.b1.id}}"` → 42, not "42")."""
    if isinstance(value, str):
        whole = _TEMPLATE.fullmatch(value.strip())
        if whole:
            return lookup(whole.group(1), variables)
        return render(value, variables)
    if isinstance(value, dict):
        return {k: render_value(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [render_value(v, variables) for v in value]
    return value


def template_names(value: Any) -> list[str]:
    """Every `{{name}}` used anywhere in a JSON-like value."""
    if isinstance(value, str):
        return _TEMPLATE.findall(value)
    if isinstance(value, dict):
        return [n for v in value.values() for n in template_names(v)]
    if isinstance(value, list):
        return [n for v in value for n in template_names(v)]
    return []


def sql_params(sql: str, variables: dict[str, Any]) -> tuple[str, list[Any]]:
    """`{{x}}` in a query → `%s` placeholders + their values (never pasted into the text)."""
    params: list[Any] = []

    def sub(m: re.Match[str]) -> str:
        params.append(lookup(m.group(1), variables))
        return "%s"

    return _TEMPLATE.sub(sub, sql.replace("%", "%%")), params
