"""FinMind official API client (https://finmindtrade.com/).

This is the primary, low-risk data source for this project: FinMind
is built and documented specifically for programmatic access to
Taiwan market data (it wraps and normalizes TWSE/TPEx/MOPS-sourced
data), publishes an official free-tier quota, and explicitly expects
automated use — unlike goodinfo.tw. No anti-detection logic is
needed or present here.

Dataset names below match FinMind's documented API v4
(https://finmindtrade.com/analysis/#/data/api) as of this writing.
Verify against current docs if any call starts returning empty data —
FinMind occasionally renames or splits datasets.
"""
from __future__ import annotations

from typing import Any

from core.http_client import get_json
from core.logger import get_logger
from core.rate_limiter import RateLimiter

logger = get_logger(__name__)


class FinMindApiError(RuntimeError):
    """A FinMind response with a non-success status and no data --
    most commonly the free-tier hourly quota being exhausted. Raised
    rather than swallowed so the batch runner's checkpoint records
    the job as failed (retryable next run) instead of falsely "done"
    with zero records."""

# FinMind's long-format statement datasets return one row per
# (date, line-item type) rather than one row per period — so the
# natural dedup key is the pair, not the date alone.
_LONG_FORMAT_DATASETS = {
    "TaiwanStockFinancialStatements",
    "TaiwanStockBalanceSheet",
    "TaiwanStockCashFlowsStatement",
}

# Institutional buy/sell is also long-format, but discriminated by
# `name` (the investor category: 外資, 投信, 自營商自行, ...) rather
# than `type` -- one row per (date, investor category).
_LONG_FORMAT_BY_NAME_DATASETS = {
    "TaiwanStockInstitutionalInvestorsBuySell",
}


class FinMindClient:
    def __init__(self, config):
        self.config = config
        self.rate_limiter = RateLimiter(requests_per_minute=config.requests_per_minute)

    def _fetch(self, dataset: str, *, data_id: str | None = None, start_date: str | None = None, end_date: str | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"dataset": dataset}
        if data_id:
            params["data_id"] = data_id
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        if self.config.token:
            params["token"] = self.config.token

        body = get_json(
            self.config.base_url,
            params=params,
            rate_limiter=self.rate_limiter,
            max_retries=self.config.max_retries,
            timeout=self.config.timeout_seconds,
        )
        if not isinstance(body, dict) or "data" not in body:
            raise ValueError(f"Unexpected FinMind response shape for dataset={dataset}: {body!r}")
        data = body["data"]
        if body.get("status") not in (200, "200", None):
            if not data:
                # An error status with no data is most often quota
                # exhaustion -- FinMind returns this as a normal JSON
                # body (no error HTTP status the retry layer would
                # catch), so silently treating it as "0 real records"
                # would mark the checkpoint job done and it would
                # never be retried once quota resets. Raise instead.
                raise FinMindApiError(
                    f"FinMind dataset={dataset} data_id={data_id} status={body.get('status')} msg={body.get('msg')}"
                )
            logger.warning(
                "FinMind dataset=%s data_id=%s returned status=%s msg=%s (had %d records anyway, continuing)",
                dataset, data_id, body.get("status"), body.get("msg"), len(data),
            )
        return data

    # -- market-wide -----------------------------------------------------
    def fetch_stock_list(self) -> list[dict[str, Any]]:
        """All TWSE + TPEx listed securities (dataset: TaiwanStockInfo).
        Used to build the full-market ticker universe for batch runs."""
        return self._fetch("TaiwanStockInfo")

    # -- per-ticker, mapped onto our storage schema -----------------------
    def fetch_dataset_for_ticker(self, dataset: str, ticker: str, start_date: str = "2000-01-01") -> list[tuple[str, dict[str, Any]]]:
        """Returns [(record_date_key, payload), ...] ready for Storage.save_record."""
        records = self._fetch(dataset, data_id=ticker, start_date=start_date)
        out = []
        for rec in records:
            if dataset in _LONG_FORMAT_DATASETS:
                key = f"{rec.get('date')}|{rec.get('type')}"
            elif dataset in _LONG_FORMAT_BY_NAME_DATASETS:
                key = f"{rec.get('date')}|{rec.get('name')}"
            elif dataset == "TaiwanStockMonthRevenue":
                key = f"{rec.get('revenue_year')}-{int(rec.get('revenue_month', 0)):02d}"
            else:
                key = str(rec.get("date", ""))
            if not key or key in ("|", "None"):
                continue
            out.append((key, rec))
        return out

    # Convenience wrappers used by the batch runner's dataset registry.
    def cash_flow_statement(self, ticker: str, start_date: str = "2000-01-01"):
        return self.fetch_dataset_for_ticker("TaiwanStockCashFlowsStatement", ticker, start_date)

    def income_statement(self, ticker: str, start_date: str = "2000-01-01"):
        return self.fetch_dataset_for_ticker("TaiwanStockFinancialStatements", ticker, start_date)

    def balance_sheet(self, ticker: str, start_date: str = "2000-01-01"):
        return self.fetch_dataset_for_ticker("TaiwanStockBalanceSheet", ticker, start_date)

    def monthly_revenue(self, ticker: str, start_date: str = "2000-01-01"):
        return self.fetch_dataset_for_ticker("TaiwanStockMonthRevenue", ticker, start_date)

    def dividend(self, ticker: str, start_date: str = "2000-01-01"):
        return self.fetch_dataset_for_ticker("TaiwanStockDividend", ticker, start_date)

    def price(self, ticker: str, start_date: str = "2000-01-01"):
        return self.fetch_dataset_for_ticker("TaiwanStockPrice", ticker, start_date)

    # -- 籌碼面 (chip/institutional-flow) datasets --------------------------
    def institutional_investors(self, ticker: str, start_date: str = "2000-01-01"):
        """三大法人（外資/投信/自營商）買賣超, one row per (date, category)."""
        return self.fetch_dataset_for_ticker("TaiwanStockInstitutionalInvestorsBuySell", ticker, start_date)

    def margin_trading(self, ticker: str, start_date: str = "2000-01-01"):
        """融資融券餘額, one row per date."""
        return self.fetch_dataset_for_ticker("TaiwanStockMarginPurchaseShortSale", ticker, start_date)


# dataset key -> FinMind client method name, used by the batch runner
# to build its job matrix generically.
FINMIND_DATASETS: dict[str, str] = {
    "cash_flow_statement": "cash_flow_statement",
    "income_statement": "income_statement",
    "balance_sheet": "balance_sheet",
    "monthly_revenue": "monthly_revenue",
    "dividend": "dividend",
    "price": "price",
    "institutional_investors": "institutional_investors",
    "margin_trading": "margin_trading",
}
