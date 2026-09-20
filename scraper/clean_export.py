#!/usr/bin/env python3
"""Clean a data/exports/ directory (or a zip of one) produced by
run_batch_download.py's `export` command.

Built after inspecting a real export from the whole-market smoke test
and finding several concrete data-quality problems -- this isn't
speculative cleaning, each transform below fixes something actually
observed:

1. **ROC/Minguo calendar dates as bare digit strings.** TWSE/TPEx
   OpenAPI returns dates like "1150918" (ROC year 115 = 2026, month
   09, day 18) with no separators and no indication it isn't a plain
   number. Every such field gets a matching "..._iso" field added
   (e.g. Date -> date_iso "2026-09-18"); the original field is kept
   so nothing is lost.
2. **Numbers stored as strings.** TradeVolume, ClosingPrice, and
   similar fields from TWSE/TPEx arrive as JSON strings ("2460.00",
   not 2460.00). Converted to real int/float where the whole field is
   numeric-looking; conversion failures are recorded in the report
   instead of silently coercing to something wrong.
3. **The stock-day-all ticker universe is not just common stocks.**
   In a real export, ~79% of all distinct tickers seen were 6-digit
   codes (warrants), with the rest split between 4-digit common
   stocks, 5-digit and digit+letter ETF codes. Mixing these into one
   undifferentiated "stock" dataset is itself the data-quality
   problem for anyone doing per-company analysis. Every stock-day-all
   record gets a `security_type` field
   (common_stock / etf / warrant_or_other); pass --split-by-type to
   also physically separate the output into subfolders.
4. **Empty strings instead of missing/null.** e.g. 股東會日期 is ""
   when a shareholder meeting hasn't been scheduled yet. Converted to
   JSON null so a downstream consumer doesn't have to special-case
   both "" and null.

FinMind's own datasets (cash_flow_statement, monthly_revenue, ...)
were already well-typed (real JSON numbers, ISO dates) in the export
inspected while building this -- they only get the empty-string-to-
null pass, not a guessed numeric/date conversion, since guessing
wrong there would be worse than doing nothing.

Usage:
    python clean_export.py --input data/exports --output data/exports_clean
    python clean_export.py --input scraper-smoke-test-output.zip --output cleaned/
    python clean_export.py --input data/exports --output data/exports_clean --split-by-type
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import tempfile
import zipfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

# -- pure, unit-testable conversion helpers -----------------------------

_ROC_DATE_RE = re.compile(r"^(\d{2,3})(\d{2})(\d{2})$")

TICKER_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("common_stock", re.compile(r"^\d{4}$")),
    ("etf", re.compile(r"^\d{5}$")),
    ("etf_share_class", re.compile(r"^\d{4,6}[A-Z]$")),
    ("warrant_or_other", re.compile(r"^\d{6}$")),
]


def classify_ticker(code: str) -> str:
    code = (code or "").strip()
    for label, pattern in TICKER_PATTERNS:
        if pattern.match(code):
            return label
    return "unclassified"


def roc_date_to_iso(value: Any) -> str | None:
    """"1150918" (ROC 民國115年09月18日) -> "2026-09-18". Returns None
    for blank/unparseable input rather than raising -- callers decide
    whether that's worth a warning."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    m = _ROC_DATE_RE.match(s)
    if not m:
        return None
    roc_year, month, day = m.groups()
    try:
        gregorian_year = int(roc_year) + 1911
        iso = f"{gregorian_year:04d}-{month}-{day}"
        # Reject obviously-invalid calendar dates (e.g. month 13).
        if not (1 <= int(month) <= 12 and 1 <= int(day) <= 31):
            return None
        return iso
    except ValueError:
        return None


def to_number(value: Any) -> Any:
    """Best-effort string -> int/float. Returns the original value
    unchanged if it doesn't look like a plain number (never guesses)."""
    if value is None or isinstance(value, (int, float)):
        return value
    s = str(value).strip()
    if not s:
        return None
    try:
        if re.fullmatch(r"[+-]?\d+", s):
            return int(s)
        return float(s)
    except ValueError:
        return value


def clean_value(value: Any) -> Any:
    """Universal pass applied to every field of every dataset: trim
    whitespace, turn "" into None. Never touches non-string values."""
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return value


# -- dataset-specific row cleaners --------------------------------------

# TWSE and TPEx use entirely different field names for the same
# concepts (checked against a real export -- not assumed symmetric).
_STOCK_DAY_NUMERIC_FIELDS_BY_DATASET = {
    "twse_stock_day_all": [
        "TradeVolume", "TradeValue", "OpeningPrice", "HighestPrice",
        "LowestPrice", "ClosingPrice", "Change", "Transaction",
    ],
    "tpex_stock_day_all": [
        "Close", "Change", "Open", "High", "Low", "Average", "TradingShares",
        "TransactionAmount", "TransactionNumber", "LatestBidPrice", "LatesAskPrice",
        "Capitals", "NextReferencePrice", "NextLimitUp", "NextLimitDown",
    ],
}
_STOCK_DAY_TICKER_FIELD_BY_DATASET = {
    "twse_stock_day_all": "Code",
    "tpex_stock_day_all": "SecuritiesCompanyCode",
}

