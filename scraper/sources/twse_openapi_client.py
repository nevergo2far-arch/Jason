"""TWSE / TPEx official Open Data API client.

https://openapi.twse.com.tw/ and https://www.tpex.org.tw/openapi/ are
the exchanges' own published open-data portals: unauthenticated,
no-ToS-restriction JSON endpoints, explicitly meant for programmatic
consumption. They're also efficient for a whole-market batch job —
most endpoints return every listed company's row for a given report
in a single call, instead of one call per ticker.

Each entry below was verified against a real request in GitHub Actions
(this sandbox has no internet egress to do that itself — see
scraper/README.md). Swagger specs, for re-checking if an exchange
renames/moves something later: https://openapi.twse.com.tw/v1/swagger.json
and https://www.tpex.org.tw/openapi/swagger.json (note: no "/v1" in the
TPEx one, unlike its data paths below).

There is deliberately no "monthly revenue" entry here: neither
exchange's open-data portal has a plain per-company monthly-revenue
endpoint (TPEx has aggregate/derived tables like
/mopsfin_t187ap05_OA "二十九大類股營收變化統計表", but nothing
matching finmind_client.py's per-ticker TaiwanStockMonthRevenue
shape). Get monthly revenue from FinMind, which does cover it.
"""
from __future__ import annotations

from typing import Any

from core.http_client import get_json
from core.logger import get_logger
from core.rate_limiter import RateLimiter

logger = get_logger(__name__)

# path -> (exchange, human description). Verified working (see module
# docstring); re-verify if one starts returning unexpected data.
ENDPOINTS: dict[str, tuple[str, str]] = {
    "twse_stock_day_all": ("twse", "/v1/exchangeReport/STOCK_DAY_ALL"),  # all-listed daily OHLC
    "twse_dividend": ("twse", "/v1/opendata/t187ap45_L"),  # 上市公司股利分派情形
    "tpex_stock_day_all": ("tpex", "/v1/tpex_mainboard_daily_close_quotes"),  # OTC daily quotes
}


class TwseOpenApiClient:
    def __init__(self, config):
        self.config = config
        self.rate_limiter = RateLimiter(requests_per_minute=config.requests_per_minute)

    def fetch(self, endpoint_key: str) -> list[dict[str, Any]]:
        if endpoint_key not in ENDPOINTS:
            raise KeyError(f"Unknown endpoint key {endpoint_key!r}; add it to ENDPOINTS first.")
        exchange, path = ENDPOINTS[endpoint_key]
        base = self.config.twse_base_url if exchange == "twse" else self.config.tpex_base_url
        url = base + path
        data = get_json(
            url,
            rate_limiter=self.rate_limiter,
            max_retries=self.config.max_retries,
            timeout=self.config.timeout_seconds,
        )
        if not isinstance(data, list):
            logger.warning("Unexpected non-list response from %s: %r", url, type(data))
            return []
        return data
