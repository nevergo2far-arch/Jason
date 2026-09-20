#!/usr/bin/env python3
"""
離線測試：驗證 run_connectivity_test.py 的 diagnose() 純函式。

背景（工程師實測回報的bug）：finmind_client.fetch() 在重試次數用完後，會把
result.outcome 統一蓋成 Outcome.GAVE_UP，但 http_status/json_status/msg/error_detail
仍保留最後一次嘗試的真實內容。修正前的診斷分支用「result.outcome == Outcome.API_ERROR」
之類的比對，永遠比對不到（因為outcome早就被蓋成GAVE_UP了），只會落到最後的
「無法明確歸類根因」訊息——不管實際上是402額度用盡、還是500伺服器錯誤、還是DNS連不到。

這支測試直接構造APIResult物件（不用真的連網路），驗證修正後的diagnose()能正確
依http_status/json_status把GAVE_UP的各種情況歸類到正確的診斷訊息。

跑法：
    python tests/test_connectivity_diagnosis.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from run_connectivity_test import diagnose
from src.finmind_client import APIResult, Outcome


def make_result(**overrides) -> APIResult:
    base = dict(
        outcome=Outcome.GAVE_UP,
        dataset="TaiwanStockPrice",
        data_id="2330",
        start_date="2026-09-01",
        end_date="2026-09-05",
        http_status=None,
        json_status=None,
        msg=None,
        row_count=0,
        data=[],
        attempts=1,
        error_detail=None,
    )
    base.update(overrides)
    return APIResult(**base)


def main() -> None:
    # 1) 工程師實測的真實情境：重試用完，最後一次是HTTP 200但JSON status=402額度用盡
    #    （這是修正前bug永遠診斷不到的核心案例——outcome是GAVE_UP，不是API_ERROR）
    r1 = make_result(
        outcome=Outcome.GAVE_UP,
        http_status=200,
        json_status=402,
        msg="Data search too fast, please try again in 1 minute.",
        error_detail=None,
    )
    msg1, code1 = diagnose(r1, max_retries=1)
    assert code1 == 1, f"402額度用盡應該回傳exit_code=1，實際={code1}"
    assert "json_status=402" in msg1 or "402" in msg1, f"訊息應該提到402，實際：{msg1}"
    assert "額度用盡" in msg1 or "402" in msg1, f"訊息應該有額度用盡相關診斷，實際：{msg1}"
    assert "共用同一組token" in msg1, f"應該包含shared-token的解釋（工程師實測發現的根因），實際：{msg1}"
    print("✓ 案例1（GAVE_UP + http200/json402額度用盡）正確歸類，不再落入generic fallback")

    # 2) SUCCESS：正常情況，不受GAVE_UP覆寫影響（outcome本身就是SUCCESS，不會走到GAVE_UP分支）
    r2 = make_result(outcome=Outcome.SUCCESS, http_status=200, json_status=200, msg="success", row_count=5)
    msg2, code2 = diagnose(r2, max_retries=1)
    assert code2 == 0, f"SUCCESS應該回傳exit_code=0，實際={code2}"
    assert "連線正常" in msg2
    print("✓ 案例2（SUCCESS）正確回傳exit_code=0")

    # 3) SUCCESS_EMPTY：也算正常（真實無資料，不是錯誤）
    r3 = make_result(outcome=Outcome.SUCCESS_EMPTY, http_status=200, json_status=200, msg="success", row_count=0)
    msg3, code3 = diagnose(r3, max_retries=1)
    assert code3 == 0
    assert "連線正常" in msg3
    print("✓ 案例3（SUCCESS_EMPTY）正確回傳exit_code=0")

    # 4) 網路層失敗：GAVE_UP，但http_status是None（根本連不上，不是FinMind錯誤回應）
    r4 = make_result(
        outcome=Outcome.GAVE_UP,
        http_status=None,
        json_status=None,
        error_detail="ConnectionError: [Errno -2] Name or service not known",
    )
    msg4, code4 = diagnose(r4, max_retries=1)
    assert code4 == 1
    assert "網路層失敗" in msg4, f"http_status=None應該診斷為網路層失敗，實際：{msg4}"
    assert "ConnectionError" in msg4
    print("✓ 案例4（GAVE_UP + http_status=None）正確歸類為網路層失敗")

    # 5) HTTP層錯誤：GAVE_UP，http_status=500（伺服器錯誤，不是額度問題）
    r5 = make_result(
        outcome=Outcome.GAVE_UP,
        http_status=500,
        json_status=None,
        error_detail="HTTP 500 Internal Server Error",
    )
    msg5, code5 = diagnose(r5, max_retries=1)
    assert code5 == 1
    assert "HTTP層錯誤" in msg5, f"http_status=500應該診斷為HTTP層錯誤，實際：{msg5}"
    assert "500" in msg5
    print("✓ 案例5（GAVE_UP + http_status=500）正確歸類為HTTP層錯誤")

    # 6) PARSE_ERROR情境：GAVE_UP，http_status=200但json_status=None（回應不是合法JSON）
    r6 = make_result(
        outcome=Outcome.GAVE_UP,
        http_status=200,
        json_status=None,
        error_detail="JSONDecodeError: Expecting value: line 1 column 1 (char 0)",
    )
    msg6, code6 = diagnose(r6, max_retries=1)
    assert code6 == 1
    assert "不是合法JSON" in msg6, f"http200+json_status=None應該診斷為JSON parse錯誤，實際：{msg6}"
    print("✓ 案例6（GAVE_UP + http200/json_status=None）正確歸類為JSON parse錯誤")

    # 7) 無法歸類的邊界情況：GAVE_UP，但http_status=200且json_status=200（理論上不該發生，
    #    因為這種組合本來outcome應該是SUCCESS/SUCCESS_EMPTY，但這裡故意測試fallback分支還在，
    #    不會拋例外，會給出可讀的診斷訊息讓人貼給數據師看）
    r7 = make_result(outcome=Outcome.GAVE_UP, http_status=200, json_status=200, msg="success")
    msg7, code7 = diagnose(r7, max_retries=1)
    assert code7 == 1
    assert "無法明確歸類" in msg7
    print("✓ 案例7（邊界情況）fallback分支仍正常運作，不會拋例外")

    print("\n全部連線診斷測試通過（7/7）。GAVE_UP覆寫outcome不再讓具體錯誤分類失效。")


if __name__ == "__main__":
    main()
