"""公開資訊觀測站 (MOPS) direct-query fallback.

Prefer `twse_openapi_client` and `finmind_client` for anything they
cover — they're documented, stable APIs. This client exists only for
per-company filing detail that isn't mirrored there. It is public
official disclosure data (not a third-party site with an anti-bot
ToS like Goodinfo), but MOPS has no documented public REST API for
this, so treat it the same as any other "be a good citizen" scrape:
- robots.txt is checked at runtime before any request; a path that's
  disallowed, or a robots.txt that can't be fetched at all, blocks
  the request rather than proceeding anyway (fail closed).
- Honest, identifying User-Agent (see config.MopsConfig).
- Conservative fixed rate limit (default 12 req/min), sequential only.

IMPORTANT — the query path below could not be verified from this
sandbox (no general internet egress here). Confirm against MOPS's own
site before relying on it: open the page for a company's financial
statement query in a browser, check the network tab for the actual
form-submit endpoint, and update QUERY_PATH accordingly.
"""
from __future__ import annotations

from typing import Any

import requests

from core.http_client import get_text
from core.logger import get_logger
from core.rate_limiter import RateLimiter
from core.robots import RobotsCheck

logger = get_logger(__name__)

# VERIFY: last known financial-statement query CGI path under
# mopsov.twse.com.tw/mops/web/. Update if MOPS has changed it.
QUERY_PATH = "/ajax_t05st03"


def _robots_http_get(url: str) -> str:
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    return resp.text


class MopsClient:
    def __init__(self, config):
        self.config = config
        self.rate_limiter = RateLimiter(requests_per_minute=config.requests_per_minute)
        self.robots = RobotsCheck(config.base_url, config.user_agent, _robots_http_get)

    def fetch_financial_statement_detail(self, ticker: str, year: str, season: str) -> dict[str, Any] | None:
        if not self.config.enabled:
            logger.info("MOPS detail client disabled via config; skipping ticker=%s", ticker)
            return None
        if not self.robots.can_fetch(QUERY_PATH):
            logger.warning(
                "robots.txt disallows %s (or was unreachable) — skipping MOPS detail fetch for %s. "
                "This client fails closed on purpose.", QUERY_PATH, ticker,
            )
            return None

        headers = {"User-Agent": self.config.user_agent}
        params = {"co_id": ticker, "year": year, "season": season}
        text = get_text(
            self.config.base_url + QUERY_PATH,
            params=params,
            headers=headers,
            rate_limiter=self.rate_limiter,
            max_retries=self.config.max_retries,
            timeout=self.config.timeout_seconds,
        )
        return {"ticker": ticker, "year": year, "season": season, "raw_response": text}
