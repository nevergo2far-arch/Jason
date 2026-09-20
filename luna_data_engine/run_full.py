#!/usr/bin/env python3
"""
LUNA Data Engine — 正式全量下載入口

執行前會先檢查 reports/poc_status.json 是否存在且 passed=true，
這是把「通過POC品質驗證後，才擴大至完整研究股票池」這條規則寫進程式，
不是靠人記得先跑POC。要跳過檢查（例如你已經手動確認過POC報告但門檻算法不合理），
用 --skip-poc-check 並自行負責。

股票池預設讀 config/stock_pool_full.yaml；這個skeleton沒有內建~1970檔的完整清單
（那份清單最新狀態應該動態抓TaiwanStockInfo，不要寫死在檔案裡免得過期），
如果該檔案不存在，會直接用POC的10檔並印警告。

v1.1更新（2026-09-20）：全量下載請求數遠多於POC，撞到執行環境時間上限的風險更高，
所以套用跟run_poc.py一樣的邏輯：中斷/例外時仍照樣輸出目前為止的品質報告，
並支援 --max-minutes 分段執行——長時間下載本來就建議切成多次執行，
每次都用 `python run_full.py` 重跑，靠progress.json自動接續未完成的部分。
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime

from src import config, storage
from src.downloader import ProgressStore, run_download
from src.finmind_client import FinMindClient
from src.logger_setup import setup_logging
from src.quality_checks import (
    data_availability_table,
    list_failed_requests,
    render_markdown_report,
    run_full_quality_check,
    summarize_download_log,
)


def load_full_stock_pool(logger) -> list[dict[str, str]]:
    full_path = config.CONFIG_DIR / "stock_pool_full.yaml"
    if full_path.exists():
        import yaml

        with open(full_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        return cfg["stocks"]
    logger.warning(
        "找不到 config/stock_pool_full.yaml，暫時沿用POC的10檔股票池。"
        "完整股票池建議先跑一次 TaiwanStockInfo 下載，從裡面篩選要研究的股票清單再另存成這個檔案，"
        "不要手動key~1970檔。"
    )
    return config.load_poc_stock_pool()


def main() -> int:
    parser = argparse.ArgumentParser(description="LUNA Data Engine 正式全量下載")
    parser.add_argument("--end-date", default=config.DEFAULT_POC_END_DATE, help="結束日 YYYY-MM-DD")
    parser.add_argument("--force", action="store_true", help="忽略已完成的progress紀錄，全部重抓")
    parser.add_argument(
        "--skip-poc-check", action="store_true", help="跳過POC通過檢查（不建議，除非你很清楚在做什麼）"
    )
    parser.add_argument(
        "--max-minutes",
        type=float,
        default=None,
        help="軟性時間上限（分鐘）。全量資料量大，建議分段執行時設定（例如30），"
             "不設就是跑到全部完成或被環境中斷為止（中斷一樣會產出目前為止的報告）",
    )
    args = parser.parse_args()

    config.ensure_dirs()
    run_name = f"full_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    logger = setup_logging(config.LOG_DIR, run_name)

    status_path = config.REPORT_DIR / "poc_status.json"
    if not args.skip_poc_check:
        if not status_path.exists():
            logger.error("找不到 reports/poc_status.json，請先執行 python run_poc.py。中止。")
            return 1
        status = json.loads(status_path.read_text(encoding="utf-8"))
        if not status.get("passed"):
            if status.get("interrupted"):
                logger.error(
                    f"最近一次POC被中斷（原因：{status.get('interrupt_reason')}），尚未真正完成。"
                    f"請先跑 `python run_connectivity_test.py` 排除問題，再重跑 `python run_poc.py`。中止。"
                )
            else:
                logger.error(
                    f"最近一次POC未通過（成功率{status.get('success_rate'):.1%}，"
                    f"門檻{status.get('pass_threshold'):.0%}）。請先解決POC失敗原因再跑全量下載。中止。"
                )
            return 1
        logger.info(
            f"POC檢查通過（{status.get('run_name')}，成功率{status.get('success_rate'):.1%}），繼續執行全量下載。"
        )

    settings = config.load_settings()
    datasets_cfg = config.load_datasets_config()
    stocks = load_full_stock_pool(logger)
    end_date = args.end_date

    logger.info(
        f"=== 全量下載開始 === 股票池{len(stocks)}檔，日期範圍 {config.FULL_START_DATE} ~ {end_date}"
        + (f"，時間上限{args.max_minutes:.0f}分鐘" if args.max_minutes else "，無時間上限")
    )

    client = FinMindClient(
        base_url=settings.finmind_base_url,
        token=settings.finmind_token,
        requests_per_hour=settings.requests_per_hour,
        timeout_seconds=settings.timeout_seconds,
        max_retries=settings.max_retries,
        logger=logger,
    )

    conn = storage.get_connection(config.DB_PATH)
    storage.init_db(conn)
    progress = ProgressStore(config.PROGRESS_PATH)

    interrupted = False
    interrupt_reason = None
    outcome_counts: dict[str, int] = {}

    try:
        outcome_counts = run_download(
            client=client,
            conn=conn,
            raw_dir=config.RAW_DIR,
            progress=progress,
            datasets_cfg=datasets_cfg,
            stocks=stocks,
            start_date=config.FULL_START_DATE,
            end_date=end_date,
            logger=logger,
            force=args.force,
            max_duration_seconds=(args.max_minutes * 60) if args.max_minutes else None,
        )
    except KeyboardInterrupt:
        interrupted = True
        interrupt_reason = "使用者中斷(KeyboardInterrupt)"
        logger.error(f"下載被中斷：{interrupt_reason}。將依目前資料庫內容產出報告。")
    except Exception as e:  # noqa: BLE001
        interrupted = True
        interrupt_reason = f"{type(e).__name__}: {e}"
        logger.error(f"下載過程發生未預期例外：{interrupt_reason}")
        logger.error(traceback.format_exc())
        logger.error("將依目前資料庫內容產出報告。")

    logger.info(f"下載階段結束（interrupted={interrupted}），outcome統計：{outcome_counts}")

    dataset_names = list(datasets_cfg["datasets"].keys())
    results = run_full_quality_check(conn, dataset_names, stocks)
    download_summary = summarize_download_log(conn)
    failed = list_failed_requests(conn, limit=200)
    availability = data_availability_table(conn)

    report_md = render_markdown_report(run_name, results, download_summary, failed, availability)
    if interrupted:
        report_md = (
            f"> ⚠️ **本次執行被中斷**（原因：{interrupt_reason}），以下是中斷前已下載資料的品質報告。"
            f"重新執行 `python run_full.py` 會自動接續未完成的部分。\n\n" + report_md
        )
    report_path = config.REPORT_DIR / f"{run_name}_quality_report.md"
    report_path.write_text(report_md, encoding="utf-8")
    availability.to_csv(config.REPORT_DIR / f"{run_name}_data_availability.csv", index=False, encoding="utf-8-sig")
    failed.to_csv(config.REPORT_DIR / f"{run_name}_failed_requests.csv", index=False, encoding="utf-8-sig")

    logger.info(f"品質報告：{report_path}")
    logger.info("提醒：本次執行不含單條件回測、模型訓練、投資評分、買賣訊號產生——那些在下一階段。")
    if interrupted:
        logger.info("下載尚未全部完成，重新執行 `python run_full.py` 即可從中斷處接續。")

    conn.close()
    return 1 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
