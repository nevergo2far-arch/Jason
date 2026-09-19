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

# How to decompose each endpoint's whole-market response into
# per-ticker raw_records rows: which field is the ticker code, and
# which field is the natural date key for de-dup. STOCK_DAY_ALL-style
# endpoints return whatever the most recent trading day is -- there is
# no date parameter to request an older day from these particular
# endpoints. date_field=None means the row has no single clean date
# column; the caller builds a composite key instead (see
# run_batch_download.py's market-wide command).
FIELD_MAP: dict[str, dict[str, str | None]] = {
    "twse_stock_day_all": {"ticker_field": "Code", "date_field": "Date"},
    "tpex_stock_day_all": {"ticker_field": "SecuritiesCompanyCode", "date_field": "Date"},
    "twse_dividend": {"ticker_field": "公司代號", "date_field": None},
}


def decompose_market_wide_rows(
    rows: list[dict[str, Any]], ticker_field: str, date_field: str | None
) -> list[tuple[str, str, dict[str, Any]]]:
    """Turn one whole-market API response into (ticker, record_date, row)
    triples ready for Storage.save_record. Pure function (no network,
    no storage) so it's unit-testable on its own; run_batch_download.py
    wires it to a live TwseOpenApiClient.fetch() call.

    When date_field is None (e.g. twse_dividend has no single clean
    date column), falls back to a composite key from 股利年度+期別 --
    good enough for de-dup, not a real date.
    """
    out = []
    for row in rows:
        ticker = str(row.get(ticker_field, "")).strip()
        if not ticker:
            continue
        if date_field:
            record_date = str(row.get(date_field, "")).strip()
        else:
            record_date = f"{row.get('股利年度', '')}-{row.get('期別', '')}".strip()
        if not record_date or record_date == "-":
            continue
        out.append((ticker, record_date, row))
    return out


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