_DIVIDEND_DATE_FIELDS = ["出表日期", "董事會（擬議）股利分派日", "股東會日期"]

# Known placeholder tokens the exchanges use for "not applicable /
# not yet available" -- found in the real export this script was
# built against (TPEx uses "---" for a halted/no-trade security's
# OHLC; a dividend date field of "0" means not yet resolved). Treated
# as a clean None, not a conversion failure worth warning about.
_NUMERIC_PLACEHOLDER_TOKENS = {"---", "--", "n/a", "na"}
_DATE_PLACEHOLDER_TOKENS = {"0", "00000000", "0000000"}


def clean_stock_day_row(row: dict[str, Any], dataset: str, stats: "CleaningStats") -> dict[str, Any]:
    cleaned = {k: clean_value(v) for k, v in row.items()}
    if cleaned.get("Date"):
        iso = roc_date_to_iso(cleaned["Date"])
        cleaned["date_iso"] = iso
        if iso is None:
            stats.record_warning(dataset, f"unparseable ROC date: {cleaned['Date']!r}")
        else:
            stats.dates_converted += 1
    for field in _STOCK_DAY_NUMERIC_FIELDS_BY_DATASET.get(dataset, []):
        if field in cleaned and cleaned[field] is not None:
            before = cleaned[field]
            if str(before).strip().lower() in _NUMERIC_PLACEHOLDER_TOKENS:
                cleaned[field] = None  # e.g. "---": no trade that day, not a parse failure
                continue
            cleaned[field] = to_number(before)
            if isinstance(cleaned[field], (int, float)):
                stats.numbers_converted += 1
            elif before is not None:
                stats.record_warning(dataset, f"non-numeric {field}: {before!r}")

    ticker_field = _STOCK_DAY_TICKER_FIELD_BY_DATASET[dataset]
    security_type = classify_ticker(str(row.get(ticker_field, "")))
    cleaned["security_type"] = security_type
    stats.ticker_types[security_type] += 1
    return cleaned


def clean_dividend_row(row: dict[str, Any], dataset: str, stats: "CleaningStats") -> dict[str, Any]:
    cleaned = {k: clean_value(v) for k, v in row.items()}
    for field in _DIVIDEND_DATE_FIELDS:
        if cleaned.get(field):
            if str(cleaned[field]).strip() in _DATE_PLACEHOLDER_TOKENS:
                cleaned[f"{field}_iso"] = None  # e.g. "0": not yet resolved, not a parse failure
                continue
            iso = roc_date_to_iso(cleaned[field])
            cleaned[f"{field}_iso"] = iso
            if iso is None:
                stats.record_warning(dataset, f"unparseable ROC date in {field}: {cleaned[field]!r}")
            else:
                stats.dates_converted += 1

    period = cleaned.get("股利所屬期間")
    if period and "~" in period:
        start, end = (p.strip() for p in period.split("~", 1))
        cleaned["股利所屬期間_start_iso"] = roc_date_to_iso(start)
        cleaned["股利所屬期間_end_iso"] = roc_date_to_iso(end)

    for field, value in list(cleaned.items()):
        if value is None or field.endswith("_iso"):
            continue
        if "(元" in field or "(股" in field:
            converted = to_number(value)
            if isinstance(converted, (int, float)):
                cleaned[field] = converted
                stats.numbers_converted += 1
            else:
                stats.record_warning(dataset, f"non-numeric {field}: {value!r}")
    return cleaned


def clean_generic_row(row: dict[str, Any], dataset: str, stats: "CleaningStats") -> dict[str, Any]:
    return {k: clean_value(v) for k, v in row.items()}


def clean_dataset_records(records: list[dict[str, Any]], dataset: str, stats: "CleaningStats") -> list[dict[str, Any]]:
    if dataset in _STOCK_DAY_TICKER_FIELD_BY_DATASET:
        return [clean_stock_day_row(r, dataset, stats) for r in records]
    if dataset == "twse_dividend":
        return [clean_dividend_row(r, dataset, stats) for r in records]
    return [clean_generic_row(r, dataset, stats) for r in records]


# -- stats / report -------------------------------------------------------

class CleaningStats:
    def __init__(self) -> None:
        self.tickers_processed = 0
        self.datasets_processed: Counter[str] = Counter()
        self.records_in = 0
        self.records_out = 0
        self.dates_converted = 0
        self.numbers_converted = 0
        self.ticker_types: Counter[str] = Counter()
        self._warnings: defaultdict[str, list[str]] = defaultdict(list)
        self._warning_cap_per_dataset = 10

    def record_warning(self, dataset: str, message: str) -> None:
        bucket = self._warnings[dataset]
        if len(bucket) < self._warning_cap_per_dataset:
            bucket.append(message)
        elif len(bucket) == self._warning_cap_per_dataset:
            bucket.append("... further warnings for this dataset suppressed")

    def to_report_dict(self) -> dict[str, Any]:
        return {
            "tickers_processed": self.tickers_processed,
            "records_in": self.records_in,
            "records_out": self.records_out,
            "dates_converted_to_iso": self.dates_converted,
            "numbers_converted_from_string": self.numbers_converted,
            "datasets_processed": dict(self.datasets_processed),
            "ticker_security_type_breakdown": dict(self.ticker_types),
            "warnings_by_dataset": {k: v for k, v in self._warnings.items()},
        }


