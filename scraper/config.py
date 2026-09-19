"""Central configuration for the batch downloader.

All tunables live here so behaviour (rate limits, which sources are
enabled, where data lands) can be changed without touching client code.
Values can be overridden with environment variables so the same code
runs safely in different environments (dev laptop vs. CI vs. a
shared box) without editing this file.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
DB_PATH = DATA_DIR / "tw_stock_data.db"
EXPORT_DIR = DATA_DIR / "exports"
LOG_DIR = DATA_DIR / "logs"


def _env_float(name: str, default: float) -> float:
    val = os.environ.get(name)
    return float(val) if val else default


def _env_int(name: str, default: int) -> int:
    val = os.environ.get(name)
    return int(val) if val else default


def _env_bool(name: str, default: bool) -> bool:
    val = os.environ.get(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class FinMindConfig:
    # Free tier works without a token but has a low hourly quota;
    # register at https://finmindtrade.com/ and set FINMIND_TOKEN to
    # raise the quota substantially. Never hardcode the token here.
    base_url: str = field(
        default_factory=lambda: os.environ.get(
            "FINMIND_BASE_URL", "https://api.finmindtrade.com/api/v4/data"
        )
    )
    token: str = field(default_factory=lambda: os.environ.get("FINMIND_TOKEN", ""))
    # Conservative default well under FinMind's documented free-tier
    # request budget; raise via env once you've confirmed your own
    # token's quota.
    requests_per_minute: float = _env_float("FINMIND_RPM", 50)
    max_retries: int = _env_int("FINMIND_MAX_RETRIES", 4)
    timeout_seconds: float = _env_float("FINMIND_TIMEOUT", 20)


@dataclass(frozen=True)
class TwseOpenApiConfig:
    # Official, unauthenticated, no ToS restriction on automated
    # access for these open-data endpoints.
    # Note: no "/v1" suffix here — ENDPOINTS paths in
    # sources/twse_openapi_client.py already include it. A live smoke
    # test (GitHub Actions, this sandbox has no internet) caught that
    # having it in both places silently worked for TWSE (their server
    # redirects /v1/v1/... -> /v1/...) but hard-failed for TPEx
    # (HTTP 520 on the literal doubled path) — so this is the one
    # form that's confirmed correct for both.
    twse_base_url: str = "https://openapi.twse.com.tw"
    tpex_base_url: str = "https://www.tpex.org.tw/openapi"
    requests_per_minute: float = _env_float("TWSE_OPENAPI_RPM", 30)
    max_retries: int = _env_int("TWSE_OPENAPI_MAX_RETRIES", 4)
    timeout_seconds: float = _env_float("TWSE_OPENAPI_TIMEOUT", 20)


@dataclass(frozen=True)
class MopsConfig:
    # MOPS itself has no documented public REST API for per-company
    # filing detail; these query endpoints are the ones its own web
    # UI calls. Treat as "public data, but be a good citizen": low
    # rate, honest UA, respect robots.txt (checked at runtime).
    base_url: str = "https://mopsov.twse.com.tw/mops/web"
    enabled: bool = _env_bool("MOPS_DETAIL_ENABLED", True)
    requests_per_minute: float = _env_float("MOPS_RPM", 12)
    max_retries: int = _env_int("MOPS_MAX_RETRIES", 3)
    timeout_seconds: float = _env_float("MOPS_TIMEOUT", 20)
    user_agent: str = os.environ.get(
        "MOPS_USER_AGENT",
        "tw-stock-research-bot/1.0 (personal research; contact: set MOPS_CONTACT_EMAIL env var)",
    )


@dataclass(frozen=True)
class GoodinfoConfig:
    """Disabled by default on purpose.

    goodinfo.tw's terms of service prohibit automated
    collection/crawling, and the site runs bot detection. This client
    deliberately implements no evasion (no fingerprint spoofing, no
    mouse-movement simulation, no CAPTCHA solving, no proxy rotation)
    — only a slow, honest, robots.txt-respecting fetcher. Enabling it
    is a ToS-risk decision for whoever runs this code; it is opt-in
    per run via GOODINFO_ENABLED=1, never on by default.
    """

    base_url: str = "https://goodinfo.tw"
    enabled: bool = _env_bool("GOODINFO_ENABLED", False)
    # Deliberately slow: one request per this many seconds, sequential
    # only. Do not lower this to "go faster" — that is precisely the
    # kind of change this module exists to resist.
    min_delay_seconds: float = _env_float("GOODINFO_MIN_DELAY_SECONDS", 12.0)
    max_requests_per_run: int = _env_int("GOODINFO_MAX_REQUESTS_PER_RUN", 50)
    max_retries: int = _env_int("GOODINFO_MAX_RETRIES", 2)
    timeout_seconds: float = _env_float("GOODINFO_TIMEOUT", 20)
    user_agent: str = os.environ.get(
        "GOODINFO_USER_AGENT",
        "tw-stock-research-bot/1.0 (personal research; contact: set GOODINFO_CONTACT_EMAIL env var)",
    )


@dataclass(frozen=True)
class Config:
    finmind: FinMindConfig = field(default_factory=FinMindConfig)
    twse_openapi: TwseOpenApiConfig = field(default_factory=TwseOpenApiConfig)
    mops: MopsConfig = field(default_factory=MopsConfig)
    goodinfo: GoodinfoConfig = field(default_factory=GoodinfoConfig)
    data_dir: Path = DATA_DIR
    db_path: Path = DB_PATH
    export_dir: Path = EXPORT_DIR
    log_dir: Path = LOG_DIR


CONFIG = Config()
