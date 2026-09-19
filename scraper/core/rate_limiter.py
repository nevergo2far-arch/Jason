"""Simple, dependency-free rate limiting.

The batch runner is intentionally sequential (one request at a time)
rather than concurrent — that's what keeps every source's load on the
target server low and predictable, which matters more here than raw
throughput.
"""
from __future__ import annotations

import time


class RateLimiter:
    """Enforces a minimum gap between calls to `wait()`.

    Can be constructed either from requests-per-minute or straight
    from a minimum delay in seconds (Goodinfo's config uses the
    latter since "gap between requests" is the thing we want to
    reason about directly there).
    """

    def __init__(self, *, requests_per_minute: float | None = None, min_delay_seconds: float | None = None):
        if min_delay_seconds is not None:
            self.min_interval = max(0.0, min_delay_seconds)
        elif requests_per_minute is not None and requests_per_minute > 0:
            self.min_interval = 60.0 / requests_per_minute
        else:
            self.min_interval = 0.0
        self._last_call: float | None = None

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        now = time.monotonic()
        if self._last_call is not None:
            elapsed = now - self._last_call
            remaining = self.min_interval - elapsed
            if remaining > 0:
                time.sleep(remaining)
        self._last_call = time.monotonic()


class RunBudget:
    """Caps the total number of requests spent in a single process run.

    Used by the Goodinfo client so a single invocation can never
    accidentally hammer the site even if the caller passes a huge
    ticker list.
    """

    def __init__(self, max_requests: int):
        self.max_requests = max_requests
        self.spent = 0

    def consume(self) -> None:
        if self.spent >= self.max_requests:
            raise RunBudgetExceeded(
                f"Run budget of {self.max_requests} requests exhausted for this source. "
                "Re-run the batch job later (already-fetched tickers are skipped via the checkpoint)."
            )
        self.spent += 1


class RunBudgetExceeded(RuntimeError):
    pass
