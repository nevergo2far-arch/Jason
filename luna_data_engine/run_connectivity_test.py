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
from src.finmind_client import APIResult, Outcome


def diagnose(result: APIResult, max_retries: int) -> tuple[str, int]:
    """
    純函式：只看result的欄位判斷根因，回傳(診斷文字, exit_code)。
    抽成獨立函式是為了能離線單元測試（tests/test_connectivity_diagnosis.py），
    不用真的連FinMind也能驗證「各種失敗情況會不會被正確歸類」。

    重要設計提醒：重試次數用完後，client.fetch()會把result.outcome統一蓋成GAVE_UP，
    但http_status/json_status/msg/error_detail仍保留最後一次嘗試的真實內容。
    所以這裡刻意不用「result.outcome == Outcome.XXX」去比對這些具體類別，
    改成直接看這些原始欄位——不然只要重試次數用完（幾乎每次失敗都會走到這裡），
    下面這些具體診斷分支永遠比對不到，只會掉到最後的「無法明確歸類」。
    """
    lines: list[str] = []

    if result.outcome in (Outcome.SUCCESS, Outcome.SUCCESS_EMPTY):
        lines.append(f"✅ 連線正常，可以放心跑 run_poc.py。（這次拿到{result.row_count}筆資料）")
        return "\n".join(lines), 0

    if result.outcome == Outcome.GAVE_UP:
        lines.append(f"（重試{max_retries}次後仍未成功，以下依最後一次嘗試的回應內容判斷根因）")

    if result.http_status is None:
        lines.append("❌ 網路層失敗：這個環境可能連不到 api.finmindtrade.com（DNS/防火牆/白名單問題）。")
        lines.append(f"   錯誤細節：{result.error_detail}")
        lines.append("   請確認這台機器可以直接用瀏覽器或curl打得到FinMind網站。")
        return "\n".join(lines), 1

    if result.http_status != 200:
        lines.append(f"❌ HTTP層錯誤（status={result.http_status}）：不是額度問題，通常是網址、參數或伺服器端問題。")
        lines.append(f"   錯誤細節：{result.error_detail}")
        return "\n".join(lines), 1

    if result.json_status is not None and result.json_status != 200:
        lines.append(f"❌ FinMind回了HTTP 200，但JSON內部status={result.json_status}顯示錯誤：{result.msg}")
        lines.append("   常見原因：token無效、額度用盡、或這個dataset需要更高付費層級（例如大戶持股TaiwanStockHoldingSharesPer）。")
        lines.append("   → 如果接下來要跑POC，這個原因不解決的話，320+次請求會每次都重試5次、每次疊加到62秒等待，")
        lines.append("     這就是POC會被環境時間上限中斷的最可能原因，不是FinMind真的連不上。")
        lines.append("   → 額度用盡(402)如果是跟其他排程共用同一組token造成的，等額度重置後重跑這支腳本即可，")
        lines.append("     不代表程式碼本身有問題。")
        return "\n".join(lines), 1

    if result.json_status is None:
        lines.append("❌ 回應不是合法JSON，可能是FinMind回了一個HTML錯誤頁（例如維護中或被擋）。")
        lines.append(f"   錯誤細節：{result.error_detail}")
        return "\n".join(lines), 1

    lines.append(f"⚠️ 無法明確歸類根因，原始outcome={result.outcome.value}。以下是完整欄位，請貼給數據師看：")
    lines.append(f"   http_status={result.http_status} json_status={result.json_status} "
                  f"msg={result.msg} error_detail={result.error_detail}")
    return "\n".join(lines), 1


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

    from src.finmind_client import FinMindClient

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
    message, exit_code = diagnose(result, args.max_retries)
    print(message)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
