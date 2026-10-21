"""structlog setup: pretty console + JSON file, with secret values masked."""

from __future__ import annotations

import logging
import logging.handlers
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import structlog

MASK = "***"

_secret_values: list[str] = []


def set_secret_values(values: Iterable[str]) -> None:
    """Register secret strings to mask. Short values are ignored (too many false hits)."""
    _secret_values[:] = sorted({v for v in values if v and len(v) >= 6}, key=len, reverse=True)


def add_secret_values(values: Iterable[str]) -> None:
    """Mask these too, keeping the ones already registered (P10: the dev DB credentials and the
    secrets a test stack generates)."""
    set_secret_values([*_secret_values, *values])


def _mask(value: Any) -> Any:
    if isinstance(value, str):
        for secret in _secret_values:
            if secret in value:
                value = value.replace(secret, MASK)
        return value
    if isinstance(value, dict):
        return {k: _mask(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(_mask(v) for v in value)
    return value


def mask_value(value: Any) -> Any:
    """Registered secret values in `value` (str / dict / list) → `***`."""
    return _mask(value)


def mask_text(value: Any) -> Any:
    """Everything that leaves the machine (chat, Jira, GitLab, Chat LLM prompts; P8 1.8):
    registered secret values **and** anything shaped like a credential (verify's secret
    patterns: private keys, tokens, JWTs, hard-coded passwords) → `***`. Str / dict / list."""
    from pipeline.verify import SECRET_PATTERNS

    if isinstance(value, str):
        value = _mask(value)
        for _label, pattern in SECRET_PATTERNS:
            value = pattern.sub(MASK, value)
        return value
    if isinstance(value, dict):
        return {k: mask_text(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return type(value)(mask_text(v) for v in value)
    return value


def mask_file(path: Path) -> bool:
    """Registered secret values in a written file (an agent transcript) → `***`, in place."""
    if not _secret_values or not path.is_file():
        return False
    text = path.read_text(errors="replace")
    masked = _mask(text)
    if masked == text:
        return False
    path.write_text(masked)
    return True


def mask_secrets(_logger: Any, _method: str, event_dict: dict) -> dict:
    """structlog processor: replace any known secret value with ***."""
    if not _secret_values:
        return event_dict
    return {k: _mask(v) for k, v in event_dict.items()}


_SHARED: list[Any] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso"),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
    mask_secrets,
]


def configure_logging(
    log_file: Path | None = None,
    level: str = "INFO",
    secrets: Iterable[str] = (),
    console_level: str | None = None,
) -> None:
    """Console (pretty) to stderr, plus JSON lines to `log_file` if given. `console_level`
    raises the console threshold (e.g. WARNING while chat output is shown)."""
    set_secret_values(secrets)
    structlog.configure(
        processors=[*_SHARED, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level)

    console = logging.StreamHandler()
    console.setLevel(console_level or level)
    console.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=_SHARED,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.dev.ConsoleRenderer(),
            ],
        )
    )
    root.addHandler(console)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10_000_000, backupCount=5, encoding="utf-8"
        )
        fh.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                foreign_pre_chain=_SHARED,
                processors=[
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.JSONRenderer(),
                ],
            )
        )
        root.addHandler(fh)

    # Quiet chatty libraries.
    for name in ("urllib3", "googleapiclient", "httpx", "atlassian"):
        logging.getLogger(name).setLevel(logging.WARNING)


def get_logger(name: str = "pipeline", **bind: Any) -> structlog.stdlib.BoundLogger:
    """Logger bound with e.g. ticket=, node=, repo=, slot= (None values are dropped)."""
    log = structlog.get_logger(name)
    values = {k: v for k, v in bind.items() if v is not None}
    return log.bind(**values) if values else log
