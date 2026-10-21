"""What `implement` asks a person (D202): the tech spec link and the repositories in scope.

They used to be read from two Jira custom fields (Tech Spec URL, Repos). Now the person who
starts the ticket gives them: in the terminal (`pipeline cli`) the pipeline asks, elsewhere
`implement KEY --spec <link> --repos a,b`. The answer is kept as the ticket note `intake` (it
survives `retry` and restarts, and is deleted with the ticket); the Jira fields still work for
tickets that carry them, but nothing needs them.

- `check_spec`: the link must be a Google **Doc** (docs.google.com/document/d/<id>) the pipeline
  can read; otherwise the ticket is not started and the reason is told.
- `spec.source: local` (env TECH_SPEC_SOURCE): the spec is a document in the pipeline's
  `tech_spec/` folder instead; `local_options` lists it and `check_spec_input` takes the number
  or file name the person types. The ticket then keeps a `file://` link in `spec_url`.
- `repo_options`: the pick list is the config's `repos` (names from `pipeline.config.yaml`).
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from pipeline.config import PipelineConfig
from pipeline.integrations.base import IntegrationError, SpecPort
from pipeline.integrations.local_spec import (
    SPEC_SUFFIXES,
    is_local_url,
    list_specs,
    path_of,
    spec_dir,
)

INTAKE_NOTE = "intake"  # ticket note: {"spec_url", "repos", "by"}
_DOC_PATH = re.compile(r"^/document/(?:u/\d+/)?d/([A-Za-z0-9_-]{10,})(?:/|$)")
_OTHER_GOOGLE = {
    "spreadsheets": "a Google Sheet", "presentation": "Google Slides", "forms": "a Google Form",
    "file": "a Google Drive file", "drawings": "a Google Drawing",
}


def parse_spec_url(text: str) -> tuple[str | None, str]:
    """(the link, "") for a Google Docs link; (None, why not) otherwise. A heading or tab part
    (`#heading=…`, `?tab=…`) is kept: the whole document is used either way (D66)."""
    link = (text or "").strip().strip("<>\"'")
    if not link:
        return None, "no link was given"
    if " " in link or "\n" in link:
        return None, "that is more than one link or has spaces in it; paste just the link"
    if "://" not in link and link.startswith(("docs.google.com", "drive.google.com")):
        link = "https://" + link
    parsed = urlparse(link)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None, "that does not look like a link (it should start with https://)"
    host = parsed.netloc.lower()
    if host == "drive.google.com":
        return None, ("that is a Google Drive link; open the document and paste its "
                      "docs.google.com/document/… address")  # fmt: skip
    if host != "docs.google.com":
        return None, f"that is not a Google Docs link (it points to {host})"
    kind = parsed.path.split("/")[1] if parsed.path.count("/") >= 1 else ""
    if kind in _OTHER_GOOGLE:
        return None, f"that is {_OTHER_GOOGLE[kind]}, not a Google Doc"
    if not _DOC_PATH.match(parsed.path):
        return None, ("the link has no document id (it should look like "
                      "docs.google.com/document/d/<id>/edit)")  # fmt: skip
    return link, ""


def check_spec(text: str, spec: SpecPort) -> tuple[str | None, str]:
    """(the link, "") when it is a Google Doc the pipeline can read and that has text; else
    (None, the reason). Reads the document (cached for the planner afterwards)."""
    link, why = parse_spec_url(text)
    if link is None:
        return None, why
    try:
        md, _ = spec.fetch(link)
    except IntegrationError as err:
        return None, _fetch_reason(str(err))
    except Exception as err:  # noqa: BLE001 (a network hiccup is a reason too, not a crash)
        return None, f"the document could not be read ({err})"
    if not (md or "").strip():
        return None, "the document is empty"
    return link, ""


def spec_is_local(config: PipelineConfig) -> bool:
    return config.spec.source == "local"


def local_options(config: PipelineConfig) -> tuple[list[tuple[str, str]], str]:
    """([(file name, link)], "") for the documents in the tech_spec folder; ([], why) when the
    folder is missing or has none."""
    directory = spec_dir(config.spec.local_dir)
    if not directory.is_dir():
        return [], (f"the tech spec folder {directory} doesn't exist; create it and put the "
                    f"document there ({', '.join(SPEC_SUFFIXES)})")  # fmt: skip
    files = list_specs(directory)
    if not files:
        return [], (f"there is no document in the tech spec folder {directory}; put the tech "
                    f"spec there ({', '.join(SPEC_SUFFIXES)})")  # fmt: skip
    return [(p.name, p.as_uri()) for p in files], ""


def check_local_spec(text: str, config: PipelineConfig, spec: SpecPort) -> tuple[str | None, str]:
    """(the `file://` link, "") for a document of the tech_spec folder named by its list number
    or its file name (a link this pipeline made earlier works too); else (None, why)."""
    options, why = local_options(config)
    if not options:
        return None, why
    answer = (text or "").strip().strip("\"'")
    if not answer:
        return None, "no document was chosen"
    link = None
    if answer.isdigit():
        if 1 <= int(answer) <= len(options):
            link = options[int(answer) - 1][1]
    elif is_local_url(answer):
        link = next((u for _, u in options if path_of(u) == path_of(answer)), None)
    else:
        low = answer.lower()
        link = next((u for n, u in options
                     if n.lower() == low or n.rsplit(".", 1)[0].lower() == low), None)
    if link is None:
        names = ", ".join(f"{i}. {n}" for i, (n, _) in enumerate(options, 1))
        return None, f"`{answer}` is not one of the documents in the folder ({names})"
    try:
        md, _ = spec.fetch(link)
    except Exception as err:  # noqa: BLE001
        return None, f"the document could not be read ({err})"
    if not (md or "").strip():
        return None, "the document is empty"
    return link, ""


def check_spec_input(text: str, config: PipelineConfig,
                     spec: SpecPort) -> tuple[str | None, str]:
    """What the person gave for the spec, checked the way `spec.source` says."""
    if spec_is_local(config):
        return check_local_spec(text, config, spec)
    return check_spec(text, spec)


def _fetch_reason(error: str) -> str:
    low = error.lower()
    if "404" in low or "not found" in low:
        return ("the document was not found; check the link, and that it is shared with the "
                "Google account the pipeline reads with")  # fmt: skip
    if "403" in low or "permission" in low or "forbidden" in low or "denied" in low:
        return ("the pipeline has no access to the document; share it with the Google account "
                "the pipeline reads with")  # fmt: skip
    if "token" in low or "credential" in low or "oauth" in low or "invalid_grant" in low:
        return f"the pipeline is not signed in to Google ({error})"
    return f"the document could not be read ({error})"


def repo_options(config: PipelineConfig) -> list[tuple[str, str]]:
    """(name, label) for every repository in the config, in coding order."""
    out = []
    for name in config.repo_order(config.repo_names):
        cfg = config.repo(name)
        label = f"{name}  ({cfg.role}" + (f": {cfg.description}" if cfg.description else "") + ")"
        out.append((name, label))
    return out


def parse_repos(text: str, config: PipelineConfig) -> tuple[list[str], str]:
    """`a,b` / `a b` / numbers from the pick list → (repo names in coding order, "") or
    ([], why not). Only names from the config are accepted."""
    parts = [p for p in re.split(r"[,\s;]+", (text or "").strip()) if p]
    if not parts:
        return [], "no repository was chosen"
    names = config.repo_order(config.repo_names)
    if len(parts) == 1 and parts[0].lower() in ("all", "*"):
        return names, ""
    chosen: list[str] = []
    for part in parts:
        if part.isdigit() and 1 <= int(part) <= len(names):
            part = names[int(part) - 1]
        if part not in config.repo_names:
            known = ", ".join(names)
            return [], f"`{part}` is not one of the configured repositories ({known})"
        if part not in chosen:
            chosen.append(part)
    return config.repo_order(chosen), ""


def intake_of(store: Any, key: str) -> dict:
    """The ticket's saved answer ({} when none)."""
    value = store.get_note(key, INTAKE_NOTE) if store is not None else None
    return value if isinstance(value, dict) else {}
