"""
LUNA Data Engine — Logging設定

兩條log分開看：
- engine.log：完整流程訊息(含debug)，開發時看這個
- error.log：只留warning以上，出事故時看這個，不會被大量成功訊息淹沒
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path


def setup_logging(log_dir: Path, run_name: str) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("luna_data_engine")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    engine_file = logging.FileHandler(
        log_dir / f"{run_name}_engine.log", encoding="utf-8"
    )
    engine_file.setLevel(logging.DEBUG)
    engine_file.setFormatter(fmt)
    logger.addHandler(engine_file)

    error_file = logging.FileHandler(
        log_dir / f"{run_name}_error.log", encoding="utf-8"
    )
    error_file.setLevel(logging.WARNING)
    error_file.setFormatter(fmt)
    logger.addHandler(error_file)

    return logger
