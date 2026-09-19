"""Unit tests for FinMindClient's record-key derivation logic, using a
mocked requests.get (no real network) -- covers the newer
institutional_investors (keyed by date+investor-category name) and
margin_trading (keyed by plain date) datasets alongside the
already-long-format statement datasets.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import FinMindConfig
from sources.finmind_client import FinMindClient


class _FakeResponse:
    def __init__(self, json_data):
        self.status_code = 200
        self._json_data = json_data
        self.text = ""

    def json(self):
        return self._json_data


class TestFinMindClientKeys(unittest.TestCase):
    def setUp(self):
        self.client = FinMindClient(FinMindConfig(requests_per_minute=100000))

    @patch("core.http_client.requests.get")
    def test_institutional_investors_keyed_by_date_and_name(self, mock_get):
        mock_get.return_value = _FakeResponse({
            "status": 200, "msg": "ok",
            "data": [
                {"date": "2026-09-18", "stock_id": "2330", "name": "Foreign_Investor", "buy": 100, "sell": 50},
                {"date": "2026-09-18", "stock_id": "2330", "name": "Investment_Trust", "buy": 10, "sell": 5},
            ],
        })
        pairs = self.client.institutional_investors("2330", "2026-06-19")
        keys = [k for k, _ in pairs]
        self.assertEqual(keys, ["2026-09-18|Foreign_Investor", "2026-09-18|Investment_Trust"])

    @patch("core.http_client.requests.get")
    def test_margin_trading_keyed_by_plain_date(self, mock_get):
        mock_get.return_value = _FakeResponse({
            "status": 200, "msg": "ok",
            "data": [{"date": "2026-09-18", "stock_id": "2330", "MarginPurchaseTodayBalance": 1000}],
        })
        pairs = self.client.margin_trading("2330", "2026-06-19")
        self.assertEqual([k for k, _ in pairs], ["2026-09-18"])

    @patch("core.http_client.requests.get")
    def test_start_date_is_forwarded_as_query_param(self, mock_get):
        mock_get.return_value = _FakeResponse({"status": 200, "msg": "ok", "data": []})
        self.client.price("2330", "2026-06-19")
        _, kwargs = mock_get.call_args
        self.assertEqual(kwargs["params"]["start_date"], "2026-06-19")
        self.assertEqual(kwargs["params"]["data_id"], "2330")


if __name__ == "__main__":
    unittest.main()
