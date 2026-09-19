"""Unit tests for clean_export.py's pure conversion functions, plus an
end-to-end test over a small synthetic export directory (no network).
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clean_export import (
    CleaningStats,
    classify_ticker,
    clean_dividend_row,
    clean_export_dir,
    clean_stock_day_row,
    roc_date_to_iso,
    to_number,
)


class TestRocDateToIso(unittest.TestCase):
    def test_converts_valid_roc_date(self):
        self.assertEqual(roc_date_to_iso("1150918"), "2026-09-18")

    def test_two_digit_roc_year(self):
        # Guard against assuming the ROC year is always 3 digits.
        # ROC year 99 = 99 + 1911 = Gregorian 2010, not 1990.
        self.assertEqual(roc_date_to_iso("990101"), "2010-01-01")

    def test_blank_and_none_return_none(self):
        self.assertIsNone(roc_date_to_iso(""))
        self.assertIsNone(roc_date_to_iso(None))

    def test_garbage_returns_none_not_raise(self):
        self.assertIsNone(roc_date_to_iso("not-a-date"))

    def test_invalid_month_rejected(self):
        self.assertIsNone(roc_date_to_iso("1151318"))  # month 13


class TestToNumber(unittest.TestCase):
    def test_integer_string(self):
        self.assertEqual(to_number("40892688"), 40892688)
        self.assertIsInstance(to_number("40892688"), int)

    def test_decimal_string(self):
        self.assertEqual(to_number("2460.00"), 2460.0)

    def test_negative_decimal(self):
        self.assertEqual(to_number("-0.0500"), -0.05)

    def test_already_numeric_passthrough(self):
        self.assertEqual(to_number(42), 42)
        self.assertEqual(to_number(3.14), 3.14)

    def test_non_numeric_returned_unchanged(self):
        self.assertEqual(to_number("台積電"), "台積電")

    def test_blank_becomes_none(self):
        self.assertIsNone(to_number(""))

    def test_none_passthrough(self):
        self.assertIsNone(to_number(None))


class TestClassifyTicker(unittest.TestCase):
    def test_common_stock(self):
        self.assertEqual(classify_ticker("2330"), "common_stock")

    def test_etf(self):
        self.assertEqual(classify_ticker("00646"), "etf")

    def test_etf_share_class(self):
        self.assertEqual(classify_ticker("00401A"), "etf_share_class")

    def test_warrant_or_other(self):
        self.assertEqual(classify_ticker("006201"), "warrant_or_other")

    def test_unclassified(self):
        self.assertEqual(classify_ticker("2887Z1"), "unclassified")


class TestCleanStockDayRow(unittest.TestCase):
    def test_twse_row(self):
        row = {
            "Date": "1150918", "Code": "2330", "Name": "台積電",
            "TradeVolume": "40892688", "TradeValue": "100394074139",
            "OpeningPrice": "2460.00", "HighestPrice": "2460.00",
            "LowestPrice": "2435.00", "ClosingPrice": "2460.00",
            "Change": "35.0000", "Transaction": "69571",
        }
        stats = CleaningStats()
        cleaned = clean_stock_day_row(row, "twse_stock_day_all", stats)
        self.assertEqual(cleaned["date_iso"], "2026-09-18")
        self.assertEqual(cleaned["ClosingPrice"], 2460.0)
        self.assertIsInstance(cleaned["TradeVolume"], int)
        self.assertEqual(cleaned["security_type"], "common_stock")
        self.assertEqual(stats.ticker_types["common_stock"], 1)

    def test_tpex_row_uses_its_own_field_names(self):
        row = {
            "Date": "1150918", "SecuritiesCompanyCode": "006201", "CompanyName": "X",
            "Close": "23.25", "Change": "-0.05", "Open": "23.30", "High": "23.40",
            "Low": "23.20", "Average": "23.25", "TradingShares": "32435",
            "TransactionAmount": "755645", "TransactionNumber": "30",
            "LatestBidPrice": "23.20", "LatesAskPrice": "23.30", "Capitals": "0",
            "NextReferencePrice": "0", "NextLimitUp": "0", "NextLimitDown": "0",
        }
        stats = CleaningStats()
        cleaned = clean_stock_day_row(row, "tpex_stock_day_all", stats)
        self.assertEqual(cleaned["Close"], 23.25)
        self.assertEqual(cleaned["security_type"], "warrant_or_other")

    def test_empty_string_becomes_null(self):
        row = {"Date": "1150918", "Code": "2330", "Change": ""}
        stats = CleaningStats()
        cleaned = clean_stock_day_row(row, "twse_stock_day_all", stats)
        self.assertIsNone(cleaned["Change"])


class TestCleanDividendRow(unittest.TestCase):
    def test_dates_and_period_converted(self):
        row = {
            "出表日期": "1150918",
            "公司代號": "2330",
            "股東會日期": "",
            "股利所屬期間": "1141001~1141231",
            "股東配發-盈餘分配之現金股利(元/股)": "6.00003573",
        }
        stats = CleaningStats()
        cleaned = clean_dividend_row(row, "twse_dividend", stats)
        self.assertEqual(cleaned["出表日期_iso"], "2026-09-18")
        self.assertIsNone(cleaned["股東會日期"])
        self.assertNotIn("股東會日期_iso", cleaned)  # blank date never gets a converted field
        self.assertEqual(cleaned["股利所屬期間_start_iso"], "2025-10-01")
        self.assertEqual(cleaned["股利所屬期間_end_iso"], "2025-12-31")
        self.assertEqual(cleaned["股東配發-盈餘分配之現金股利(元/股)"], 6.00003573)


class TestCleanExportDirEndToEnd(unittest.TestCase):
    def test_processes_a_small_synthetic_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_dir = Path(tmp) / "in"
            output_dir = Path(tmp) / "out"
            ticker_dir = input_dir / "2330"
            ticker_dir.mkdir(parents=True)
            (ticker_dir / "twse_stock_day_all.json").write_text(
                json.dumps([{
                    "Date": "1150918", "Code": "2330", "Name": "台積電",
                    "TradeVolume": "40892688", "TradeValue": "100394074139",
                    "OpeningPrice": "2460.00", "HighestPrice": "2460.00",
                    "LowestPrice": "2435.00", "ClosingPrice": "2460.00",
                    "Change": "35.0000", "Transaction": "69571",
                }]),
                encoding="utf-8",
            )

            stats = clean_export_dir(input_dir, output_dir, split_by_type=False)

            self.assertEqual(stats.tickers_processed, 1)
            self.assertEqual(stats.records_in, 1)
            self.assertEqual(stats.records_out, 1)
            self.assertTrue((output_dir / "2330" / "twse_stock_day_all.json").exists())
            self.assertTrue((output_dir / "index.json").exists())
            self.assertTrue((output_dir / "cleaning_report.json").exists())

    def test_split_by_type_separates_into_subfolders(self):
        with tempfile.TemporaryDirectory() as tmp:
            input_dir = Path(tmp) / "in"
            output_dir = Path(tmp) / "out"
            for ticker, code_kind in [("2330", "common_stock"), ("006201", "warrant_or_other")]:
                d = input_dir / ticker
                d.mkdir(parents=True)
                (d / "tpex_stock_day_all.json").write_text(
                    json.dumps([{
                        "Date": "1150918", "SecuritiesCompanyCode": ticker, "CompanyName": "X",
                        "Close": "1.0", "Change": "0", "Open": "1.0", "High": "1.0", "Low": "1.0",
                        "Average": "1.0", "TradingShares": "1", "TransactionAmount": "1",
                        "TransactionNumber": "1", "LatestBidPrice": "1.0", "LatesAskPrice": "1.0",
                        "Capitals": "0", "NextReferencePrice": "0", "NextLimitUp": "0", "NextLimitDown": "0",
                    }]),
                    encoding="utf-8",
                )

            clean_export_dir(input_dir, output_dir, split_by_type=True)

            self.assertTrue((output_dir / "common_stock" / "2330" / "tpex_stock_day_all.json").exists())
            self.assertTrue((output_dir / "warrant_or_other" / "006201" / "tpex_stock_day_all.json").exists())


if __name__ == "__main__":
    unittest.main()
