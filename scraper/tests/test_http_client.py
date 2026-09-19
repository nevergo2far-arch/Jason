"""Unit tests for core/http_client.py's retry behavior, using a fake
`requests.get` (no real network) so Cloudflare-style failures modes
found in live CI runs (a 520 status; a 200 response whose body isn't
valid JSON) can be reproduced deterministically.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.http_client import HttpError, get_json
from core.rate_limiter import RateLimiter


class _FakeResponse:
    def __init__(self, status_code: int, json_data=None, text: str = "", raises: bool = False):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text
        self._raises = raises

    def json(self):
        if self._raises:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._json_data


def _no_op_sleep(*_args, **_kwargs):
    pass


class TestRetryBehavior(unittest.TestCase):
    def setUp(self):
        self.rate_limiter = RateLimiter()  # no delay configured, tests stay fast

    @patch("core.http_client.time.sleep", _no_op_sleep)
    @patch("core.http_client.requests.get")
    def test_520_is_retried_then_succeeds(self, mock_get):
        mock_get.side_effect = [
            _FakeResponse(520, text="<html>cloudflare error</html>"),
            _FakeResponse(200, json_data={"ok": True}),
        ]
        result = get_json("https://example.test", rate_limiter=self.rate_limiter, max_retries=3, timeout=5)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(mock_get.call_count, 2)

    @patch("core.http_client.time.sleep", _no_op_sleep)
    @patch("core.http_client.requests.get")
    def test_non_json_200_body_is_retried_then_succeeds(self, mock_get):
        mock_get.side_effect = [
            _FakeResponse(200, raises=True, text="<html>interstitial</html>"),
            _FakeResponse(200, json_data={"ok": True}),
        ]
        result = get_json("https://example.test", rate_limiter=self.rate_limiter, max_retries=3, timeout=5)
        self.assertEqual(result, {"ok": True})
        self.assertEqual(mock_get.call_count, 2)

    @patch("core.http_client.time.sleep", _no_op_sleep)
    @patch("core.http_client.requests.get")
    def test_exhausts_retries_and_raises_http_error(self, mock_get):
        mock_get.return_value = _FakeResponse(520, text="still down")
        with self.assertRaises(HttpError):
            get_json("https://example.test", rate_limiter=self.rate_limiter, max_retries=2, timeout=5)
        self.assertEqual(mock_get.call_count, 2)

    @patch("core.http_client.requests.get")
    def test_non_retryable_4xx_raises_immediately_no_retry(self, mock_get):
        mock_get.return_value = _FakeResponse(404, text="not found")
        with self.assertRaises(HttpError):
            get_json("https://example.test", rate_limiter=self.rate_limiter, max_retries=3, timeout=5)
        self.assertEqual(mock_get.call_count, 1)  # never retried, unlike the 5xx/520 cases above


if __name__ == "__main__":
    unittest.main()
