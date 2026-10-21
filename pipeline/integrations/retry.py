"""Retry with exponential backoff for HTTP integrations (429 / 5xx / connection errors)."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TypeVar

import requests

from pipeline.integrations.base import IntegrationError
from pipeline.logging import get_logger

T = TypeVar("T")

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def http_status(err: BaseException) -> int | None:
    response = getattr(err, "response", None)
    return getattr(response, "status_code", None)


def is_retryable(err: BaseException) -> bool:
    if isinstance(err, requests.ConnectionError | requests.Timeout):
        return True
    status = http_status(err)
    return status in RETRY_STATUS


def with_retry(
    fn: Callable[[], T],
    *,
    service: str,
    what: str,
    tries: int = 3,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call `fn`; retry retryable errors up to `tries` total; wrap failures in IntegrationError."""
    log = get_logger("pipeline.retry", service=service)
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except IntegrationError:
            raise
        except Exception as err:
            retryable = is_retryable(err)
            if retryable and attempt < tries:
                delay = base_delay * 2 ** (attempt - 1)
                response = getattr(err, "response", None)
                retry_after = response.headers.get("Retry-After") if response is not None else None
                if retry_after and str(retry_after).isdigit():
                    delay = max(delay, float(retry_after))
                log.warning("retrying", what=what, attempt=attempt, delay=delay, error=str(err))
                sleep(delay)
                continue
            status = http_status(err)
            detail = f" (HTTP {status})" if status else ""
            raise IntegrationError(
                f"{service}: {what} failed{detail}: {err}", service=service, retryable=retryable
            ) from err
    raise AssertionError("unreachable")  # pragma: no cover
