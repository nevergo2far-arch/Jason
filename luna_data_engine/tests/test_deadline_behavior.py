#!/usr/bin/env python3
"""
離線測試：驗證 v1.1 新增的「軟性時間上限」機制。

背景：LUNA實測回報POC被環境強制中斷、什麼報告都沒留下。根因分析：如果大量請求持續
API_ERROR，每個請求都跑滿重試+backoff，累積時間很容易超過環境給的執行上限。
這支測試驗證兩件事：
  1. run_download 收到 max_duration_seconds 後，真的會在時間到之前停止排新請求，
     而不是無限跑下去。
  2. 已經處理過的請求，即使在deadline之後才被中止，資料仍然正確落地
     （不會因為中途停止就整批遺失）。

跑法：
    python tests/test_deadline_behavior.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import storage
from src.downloader import ProgressStore, run_download
from src.finmind_client import FinMindClient
from src.logger_setup import setup_logging

TMP_DB = Path(__file__).parent / "_deadline_test.db"
TMP_LOG_DIR = Path(__file__).parent / "_deadline_test_logs"
TMP_RAW = Path(__file__).parent / "_deadline_test_raw"
TMP_PROGRESS = Path(__file__).parent / "_deadline_test_progress.json"


def slow_transport(base_url: str, params: dict, timeout: int) -> tuple[int, str]:
    """每次呼叫都刻意花0.3秒，模擬有一堆請求要打，用來驗證deadline會提早喊停。"""
    time.sleep(0.3)
    import json as _json

    dataset = params["dataset"]
    if dataset == "TaiwanStockInfo":
        return 200, _json.dumps({"status": 200, "msg": "success", "data": [
            {"stock_id": "2330", "stock_name": "台積電", "industry_category": "半導體業", "type": "twse", "date": "2026-09-20"}
        ]})
    return 200, _json.dumps({
        "status": 200, "msg": "success",
        "data": [{"date": params.get("start_date"), "stock_id": params.get("data_id"), "close": 100.0,
                   "open": 99.0, "max": 101.0, "min": 98.0, "Trading_Volume": 1000}],
    })


def main() -> None:
    for p in (TMP_DB, TMP_PROGRESS):
        if p.exists():
            p.unlink()

    logger = setup_logging(TMP_LOG_DIR, "deadline_test")
    conn = storage.get_connection(TMP_DB)
    storage.init_db(conn)
    progress = ProgressStore(TMP_PROGRESS)

    client = FinMindClient(
        base_url="https://fake.local/api",
        token="TEST_TOKEN",
        requests_per_hour=100000,
        timeout_seconds=5,
        max_retries=1,
        transport=slow_transport,
        logger=logger,
        sleep_fn=lambda s: None,
    )

    datasets_cfg = {
        "datasets": {
            "TaiwanStockInfo": {"scope": "all_stocks"},
            "TaiwanStockPrice": {"scope": "single_stock_free"},
            "TaiwanStockPriceAdj": {"scope": "single_stock_free"},
        }
    }
    # 10檔股票 x 2個dataset x 4年chunk(2023~2026) = 80次請求，每次0.3秒 = 理論上要跑24秒
    stocks = [{"stock_id": str(2000 + i), "name": f"測試股{i}"} for i in range(10)]

    print("=== 測試：deadline=1秒，理應遠早於80次請求跑完前就停止 ===")
    t0 = time.monotonic()
    outcome_counts = run_download(
        client=client, conn=conn, raw_dir=TMP_RAW, progress=progress,
        datasets_cfg=datasets_cfg, stocks=stocks,
        start_date="2023-01-01", end_date="2026-09-18",
        logger=logger, force=False, max_duration_seconds=1.0,
    )
    elapsed = time.monotonic() - t0
    total_done = sum(outcome_counts.values())

    print(f"  耗時={elapsed:.2f}秒, 完成請求數={total_done}, outcome_counts={outcome_counts}")
    assert elapsed < 10, f"deadline應該讓程式在遠早於80次請求跑完(24秒)前停止，實際耗時{elapsed:.2f}秒"
    assert 0 < total_done < 80, f"應該只完成部分請求，不是0筆也不是全部80筆，實際完成{total_done}筆"
    print("  ✓ deadline機制正確：時間到之前提早停止，沒有跑完全部80次請求")

    # 驗證已完成的部分確實有落地到DB，不是被deadline中止就整批消失
    row_count = conn.execute('SELECT COUNT(*) FROM download_log').fetchone()[0]
    assert row_count == total_done, f"download_log筆數({row_count})應該等於完成的請求數({total_done})"
    print(f"  ✓ 已完成的{row_count}筆請求都正確寫進download_log，deadline中止不影響已完成部分")

    clean_rows = conn.execute('SELECT COUNT(*) FROM "clean_TaiwanStockPrice"').fetchone()[0] \
        if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='clean_TaiwanStockPrice'").fetchone() \
        else 0
    print(f"  ✓ clean_TaiwanStockPrice已寫入{clean_rows}筆（部分成功結果正常落地，不是全有全無）")

    print("\n=== 測試：不設deadline時，全部80+次請求應該正常跑完 ===")
    conn2 = storage.get_connection(Path(__file__).parent / "_deadline_test_nolimlit.db")
    if (Path(__file__).parent / "_deadline_test_nolimlit.db").exists():
        pass
    storage.init_db(conn2)
    progress2 = ProgressStore(Path(__file__).parent / "_deadline_test_nolimit_progress.json")
    client2 = FinMindClient(
        base_url="https://fake.local/api", token="TEST_TOKEN", requests_per_hour=100000,
        timeout_seconds=5, max_retries=1, transport=slow_transport, logger=logger, sleep_fn=lambda s: None,
    )
    outcome_counts2 = run_download(
        client=client2, conn=conn2, raw_dir=TMP_RAW, progress=progress2,
        datasets_cfg=datasets_cfg, stocks=stocks[:2],  # 縮小規模讓測試跑快一點：2股x2dataset x4chunk=16次
        start_date="2023-01-01", end_date="2026-09-18",
        logger=logger, force=False, max_duration_seconds=None,
    )
    total2 = sum(outcome_counts2.values())
    expected = 1 + 2 * 2 * 4  # TaiwanStockInfo(1) + 2股票 x 2資料集 x 4年chunk
    assert total2 == expected, f"沒設deadline應該跑完全部{expected}次請求，實際完成{total2}次"
    print(f"  ✓ 沒設deadline時正確跑完全部{total2}次請求（{outcome_counts2}）")

    conn.close()
    conn2.close()
    print("\n全部deadline機制測試通過。")


if __name__ == "__main__":
    main()
