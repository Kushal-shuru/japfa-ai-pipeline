"""Hidden idempotency markers on posted comments: `aipl:{KEY}:{node}:{round}:{extra}` (§9.3)."""

from __future__ import annotations

import re

PREFIX = "aipl"
_MARKER_RE = re.compile(r"aipl:[A-Z][A-Z0-9_]*-\d+:[\w.-]+:[\w.-]*:[\w.,+-]*")


def make_marker(key: str, node: str, round: int | str = 0, extra: str = "") -> str:
    """`aipl:DEMO-12:review:2:demo-ws1-closio`. Fields must not contain ':' or whitespace."""
    parts = [key, node, str(round), extra]
    for p in parts:
        if ":" in p or any(c.isspace() for c in p):
            raise ValueError(f"marker part {p!r} must not contain ':' or whitespace")
    return ":".join([PREFIX, *parts])


def gitlab_marker(marker: str) -> str:
    """Trailer for GitLab markdown: an HTML comment (invisible when rendered)."""
    return f"\n\n<!-- {marker} -->"


# Jira form: `aipl:{node}:{round}[:{extra}]`. No ticket key: Jira renders an issue key as a
# smart link (title + status) and the comment already lives on its ticket (D72). The first
# segment is a lowercase node name, so this never matches the keyed form.
_JIRA_MARKER_RE = re.compile(r"aipl:([a-z][\w.-]*):([\w.-]*)(?::([\w.,+-]+))?")


def jira_short(marker: str) -> str:
    """`aipl:DEMO-7:open_mrs:initial:` → `aipl:open_mrs:initial` (key and empty extra dropped)."""
    _, _key, node, round_, extra = marker.split(":", 4)
    return f"{PREFIX}:{node}:{round_}" + (f":{extra}" if extra else "")


# Jira comments carry the marker as a hidden comment property (`JIRA_PROPERTY`), read back with
# `expand=properties`. Older comments carry it as text (`jira_marker`), still recognised.
JIRA_PROPERTY = "aipl"


def jira_marker(marker: str) -> str:
    """Old text trailer for Jira wiki markup (small and grey, short form without the key).
    Only recognised now: new comments carry the marker as a property."""
    return f"\n\n{{color:#dfe1e6}}{jira_short(marker)}{{color}}"


def find_jira_markers(text: str | None, key: str) -> list[str]:
    """Full markers (`aipl:{key}:…`) in a Jira comment body: short-form markers rebuilt with
    `key`, plus any old keyed markers posted before D72."""
    found = [make_marker(key, m[0], m[1], m[2] or "") for m in _JIRA_MARKER_RE.findall(text or "")]
    return found + [m for m in find_markers(text) if m.startswith(f"{PREFIX}:{key}:")]


def has_jira_marker(text: str | None, marker: str) -> bool:
    """True if a Jira comment body carries `marker`, in the short or the old keyed form."""
    key = marker.split(":", 2)[1]
    return marker in find_jira_markers(text, key)


def jira_comment_markers(comment: dict, key: str) -> list[str]:
    """Full markers of a Jira comment dict: its property marker, plus any text markers."""
    prop = [comment["marker"]] if comment.get("marker") else []
    return prop + find_jira_markers(comment.get("body"), key)


def is_pipeline_jira_comment(comment: dict) -> bool:
    """True for a Jira comment the pipeline wrote: a property marker or a text marker."""
    return bool(comment.get("marker")) or is_pipeline_comment(comment.get("body"))


def is_pipeline_comment(text: str | None) -> bool:
    """True for any comment the pipeline wrote (either marker form)."""
    return bool(find_markers(text) or _JIRA_MARKER_RE.search(text or ""))


def has_marker(text: str | None, marker: str) -> bool:
    """True if `marker` appears in `text` as a whole marker (not a prefix of a longer one)."""
    if not text:
        return False
    return marker in _MARKER_RE.findall(text)


def find_markers(text: str | None) -> list[str]:
    return _MARKER_RE.findall(text or "")