# -- I/O -------------------------------------------------------------------

def _write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    seen = set()
    for rec in records:
        for k in rec.keys():
            if k not in seen:
                seen.add(k)
                fieldnames.append(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for rec in records:
            writer.writerow(rec)


def clean_export_dir(input_dir: Path, output_dir: Path, split_by_type: bool) -> CleaningStats:
    stats = CleaningStats()
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_tickers: dict[str, Any] = {}

    for ticker_dir in sorted(p for p in input_dir.iterdir() if p.is_dir()):
        ticker = ticker_dir.name
        stats.tickers_processed += 1
        ticker_datasets: dict[str, Any] = {}

        for json_path in sorted(ticker_dir.glob("*.json")):
            dataset = json_path.stem
            try:
                records = json.loads(json_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                stats.record_warning(dataset, f"{ticker}: could not parse {json_path.name}, skipped")
                continue
            if not isinstance(records, list):
                continue

            stats.records_in += len(records)
            stats.datasets_processed[dataset] += len(records)
            cleaned = clean_dataset_records(records, dataset, stats)
            stats.records_out += len(cleaned)

            if split_by_type and dataset in _STOCK_DAY_TICKER_FIELD_BY_DATASET:
                for rec in cleaned:
                    subfolder = rec.get("security_type", "unclassified")
                    dest_dir = output_dir / subfolder / ticker
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    existing = []
                    dest_json = dest_dir / f"{dataset}.json"
                    if dest_json.exists():
                        existing = json.loads(dest_json.read_text(encoding="utf-8"))
                    existing.append(rec)
                    dest_json.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
                    _write_csv(dest_dir / f"{dataset}.csv", existing)
            else:
                dest_dir = output_dir / ticker
                dest_dir.mkdir(parents=True, exist_ok=True)
                (dest_dir / f"{dataset}.json").write_text(
                    json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                _write_csv(dest_dir / f"{dataset}.csv", cleaned)

            ticker_datasets[dataset] = {"record_count": len(cleaned)}

        if ticker_datasets:
            manifest_tickers[ticker] = {"datasets": ticker_datasets}

    (output_dir / "index.json").write_text(
        json.dumps({"tickers": manifest_tickers}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    report = stats.to_report_dict()
    (output_dir / "cleaning_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return stats


def _format_report_markdown(stats: CleaningStats) -> str:
    r = stats.to_report_dict()
    lines = [
        "# 資料清洗報告",
        "",
        f"- 處理股票/證券代號數：{r['tickers_processed']}",
        f"- 輸入筆數：{r['records_in']}，輸出筆數：{r['records_out']}",
        f"- 民國曆日期轉換為 ISO 8601：{r['dates_converted_to_iso']} 筆欄位",
        f"- 字串型數字轉換為數值型別：{r['numbers_converted_from_string']} 筆欄位",
        "",
        "## 各資料集筆數",
    ]
    for ds, count in sorted(r["datasets_processed"].items()):
        lines.append(f"- `{ds}`：{count}")
    lines.append("")
    lines.append("## 證券代號分類（僅日成交資料集）")
    for t, count in sorted(r["ticker_security_type_breakdown"].items(), key=lambda kv: -kv[1]):
        lines.append(f"- {t}：{count}")
    if r["warnings_by_dataset"]:
        lines.append("")
        lines.append("## 清洗過程中的警告（無法解析的欄位，原樣保留未轉換）")
        for ds, warnings in r["warnings_by_dataset"].items():
            lines.append(f"### {ds}")
            for w in warnings:
                lines.append(f"- {w}")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, help="A data/exports directory, or a .zip of one.")
    parser.add_argument("--output", required=True, help="Directory to write cleaned output into.")
    parser.add_argument(
        "--split-by-type", action="store_true",
        help="Also physically separate stock-day-all output into common_stock/etf/warrant_or_other subfolders.",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    output_dir = Path(args.output)

    if input_path.is_file() and input_path.suffix == ".zip":
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with zipfile.ZipFile(input_path) as zf:
                zf.extractall(tmp_path)
            stats = clean_export_dir(tmp_path, output_dir, args.split_by_type)
    else:
        stats = clean_export_dir(input_path, output_dir, args.split_by_type)

    report_md = _format_report_markdown(stats)
    (output_dir / "cleaning_report.md").write_text(report_md, encoding="utf-8")
    print(report_md)


if __name__ == "__main__":
    main()
