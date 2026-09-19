"""Unit tests for the network-free core modules: rate limiter,
storage/checkpoint, and the robots.txt check. No live HTTP is used —
robots.txt fetches are stubbed via dependency injection.

Run with: python -m unittest discover -s scraper/tests -v
(from the repo root, with scraper/ on sys.path — see the sys.path
insert below).
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.rate_limiter import RateLimiter, RunBudget, RunBudgetExceeded
from core.robots import RobotsCheck
from core.storage import Storage
from sources.twse_openapi_client import decompose_market_wide_rows


class TestRateLimiter(unittest.TestCase):
    def test_no_delay_configured_returns_immediately(self):
        rl = RateLimiter()
        start = time.monotonic()
        rl.wait()
        rl.wait()
        self.assertLess(time.monotonic() - start, 0.05)

    def test_min_delay_seconds_enforced(self):
        rl = RateLimiter(min_delay_seconds=0.2)
        start = time.monotonic()
        rl.wait()  # first call never sleeps
        rl.wait()  # second call must wait out the remaining interval
        elapsed = time.monotonic() - start
        self.assertGreaterEqual(elapsed, 0.19)

    def test_requests_per_minute_converted_to_interval(self):
        rl = RateLimiter(requests_per_minute=600)  # -> 0.1s interval
        self.assertAlmostEqual(rl.min_interval, 0.1, places=3)


class TestRunBudget(unittest.TestCase):
    def test_raises_once_exhausted(self):
        budget = RunBudget(max_requests=2)
        budget.consume()
        budget.consume()
        with self.assertRaises(RunBudgetExceeded):
            budget.consume()


class TestRobotsCheck(unittest.TestCase):
    def test_fails_closed_when_robots_unreachable(self):
        def broken_get(url):
            raise ConnectionError("simulated network failure")

        rc = RobotsCheck("https://example.test", "test-bot", broken_get)
        self.assertFalse(rc.can_fetch("/anything"))

    def test_respects_disallow(self):
        robots_txt = "User-agent: *\nDisallow: /private/\nAllow: /public/\n"

        def fake_get(url):
            return robots_txt

        rc = RobotsCheck("https://example.test", "test-bot", fake_get)
        self.assertFalse(rc.can_fetch("/private/page"))
        self.assertTrue(rc.can_fetch("/public/page"))


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "test.db"
        self.storage = Storage(self.db_path)

    def tearDown(self):
        self.storage.close()
        self.tmpdir.cleanup()

    def test_upsert_stock_and_list(self):
        self.storage.upsert_stock("2330", "台積電", "TWSE", "半導體業", "twse")
        self.storage.upsert_stock("6488", "環球晶", "TWSE", "半導體業", "twse")
        self.assertEqual(self.storage.list_tickers(), ["2330", "6488"])

    def test_upsert_stock_is_idempotent_update(self):
        self.storage.upsert_stock("2330", "old name", "TWSE", "x", "twse")
        self.storage.upsert_stock("2330", "台積電", "TWSE", "半導體業", "twse")
        row = self.storage.conn.execute("SELECT name FROM stocks WHERE ticker='2330'").fetchone()
        self.assertEqual(row["name"], "台積電")
        self.assertEqual(len(self.storage.list_tickers()), 1)

    def test_export_all_includes_tickers_not_in_stocks_table(self):
        # Regression test: a market-wide source can return tickers
        # (warrants, ETF share classes, ...) that were never in the
        # stocks table via upsert_stock/stock-list. export_all() must
        # still export their data, not silently drop it.
        self.storage.upsert_stock("2330", "台積電", "TWSE", "半導體業", "twse")
        self.storage.save_record("twse_openapi", "twse_stock_day_all", "2330", "2026-09-18", {"Code": "2330"})
        self.storage.save_record("twse_openapi", "tpex_stock_day_all", "006201", "2026-09-18", {"Code": "006201"})
        self.storage.conn.commit()

        with tempfile.TemporaryDirectory() as tmp:
            export_dir = Path(tmp)
            result = self.storage.export_all(export_dir)
            self.assertIn("006201", result)
            self.assertTrue((export_dir / "006201" / "tpex_stock_day_all.json").exists())

    def test_save_and_get_records_roundtrip(self):
        self.storage.save_record("finmind", "monthly_revenue", "2330", "2024-01", {"revenue": 100})
        self.storage.save_record("finmind", "monthly_revenue", "2330", "2024-02", {"revenue": 120})
        self.storage.conn.commit()
        records = self.storage.get_records("2330", "monthly_revenue")
        self.assertEqual([r["revenue"] for r in records], [100, 120])

    def test_save_record_upsert_overwrites_same_key(self):
        self.storage.save_record("finmind", "monthly_revenue", "2330", "2024-01", {"revenue": 100})
        self.storage.save_record("finmind", "monthly_revenue", "2330", "2024-01", {"revenue": 999})
        self.storage.conn.commit()
        records = self.storage.get_records("2330", "monthly_revenue")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["revenue"], 999)

    def test_checkpoint_resume_flow(self):
        jobs = [("2330", "cash_flow_statement", "finmind"), ("2330", "monthly_revenue", "finmind")]
        self.storage.init_checkpoint_jobs(jobs)
        self.assertEqual(self.storage.checkpoint_summary(), {"pending": 2})

        pending = self.storage.pending_jobs(source="finmind")
        self.assertEqual(len(pending), 2)

        self.storage.mark_checkpoint("2330", "cash_flow_statement", "finmind", "done")
        self.assertEqual(self.storage.checkpoint_summary(), {"pending": 1, "done": 1})

        # Re-queueing the same jobs (simulating a resumed run) must not
        # reset the already-done job back to pending.
        self.storage.init_checkpoint_jobs(jobs)
        self.assertEqual(self.storage.checkpoint_summary(), {"pending": 1, "done": 1})

    def test_checkpoint_error_then_retry(self):
        self.storage.init_checkpoint_jobs([("2330", "dividend", "finmind")])
        self.storage.mark_checkpoint("2330", "dividend", "finmind", "error", "boom")
        summary = self.storage.checkpoint_summary()
        self.assertEqual(summary.get("error"), 1)
        row = self.storage.conn.execute(
            "SELECT attempts, last_error FROM checkpoint WHERE ticker='2330'"
        ).fetchone()
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["last_error"], "boom")

    def test_export_ticker_writes_json_and_csv(self):
        self.storage.upsert_stock("2330", "台積電", "TWSE", "半導體業", "twse")
        self.storage.save_record("finmind", "monthly_revenue", "2330", "2024-01", {"revenue_year": 2024, "revenue_month": 1, "revenue": 100})
        self.storage.save_record("finmind", "monthly_revenue", "2330", "2024-02", {"revenue_year": 2024, "revenue_month": 2, "revenue": 120})
        self.storage.conn.commit()

        export_dir = Path(self.tmpdir.name) / "exports"
        counts = self.storage.export_ticker("2330", export_dir)
        self.assertEqual(counts, {"monthly_revenue": 2})

        json_path = export_dir / "2330" / "monthly_revenue.json"
        csv_path = export_dir / "2330" / "monthly_revenue.csv"
        self.assertTrue(json_path.exists())
        self.assertTrue(csv_path.exists())

        import json as _json
        data = _json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(len(data), 2)

    def test_build_manifest(self):
        self.storage.upsert_stock("2330", "台積電", "TWSE", "半導體業", "twse")
        self.storage.save_record("finmind", "dividend", "2330", "2023", {"CashEarningsDistribution": 3.0})
        self.storage.conn.commit()

        export_dir = Path(self.tmpdir.name) / "exports"
        manifest_path = self.storage.build_manifest(export_dir)
        self.assertTrue(manifest_path.exists())

        import json as _json
        manifest = _json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertIn("2330", manifest["tickers"])
        self.assertEqual(manifest["tickers"]["2330"]["name"], "台積電")
        self.assertEqual(manifest["tickers"]["2330"]["datasets"]["dividend"]["record_count"], 1)


class TestDecomposeMarketWideRows(unittest.TestCase):
    def test_date_field_present(self):
        rows = [
            {"Date": "20260918", "Code": "2330", "ClosingPrice": "600"},
            {"Date": "20260918", "Code": "2454", "ClosingPrice": "1200"},
        ]
        triples = decompose_market_wide_rows(rows, ticker_field="Code", date_field="Date")
        self.assertEqual(
            [(t, d) for t, d, _ in triples],
            [("2330", "20260918"), ("2454", "20260918")],
        )

    def test_composite_key_when_no_date_field(self):
        rows = [{"公司代號": "2330", "股利年度": "2025", "期別": "第2季"}]
        triples = decompose_market_wide_rows(rows, ticker_field="公司代號", date_field=None)
        self.assertEqual(triples, [("2330", "2025-第2季", rows[0])])

    def test_rows_missing_ticker_are_skipped(self):
        rows = [{"Date": "20260918", "Code": "", "ClosingPrice": "600"}, {"Date": "20260918", "Code": "2330"}]
        triples = decompose_market_wide_rows(rows, ticker_field="Code", date_field="Date")
        self.assertEqual(len(triples), 1)
        self.assertEqual(triples[0][0], "2330")


if __name__ == "__main__":
    unittest.main()
