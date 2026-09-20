"""
LUNA Data Engine — 設定載入

集中讀取 .env 與 config/*.yaml，其他模組一律透過這裡取得設定，
不要在別的地方各自 os.environ.get()，避免設定值到處分散、難以稽核。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_DIR = PROJECT_ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
LOG_DIR = PROJECT_ROOT / "logs"
REPORT_DIR = PROJECT_ROOT / "reports"
DB_PATH = DATA_DIR / "luna_data_engine.db"
PROGRESS_PATH = DATA_DIR / "download_progress.json"

# 日期範圍（LUNA 2026-09-20決策）
POC_START_DATE = "2023-01-01"
FULL_START_DATE = "2019-01-01"
# LUNA原始指示的POC結束日是固定的2026-09-18，不是「執行當天」。
# 之前預設用date.today()會讓每次重跑的範圍悄悄跟著日期漂移，不利於重現結果，
# 所以改成預設用這個固定值；要抓到「今天」還是可以用 --end-date $(date +%F) 明確指定。
DEFAULT_POC_END_DATE = "2026-09-18"

# 單次執行的軟性時間上限（秒）。實測發現如果FinMind token失效或某dataset需要付費層級，
# 320+次請求每次都跑滿重試+backoff會很容易撞到執行環境自己的時間上限，程式被砍斷、
# 什麼報告都沒留下。這裡給一個保守預設值，讓downloader在快到時間前主動優雅停下。
DEFAULT_MAX_DURATION_SECONDS = 20 * 60  # 20分鐘


@dataclass
class Settings:
    finmind_token: str = ""
    finmind_base_url: str = "https://api.finmindtrade.com/api/v4/data"
    requests_per_hour: int = 550
    timeout_seconds: int = 20
    max_retries: int = 5


def load_settings(env_path: Path | None = None) -> Settings:
    load_dotenv(dotenv_path=env_path or (PROJECT_ROOT / ".env"))
    return Settings(
        finmind_token=os.environ.get("FINMIND_API_TOKEN", ""),
        finmind_base_url=os.environ.get(
            "FINMIND_BASE_URL", "https://api.finmindtrade.com/api/v4/data"
        ),
        requests_per_hour=int(os.environ.get("FINMIND_REQUESTS_PER_HOUR", "550")),
        timeout_seconds=int(os.environ.get("FINMIND_TIMEOUT_SECONDS", "20")),
        max_retries=int(os.environ.get("FINMIND_MAX_RETRIES", "5")),
    )


def load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_datasets_config() -> dict[str, Any]:
    return load_yaml("datasets.yaml")


def load_poc_stock_pool() -> list[dict[str, str]]:
    cfg = load_yaml("stock_pool_poc.yaml")
    return cfg["poc_stocks"]


def ensure_dirs() -> None:
    for d in (DATA_DIR, RAW_DIR, LOG_DIR, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
