#!/usr/bin/env python3
"""
LUNA Data Engine — POC 執行入口

用法：
    python run_connectivity_test.py     # 建議先跑這個，幾秒鐘確認token/連線沒問題
    python run_poc.py
    python run_poc.py --end-date 2026-09-18     # 預設就是這個值，通常不用特別指定
    python run_poc.py --max-minutes 20           # 軟性時間上限，預設20分鐘
    python run_poc.py --force                    # 忽略progress紀錄，全部重抓

v1.1更新（2026-09-20，依實測回饋修正）：
  - 中斷/例外時，report/status一律照樣輸出目前為止的結果，不會什麼都沒留下。
  - 結束日改用固定預設值，不再悄悄跟著「今天」漂移。
  - 新增 --max-minutes：快到時間上限時，downloader會優雅停止排新請求，
    已完成的部分正常落地、正常出報告，不會被環境強制砍斷。

POC完成（或中斷）後都會在 reports/ 產出一份 Markdown 品質報告，以及 reports/poc_status.json
（給 run_full.py 檢查POC是否過關用，過關的定義見下方 PASS_THRESHOLD）。
`passed` 欄位在中斷時一律是 false，並會多一個 `interrupted: true` 標記，
不會把中斷的結果誤標成通過。
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

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

PASS_THRESHOLD = 0.90


def main() -> int:
    parser = argparse.ArgumentParser(description="LUNA Data Engine POC 下載與品質檢查")
    parser.add_argument("--end-date", default=config.DEFAULT_POC_END_DATE, help="結束日 YYYY-MM-DD")
    parser.add_argument("--force", action="store_true", help="忽略已完成的progress紀錄，全部重抓")
    parser.add_argument(
        "--max-minutes",
        type=float,
        default=config.DEFAULT_MAX_DURATION_SECONDS / 60,
        help="軟性時間上限（分鐘），快到時downloader會優雅停止，不會被環境強制砍斷",
    )
    args = parser.parse_args()

    config.ensure_dirs()
    run_name = f"poc_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
    logger = setup_logging(config.LOG_DIR, run_name)

    settings = config.load_settings()
    if not settings.finmind_token:
        logger.warning(
            "FINMIND_API_TOKEN 是空的。強烈建議先跑 `python run_connectivity_test.py` 確認token/連線狀況，"
            "否則大量請求在token失效下會不斷重試，很容易撞到執行環境的時間上限。"
        )

    datasets_cfg = config.load_datasets_config()
    stocks = config.load_poc_stock_pool()
    end_date = args.end_date

    logger.info(
        f"=== POC開始 === 股票池{len(stocks)}檔，日期範圍 {config.POC_START_DATE} ~ {end_date}，"
        f"時間上限{args.max_minutes:.0f}分鐘"
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
            start_date=config.POC_START_DATE,
            end_date=end_date,
            logger=logger,
            force=args.force,
            max_duration_seconds=args.max_minutes * 60,
        )
    except KeyboardInterrupt:
        interrupted = True
        interrupt_reason = "使用者中斷(KeyboardInterrupt)"
        logger.error(f"下載被中斷：{interrupt_reason}。將依目前資料庫內容產出報告，不會沒有任何輸出。")
    except Exception as e:  # noqa: BLE001 — 這裡故意攔所有例外，確保報告一定會產出
        interrupted = True
        interrupt_reason = f"{type(e).__name__}: {e}"
        logger.error(f"下載過程發生未預期例外：{interrupt_reason}")
        logger.error(traceback.format_exc())
        logger.error("將依目前資料庫內容產出報告，不會沒有任何輸出。")

    logger.info(f"下載階段結束（interrupted={interrupted}），outcome統計：{outcome_counts}")

    # 不管上面有沒有中斷，都用目前資料庫內容產一份報告——這是這次修正的核心：
    # 中斷不再等於「什麼都沒有」，至少能看到跑到哪裡、卡在哪裡。
    dataset_names = list(datasets_cfg["datasets"].keys())
    results = run_full_quality_check(conn, dataset_names, stocks)
    download_summary = summarize_download_log(conn)
    failed = list_failed_requests(conn)
    availability = data_availability_table(conn)

    report_md = render_markdown_report(run_name, results, download_summary, failed, availability)
    if interrupted:
        report_md = (
            f"> ⚠️ **本次執行被中斷**（原因：{interrupt_reason}），以下是中斷前已下載資料的品質報告，"
            f"**不代表POC已完成或通過**。\n\n" + report_md
        )
    report_path = config.REPORT_DIR / f"{run_name}_quality_report.md"
    report_path.write_text(report_md, encoding="utf-8")
    availability.to_csv(config.REPORT_DIR / f"{run_name}_data_availability.csv", index=False, encoding="utf-8-sig")
    failed.to_csv(config.REPORT_DIR / f"{run_name}_failed_requests.csv", index=False, encoding="utf-8-sig")

    total = int(download_summary["n"].sum()) if not download_summary.empty else 0
    success = (
        int(download_summary[download_summary["outcome"].isin(["success", "success_empty"])]["n"].sum())
        if not download_summary.empty
        else 0
    )
    success_rate = (success / total) if total else 0.0
    passed = (not interrupted) and success_rate >= PASS_THRESHOLD

    status = {
        "run_name": run_name,
        "end_date": end_date,
        "total_requests": total,
        "success_requests": success,
        "success_rate": round(success_rate, 4),
        "passed": passed,
        "interrupted": interrupted,
        "interrupt_reason": interrupt_reason,
        "pass_threshold": PASS_THRESHOLD,
        "generated_at": datetime.utcnow().isoformat(),
        "report_path": str(report_path),
    }
    (config.REPORT_DIR / "poc_status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    logger.info(f"總請求數={total}，成功={success}，成功率={success_rate:.1%}")
    logger.info(f"POC {'通過' if passed else '未通過'}{'（因中斷，不計入判斷）' if interrupted else ''}")
    logger.info(f"品質報告：{report_path}")

    if interrupted:
        logger.error(
            "本次POC被中斷，未完成。建議：先跑 `python run_connectivity_test.py` 排除token/連線問題，"
            "再用較短的 --max-minutes（例如5分鐘）小規模測試，確認能穩定跑完一輪後再拉長時間。"
        )
    elif not passed:
        logger.error(
            "POC未通過門檻，請先檢查 reports/*_failed_requests.csv 找出失敗原因"
            "（常見：token沒填、額度用盡、某資料集tier不對），修正後再重跑，"
            "不建議在這個狀態下直接跑run_full.py。"
        )

    conn.close()
    return 1 if (interrupted or not passed) else 0


if __name__ == "__main__":
    sys.exit(main())
