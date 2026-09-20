"""goodinfo.tw client — polite-only, opt-in, off by default.

Read this before turning it on.

goodinfo.tw's terms of service prohibit automated collection, and the
site runs bot detection. This client does **not** try to get around
that: no browser-fingerprint spoofing, no randomized mouse/scroll
simulation, no CAPTCHA solving, no header rotation, no proxy pool, no
retry-through-a-block. What it does do:

- Off unless GOODINFO_ENABLED=1 is set (see config.GoodinfoConfig).
- Checks robots.txt at runtime and refuses to fetch a disallowed path,
  or any path at all if robots.txt itself can't be fetched (fails
  closed rather than assuming permission).
- One request at a time, with a long fixed minimum delay between them
  (default 12s) — this is a ceiling on load, not a "how fast can we
  go without getting blocked" tuning knob. Do not lower it to speed
  the client up; that defeats the point of it.
- A hard cap on requests per process run (default 50), via RunBudget,
  so a mistaken full-market ticker list can't turn into thousands of
  requests.
- An honest, identifying User-Agent — not a browser impersonation
  string.

Given all of that, running this against goodinfo.tw is still a ToS
violation if their terms prohibit automated access (they do, as of
this project's own SKILL.md notes). Whoever enables
GOODINFO_ENABLED=1 is making that call for themselves, not being
talked into it by this code. The project's existing default workflow
— asking the user to screenshot the Goodinfo page instead — remains
the recommended path; prefer that over enabling this module.

This client only fetches raw HTML and stores it verbatim; it does not
attempt to parse Goodinfo's page structure into structured fields.
Extraction logic needs real fetched pages to build and test against,
which this sandbox can't reach — add a parser once you have samples,
rather than trusting a guessed DOM structure.
"""
from __future__ import annotations

from typing import Any

import requests

from core.http_client import get_text
from core.logger import get_logger
from core.rate_limiter import RateLimiter, RunBudget
from core.robots import RobotsCheck

logger = get_logger(__name__)

PAGES = {
    "cash_flow": "/tw/StockCashFlow.asp",
    "monthly_revenue_chart": "/tw/ShowSaleMonChart.asp",
}


def _robots_http_get(url: str) -> str:
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.text


class GoodinfoClient:
    def __init__(self, config):
        self.config = config
        self.rate_limiter = RateLimiter(min_delay_seconds=config.min_delay_seconds)
        self.budget = RunBudget(config.max_requests_per_run)
        self.robots = RobotsCheck(config.base_url, config.user_agent, _robots_http_get)

    def fetch_page(self, page_key: str, ticker: str) -> dict[str, Any] | None:
        if not self.config.enabled:
            logger.info("Goodinfo client disabled (GOODINFO_ENABLED not set) — skipping %s/%s", page_key, ticker)
            return None
        if page_key not in PAGES:
            raise KeyError(f"Unknown Goodinfo page key {page_key!r}")
        path = PAGES[page_key]
        if not self.robots.can_fetch(path):
            logger.warning("robots.txt disallows %s (or was unreachable) — skipping %s", path, ticker)
            return None

        self.budget.consume()  # raises RunBudgetExceeded if the per-run cap is hit
        headers = {"User-Agent": self.config.user_agent}
        params = {"STOCK_ID": ticker}
        text = get_text(
            self.config.base_url + path,
            params=params,
            headers=headers,
            rate_limiter=self.rate_limiter,
            max_retries=self.config.max_retries,
            timeout=self.config.timeout_seconds,
        )
        return {"ticker": ticker, "page": page_key, "raw_html": text}
