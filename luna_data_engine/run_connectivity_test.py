#!/usr/bin/env python3
"""
LUNA Data Engine — 連線測試（跑POC前先跑這個）

只做一件事：打一次FinMind API，回報清楚的診斷資訊，幾秒鐘內結束。
不寫進資料庫、不重試很多次（預設只重試1次），目的是快速判斷問題出在：
  (a) token沒填或無效
  (b) 網路/DNS連不到FinMind
  (c) 額度已用盡
  (d) 一切正常，可以放心跑run_poc.py

用法：
    python run_connectivity_test.py
    python run_connectivity_test.py --dataset TaiwanStockPrice --data-id 2330 \
        --start-date 2026-09-01 --end-date 2026-09-05
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src import config
from src.finmind_client import FinMindClient, Outcome


def main() -> int:
    parser = argparse.ArgumentParser(description="LUNA Data Engine FinMind連線測試")
    parser.add_argument("--dataset", default="TaiwanStockPrice")
    parser.add_argument("--data-id", default="2330")
    parser.add_argument("--start-date", default="2026-09-01")
    parser.add_argument("--end-date", default="2026-09-05")
    parser.add_argument("--timeout", type=int, default=15, help="單次請求逾時秒數")
    parser.add_argument("--max-retries", type=int, default=1, help="最多重試次數，測試用故意設低")
    args = parser.parse_args()

    print("=== LUNA Data Engine 連線測試 ===")
    settings = config.load_settings()

    if not settings.finmind_token:
        print("❌ FINMIND_API_TOKEN 是空的。")
        print("   請確認 .env 檔案存在且已填入token（複製.env.example為.env後填入）。")
        print("   空token仍可能連得上（部分資料集免費），但額度會更低、部分資料集會直接拒絕，")
        print("   建議先確認token有沒有填對再往下判斷。")
    else:
        masked = settings.finmind_token[:4] + "..." + settings.finmind_token[-4:] \
            if len(settings.finmind_token) > 8 else "(太短，可能有誤)"
        print(f"✓ 讀到token：{masked}")

    print(f"目標：dataset={args.dataset} data_id={args.data_id} "
          f"範圍={args.start_date}~{args.end_date} timeout={args.timeout}s max_retries={args.max_retries}")

    client = FinMindClient(
        base_url=settings.finmind_base_url,
        token=settings.finmind_token,
        requests_per_hour=999999,  # 測試不要被rate limiter拖慢
        timeout_seconds=args.timeout,
        max_retries=args.max_retries,
    )

    t0 = time.time()
    result = client.fetch(args.dataset, args.data_id, args.start_date, args.end_date)
    elapsed = time.time() - t0

    print(f"\n--- 結果（耗時{elapsed:.1f}秒）---")
    print(f"outcome        : {result.outcome.value}")
    print(f"http_status    : {result.http_status}")
    print(f"json_status    : {result.json_status}")
    print(f"msg            : {result.msg}")
    print(f"row_count      : {result.row_count}")
    print(f"attempts       : {result.attempts}")
    print(f"error_detail   : {result.error_detail}")

    print("\n--- 診斷 ---")
    if result.outcome == Outcome.NETWORK_ERROR:
        print("❌ 網路層失敗：這個環境可能連不到 api.finmindtrade.com（DNS/防火牆/白名單問題）。")
        print("   請確認這台機器可以直接用瀏覽器或curl打得到FinMind網站。")
        return 1
    if result.outcome == Outcome.HTTP_ERROR:
        print(f"❌ HTTP層錯誤（status={result.http_status}）：不是額度問題，通常是網址、參數或伺服器端問題。")
        return 1
    if result.outcome == Outcome.API_ERROR:
        print(f"❌ FinMind回了HTTP 200，但JSON內部status={result.json_status}顯示錯誤：{result.msg}")
        print("   常見原因：token無效、額度用盡、或這個dataset需要更高付費層級（例如大戶持股TaiwanStockHoldingSharesPer）。")
        print("   → 如果接下來要跑POC，這個原因不解決的話，320+次請求會每次都重試5次、每次疊加到62秒等待，")
        print("     這就是POC會被環境時間上限中斷的最可能原因，不是FinMind真的連不上。")
        return 1
    if result.outcome == Outcome.PARSE_ERROR:
        print("❌ 回應不是合法JSON，可能是FinMind回了一個HTML錯誤頁（例如維護中或被擋）。")
        return 1
    if result.outcome in (Outcome.SUCCESS, Outcome.SUCCESS_EMPTY):
        print(f"✅ 連線正常，可以放心跑 run_poc.py。（這次拿到{result.row_count}筆資料）")
        return 0

    print(f"⚠️ 未預期的outcome：{result.outcome.value}，請把這次輸出貼給數據師看。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
