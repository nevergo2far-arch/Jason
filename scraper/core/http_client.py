"""Thin HTTP wrapper: retry with backoff on transient failures,
consistent timeout handling. Each source client owns its own
RateLimiter and calls through this so retries never bypass the rate
limit (the limiter's `wait()` runs before every attempt, including
retries).
"""
from __future__ import annotations

import time
from typing import Any

import requests

from .logger import get_logger
from .rate_limiter import RateLimiter

logger = get_logger(__name__)

RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class HttpError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    rate_limiter: RateLimiter,
    max_retries: int,
    timeout: float,
) -> Any:
    return _request_with_retry(
        url, params=params, headers=headers, rate_limiter=rate_limiter,
        max_retries=max_retries, timeout=timeout, parse=lambda r: r.json(),
    )


def get_text(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    rate_limiter: RateLimiter,
    max_retries: int,
    timeout: float,
) -> str:
    return _request_with_retry(
        url, params=params, headers=headers, rate_limiter=rate_limiter,
        max_retries=max_retries, timeout=timeout, parse=lambda r: r.text,
    )


def _request_with_retry(url, *, params, headers, rate_limiter: RateLimiter, max_retries: int, timeout: float, parse):
    last_exc: Exception | None = None
    for attempt in range(1, max_retries + 1):
        rate_limiter.wait()
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException as exc:
            last_exc = exc
            logger.warning("request failed (attempt %d/%d) %s: %s", attempt, max_retries, url, exc)
            _backoff_sleep(attempt)
            continue

        if resp.status_code in RETRYABLE_STATUS_CODES:
            last_exc = HttpError(f"HTTP {resp.status_code} from {url}", resp.status_code)
            logger.warning(
                "retryable status %d (attempt %d/%d) %s", resp.status_code, attempt, max_retries, url
            )
            _backoff_sleep(attempt)
            continue

        if resp.status_code >= 400:
            raise HttpError(f"HTTP {resp.status_code} from {url}: {resp.text[:300]}", resp.status_code)

        return parse(resp)

    raise HttpError(f"Exhausted {max_retries} retries for {url}: {last_exc}")


def _backoff_sleep(attempt: int) -> None:
    time.sleep(min(2 ** attempt, 30))
