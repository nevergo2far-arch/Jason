#!/usr/bin/env python3
"""
離線pipeline驗證（不連網路）

這個沙盒環境連不到api.finmindtrade.com，所以沒辦法在這裡真正跑一次POC下載。
這支腳本改用「本次對話已經用WebFetch實際呼叫過FinMind API拿到的真實回應」
（見 tests/fixtures/README_fixtures.md）當作假的transport回傳內容，
目的是驗證 finmind_client → storage → quality_checks 這條pipeline的邏輯本身沒有bug：
- JSON status/msg的雙重檢查有沒有正確攔下錯誤（用api_error_quota.json測試，且驗證重試次數）
- 正常回應會不會正確寫進clean表與data_availability
- 品質報告產不產得出來

跑法：
    python tests/test_offline_pipeline.py

這不是「已在真實環境驗證過下載成功」的證明，只是「程式邏輯用真實資料格式測過一遍」。
真正的POC仍須在能連上FinMind的環境跑 run_poc.py。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import storage
from src.finmind_client import FinMindClient, Outcome
from src.logger_setup import setup_logging
from src.quality_checks import (
    build_trading_calendar,
    data_availability_table,
    list_failed_requests,
    render_markdown_report,
    run_full_quality_check,
    summarize_download_log,
)

FIXTURES = Path(__file__).parent / "fixtures"
TMP_DB = Path(__file__).parent / "_offline_test.db"
TMP_LOG_DIR = Path(__file__).parent / "_offline_test_logs"
TMP_RAW = Path(__file__).parent / "_offline_test_raw"

# dataset -> fixture 檔名 的對照，模擬「打這個dataset的API就回傳這個檔案的內容」
DATASET_FIXTURE = {
    "TaiwanStockInfo": "taiwan_stock_info.json",
    "TaiwanStockInstitutionalInvestorsBuySell": "institutional_investors.json",
    "TaiwanStockFinancialStatements": "financial_statements.json",
    "TaiwanStockBalanceSheet": "balance_sheet.json",
    "TaiwanStockCashFlowsStatement": "cash_flow.json",
    "TaiwanStockDividend": "dividend.json",
}


class FakeTransportError(Exception):
    pass


def make_mock_transport(fail_first_n_calls_for: dict[str, int]):
    """
    回傳一個符合Transport介面的函式：依dataset查fixture檔回傳(200, json_text)。
    fail_first_n_calls_for: {dataset: N} 表示這個dataset前N次呼叫要回傳額度錯誤，
    用來測試retry邏輯真的會重試、而且最後成功時結果是對的。
    """
    call_counts: dict[str, int] = {}

    def transport(base_url: str, params: dict, timeout: int) -> tuple[int, str]:
        dataset = params["dataset"]
        call_counts[dataset] = call_counts.get(dataset, 0) + 1

        fail_n = fail_first_n_calls_for.get(dataset, 0)
        if call_counts[dataset] <= fail_n:
            text = (FIXTURES / "api_error_quota.json").read_text(encoding="utf-8")
            return 200, text

        fixture_name = DATASET_FIXTURE.get(dataset)
        if fixture_name is None:
            return 200, json.dumps({"status": 200, "msg": "success", "data": []})
        text = (FIXTURES / fixture_name).read_text(encoding="utf-8")
        return 200, text

    return transport, call_counts


def main() -> None:
    for p in (TMP_DB,):
        if p.exists():
            p.unlink()

    logger = setup_logging(TMP_LOG_DIR, "offline_test")

    # 測試1：TaiwanStockInstitutionalInvestorsBuySell 前2次回傳額度錯誤，第3次(在max_retries=5內)才成功
    # → 驗證retry邏輯 + 驗證「HTTP200但status錯誤」不會被誤判成功
    transport, call_counts = make_mock_transport(
        fail_first_n_calls_for={"TaiwanStockInstitutionalInvestorsBuySell": 2}
    )

    client = FinMindClient(
        base_url="https://fake.local/api",
        token="TEST_TOKEN",
        requests_per_hour=100000,  # 測試時不要被rate limiter拖慢
        timeout_seconds=5,
        max_retries=5,
        transport=transport,
        logger=logger,
        sleep_fn=lambda s: None,  # 測試不要真的sleep
    )

    conn = storage.get_connection(TMP_DB)
    storage.init_db(conn)

    print("=== 測試1：額度錯誤重試後成功 ===")
    result = client.fetch("TaiwanStockInstitutionalInvestorsBuySell", "2330", "2026-09-01", "2026-09-01")
    assert result.outcome == Outcome.SUCCESS, f"預期SUCCESS，實際{result.outcome}"
    assert result.attempts == 3, f"預期第3次嘗試才成功，實際attempts={result.attempts}"
    assert result.row_count == 2, f"預期2筆資料，實際{result.row_count}"
    print(f"  outcome={result.outcome.value}, attempts={result.attempts}, row_count={result.row_count} ✓")
    storage.log_download(conn, result)
    storage.save_raw_response(conn, TMP_RAW, result)
    storage.upsert_clean_table(conn, result.dataset, result.data)
    storage.update_data_availability(conn, result.dataset, "2330", result.data)

    print("\n=== 測試2：一直額度錯誤，超過max_retries仍應標記為GAVE_UP，不當成功也不當空資料 ===")
    transport2, _ = make_mock_transport(fail_first_n_calls_for={"TaiwanStockPrice": 999})
    client2 = FinMindClient(
        base_url="https://fake.local/api",
        token="TEST_TOKEN",
        requests_per_hour=100000,
        timeout_seconds=5,
        max_retries=3,
        transport=transport2,
        logger=logger,
        sleep_fn=lambda s: None,
    )
    result2 = client2.fetch("TaiwanStockPrice", "2330", "2026-09-01", "2026-09-01")
    assert result2.outcome == Outcome.GAVE_UP, f"預期GAVE_UP，實際{result2.outcome}"
    assert result2.is_usable is False
    assert result2.row_count == 0
    print(f"  outcome={result2.outcome.value}, is_usable={result2.is_usable} ✓（正確標記為失敗，不是空資料）")
    storage.log_download(conn, result2)

    print("\n=== 測試3：其餘正常資料集一次成功，寫入clean表 ===")
    for dataset in ["TaiwanStockInfo", "TaiwanStockFinancialStatements", "TaiwanStockBalanceSheet",
                     "TaiwanStockCashFlowsStatement", "TaiwanStockDividend"]:
        transport3, _ = make_mock_transport(fail_first_n_calls_for={})
        client3 = FinMindClient(
            base_url="https://fake.local/api", token="TEST_TOKEN", requests_per_hour=100000,
            timeout_seconds=5, max_retries=3, transport=transport3, logger=logger, sleep_fn=lambda s: None,
        )
        data_id = None if dataset == "TaiwanStockInfo" else "2330"
        r = client3.fetch(dataset, data_id, "2025-01-01", "2026-09-18")
        assert r.outcome == Outcome.SUCCESS, f"{dataset} 預期SUCCESS，實際{r.outcome}"
        storage.log_download(conn, r)
        storage.save_raw_response(conn, TMP_RAW, r)
        storage.upsert_clean_table(conn, r.dataset, r.data)
        if data_id:
            storage.update_data_availability(conn, r.dataset, data_id, r.data)
        print(f"  {dataset}: outcome={r.outcome.value}, row_count={r.row_count} ✓")

    print("\n=== 測試4：重複寫入同一批資料應該去重，不會筆數翻倍 ===")
    before = conn.execute('SELECT COUNT(*) FROM "clean_TaiwanStockDividend"').fetchone()[0]
    dividend_data = json.loads((FIXTURES / "dividend.json").read_text(encoding="utf-8"))["data"]
    storage.upsert_clean_table(conn, "TaiwanStockDividend", dividend_data)
    after = conn.execute('SELECT COUNT(*) FROM "clean_TaiwanStockDividend"').fetchone()[0]
    assert before == after, f"去重失敗：重複寫入前{before}筆，寫入後變成{after}筆"
    print(f"  重複寫入前後筆數皆為{after} ✓（自然鍵去重生效）")

    print("\n=== 測試5：品質報告產得出來 ===")
    calendar = build_trading_calendar(conn)
    results = run_full_quality_check(
        conn,
        list(DATASET_FIXTURE.keys()),
        [{"stock_id": "2330", "name": "台積電", "sector": "半導體"}],
    )
    summary = summarize_download_log(conn)
    failed = list_failed_requests(conn)
    availability = data_availability_table(conn)
    report_md = render_markdown_report("offline_test", results, summary, failed, availability)
    assert "LUNA Data Engine 資料品質報告" in report_md
    assert len(results) > 0
    print(f"  品質報告長度={len(report_md)}字元，涵蓋{len(results)}筆(dataset,stock)組合 ✓")
    report_path = Path(__file__).parent / "_offline_test_report.md"
    report_path.write_text(report_md, encoding="utf-8")
    print(f"  已寫出 {report_path}（可打開檢查格式）")

    conn.close()
    print("\n全部離線測試通過。")


if __name__ == "__main__":
    main()
