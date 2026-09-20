"""Runtime robots.txt check, used by the MOPS and Goodinfo clients.

This can't be verified from this sandbox (no general internet
egress here), so treat it as "fail closed": if robots.txt can't be
fetched or parsed, callers should default to NOT crawling rather than
assuming permission.
"""
from __future__ import annotations

import urllib.robotparser
from urllib.parse import urljoin


class RobotsCheck:
    def __init__(self, base_url: str, user_agent: str, http_get):
        """`http_get` is injected (a callable(url) -> text) instead of
        importing requests directly here, so this stays independently
        testable without a network call."""
        self.base_url = base_url
        self.user_agent = user_agent
        self._http_get = http_get
        self._parser: urllib.robotparser.RobotFileParser | None = None
        self._load_failed = False

    def _ensure_loaded(self) -> None:
        if self._parser is not None or self._load_failed:
            return
        robots_url = urljoin(self.base_url, "/robots.txt")
        try:
            text = self._http_get(robots_url)
        except Exception:
            self._load_failed = True
            return
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(text.splitlines())
        self._parser = parser

    def can_fetch(self, path: str) -> bool:
        self._ensure_loaded()
        if self._parser is None:
            # robots.txt unreachable/unparseable -> fail closed
            return False
        url = urljoin(self.base_url, path)
        return self._parser.can_fetch(self.user_agent, url)
