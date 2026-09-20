# 富途資料品質檢測 V1.0 — 模組規格與測試骨架

> 📌 **用途**：本文件是工程參考文件，供日後任何一次接上「富途牛牛（Futu）即時投資數據平台」的工程階段直接對照落地使用，不需重新設計。
>
> 2026-09-20 · 依據：LUNA 交接文件兩份（*Testing Roadmap V1.0*、*Market Data Quality KeyPoints*）

---

## 前提與限制

本文件不假設現有 `luna_data_engine` V1.2 程式碼內容（撰寫本文件時的對話環境未連結使用者電腦，也未收到實際專案檔），也不直接改寫或虛構現有程式。以下全部內容為**純規格設計**，目的是讓使用者（或日後接上實際專案的工程階段）可以直接對照現有 `luna_data_engine` 結構落地，而不需重新設計。

若日後提供實際專案（壓縮檔或連結電腦），需先比對現有目錄命名、共用 Token 流程、現有 Instrument Registry（若已存在）後再落實。

**不碰的地方**：現有 FinMind scraper、主回補作業、共用 Token 流程、任何交易／下單邏輯。

---

## A. 模組架構

提議新增獨立目錄，與現有 FinMind 相關模組平行存在，**不共用模組內部邏輯**，僅在最終輸出層（Market Snapshot）共用同一 Schema。

```
luna_data_engine/
  adapters/
    finmind/              <- 現有，不動
    futu/                 <- 新增，本文件範圍
      __init__.py
      client.py           <- 封裝 Futu OpenAPI (futu-api SDK) 連線、逐筆呼叫、timeout/重試
      raw_store.py        <- 保存 Raw Record（原始 JSON 回應，不覆蓋，可追溯）
      normalizer.py       <- Raw -> Market Snapshot V1.1 欄位映射
      validator.py        <- 驗證規則引擎，產出 validation_status + audit log
      error_classifier.py <- 錯誤分類（程式/權限/額度/休市/空資料/格式）
      schemas.py           <- Market Snapshot V1.1 資料類別定義（pydantic/dataclass）
      config.py            <- 標的清單、訂閱設定，獨立於 FinMind 設定檔
  core/
    base_adapter.py        <- 新增，抽象介面（見 E 節），只定義不實作，不強制 FinMind 繼承
  storage/
    futu_raw_records       <- 新表，獨立於現有 FinMind 資料表
    market_snapshot_v1_1   <- 新表，統一輸出格式（不論來源是 Futu 還是未來 FinMind）
    validation_audit_log   <- 新表
  tests/
    futu/                  <- 新增，本文件 D 節骨架
```

若現有專案已有同名目錄或命名規則（例如 `sources/` 而非 `adapters/`），比照現有命名調整，不強求此處命名。**重點是：新目錄與現有 FinMind 目錄平行，不共享模組內部狀態。**

---

## B. 資料流程

完全沿用兩份交接文件確立的五階段流程，不新建另一套：

```
Futu OpenAPI 呼叫
  │
  ▼
Raw API Response（原樣存檔，HTTP status + 完整 JSON body，不經任何加工）
  │  -- raw_store.py 寫入 futu_raw_records（每次新增一筆，絕不覆蓋，
  │     欄位包含 raw_id / 呼叫時間 retrieval_timestamp）
  ▼
Normalization（normalizer.py：把 Futu 原始欄位映射到 Market Snapshot V1.1
  統一欄位名，不做任何推論/補全）
  ▼
Validation（validator.py：逐項執行 C 節驗證規則，產出 validation_status 與
  validation_audit_log）
  ▼
分流：
  - validation_status in {PASS, CONDITIONAL} -> 寫入 market_snapshot_v1_1，可被 Research Hub 讀取
  - validation_status in {FAIL, BLOCKED, EMPTY} -> 僅寫入 audit log 與 Raw 層，
    不進入 market_snapshot_v1_1，不被 Research Hub 看到
```

- Raw 與標準化分開存檔（兩份文件都強調這點），標準化紀錄必須包含 `source_raw_id` 欄位可回連原始回應。
- 只有通過驗證的資料才進入 Research Hub 可見範圍——對應 KeyPoints 文件「Market Snapshot 只提供合格資料，不直接產生買進、賣出或加碼指令」的原則。
- `retrieval_timestamp`（程式呼叫時間）與 `data_timestamp`（Futu 回傳的 `update_time`）**分欄位存放，不得合併**。

---

## C. 驗證規則

驗證狀態整合兩份文件的用詞（Roadmap 用 PASS/CONDITIONAL/FAIL/BLOCKED/EMPTY，KeyPoints 用 PENDING/VALIDATING/PASS/CONDITIONAL/FAIL），合併為完整生命週期：

```
PENDING -> VALIDATING -> { PASS | CONDITIONAL | FAIL | BLOCKED | EMPTY }
```

驗證規則依**執行順序**列（優先處理 HTTP 200 但 JSON 內容失敗的情境）：

| # | 規則 | 判定 |
|---|------|------|
| 1 | 傳輸層檢查：HTTP status code 非 200 | → 直接 `FAIL`，分類為「程式/連線」錯誤 |
| 2 | **JSON 內容檢查（優先級最高）**：HTTP 200 不代表成功——必須解析 JSON body，檢查 Futu 自己的 `ret_code`/`status`/`msg` 欄位；`ret_code != 0`（或等效錯誤標示） | → `FAIL`，並保存完整錯誤訊息。**這是整套驗證流程的第一道閘門，不能略過** |
| 3 | 欄位完整性：必需欄位（source/symbol/contract/price/timestamp）缺失 | → `FAIL`；選填欄位缺失使用 `null`，**絕不以 0 代替**（KeyPoints 明文規定） |
| 4 | 權限/額度區分：錯誤訊息包含權限不足、未訂閱、額度用盡等關鍵字 | → `BLOCKED`（非 `FAIL`），與「真的沒有資料」的 `EMPTY` 明確區分 |
| 5 | 時間戳治理：`retrieval_timestamp` 與 `data_timestamp` 必須分開並標註時區；禁止對同一時間值重複轉換時區；若 `data_timestamp` 早於上次成功快照或晚於當前時間超過合理閾值 | → `CONDITIONAL` 並註記 |
| 6 | 價格邏輯檢查：若存在 OHLC 欄位，驗證 `high >= max(open, close, low)` 且 `low <= min(open, close, high)`；不成立 | → `FAIL` |
| 7 | **連續合約標記（硬性規則，不得略過）**：若 symbol 屬於連續合約（如 `US.CLmain`），`contract_type` 與 `roll_adjustment_method` 必須明確寫入；無法從 API 確認時，不得猜測 | → 寫為 `roll_adjustment_method = "unknown"` 且 `validation_status = CONDITIONAL`，**不得直接標 PASS** |
| 8 | 重複快照識別：相同 `symbol` + `data_timestamp` 的 raw record 已存在 | → 新筆標記為重複，不重複寫入 `market_snapshot_v1_1` |
| 9 | 市場狀態交叉比對：回傳的 `market_status` 與交易日曆（週末/假日/盤前盤後）邏輯不一致 | → `CONDITIONAL` 並註記疑點，**不自行修正** |

> ⚠️ 時間戳治理規則（#5）對應 2026-09-20 QC 報告中發現的**雙重時區轉換缺陷**，是回歸測試的重點對象。

每筆驗證結果寫入 `validation_audit_log`，欄位參考 Roadmap 文件「每次測試固定回報格式」：

`Test ID / 日期 / 目標 / API Function / 標的 / 輸入參數 / 原始回應 / 資料結果 / 欄位完整性 / 時間戳 / 錯誤分類 / validation_status / 問題 / 修正 / 下一步`

---

## D. 測試案例（骨架，pytest 風格命名）

對應 Roadmap 文件 MVP-01～MVP-04 的範圍，分四組。每個測試建議使用 mock 的 Futu 回應（固定 fixture JSON，涵蓋正常/錯誤/空資料三種場景），**不在單元測試中真實呼叫 API**，避免消耗額度或受網路狀態影響測試穩定性。

### 連線與權限（MVP-01）
- [ ] `test_connection_success_returns_pass`
- [ ] `test_missing_quote_read_permission_returns_blocked`
- [ ] `test_market_closed_state_detected_correctly`

### 快照與標準化（MVP-02/03）
- [ ] `test_six_us_equities_raw_fetch_succeeds`（NVDA/AMD/MU/AAPL/GOOGL/AMZN）
- [ ] `test_normalizer_maps_all_required_v1_1_fields`
- [ ] `test_missing_optional_field_stored_as_null_not_zero`
- [ ] `test_raw_record_immutable_never_overwritten`
- [ ] `test_snapshot_traceable_to_raw_record_via_id`

### 錯誤與邊界情境（MVP-04，優先實作）
- [ ] `test_http_200_with_ret_code_error_marks_fail` ← **使用者指定優先項**
- [ ] `test_http_non_200_marks_fail_as_connection_error`
- [ ] `test_malformed_json_body_marks_fail`
- [ ] `test_empty_result_set_marks_empty_not_fail`
- [ ] `test_subscription_required_marks_blocked_not_empty`
- [ ] `test_timeout_marks_fail_with_retry_log`
- [ ] `test_after_hours_data_flagged_with_market_session`
- [ ] `test_duplicate_snapshot_same_symbol_timestamp_rejected`
- [ ] `test_continuous_contract_without_roll_method_marks_conditional`（US.CLmain 類，不得直接 PASS）
- [ ] `test_retrieval_timestamp_and_data_timestamp_never_merged`（回歸測試，對應 2026-09-20 發現的雙重轉換缺陷）

### 尚無法實作（需真實 API 呼叫才能驗證，先寫為 skip/xfail）
- [ ] `test_rate_limited_distinguished_from_no_data`（LUNA 先前文件標註尚未壓力測試）
- [ ] `test_oauth_scope_rejects_trading_calls`（需實際 OAuth 環境才能驗證為真唯讀）

---

## E. 與現有 LUNA 系統整合的介面設計與注意事項

### 共用介面（不強制 FinMind 現在改寫）

定義一個抽象基底類 `BaseMarketDataAdapter`（新檔案，不插入現有程式），只規定介面不強制繼承：

```python
class BaseMarketDataAdapter(Protocol):
    def fetch_raw(self, symbol: str) -> RawRecord: ...
    def normalize(self, raw: RawRecord) -> MarketSnapshotV1_1: ...
    def validate(self, snapshot: MarketSnapshotV1_1) -> ValidationResult: ...
    def to_snapshot(self, symbol: str) -> MarketSnapshotV1_1: ...  # 串起上三步
```

新 Futu Adapter 實作此介面。現有 FinMind 模組**不需立即改寫**來符合此介面——是否讓 FinMind 也改用同一介面是日後可選項，不列入本階段範圍。Research Hub 層只需讀取統一的 `market_snapshot_v1_1` 表，不需知道資料來自 FinMind 還是 Futu。

### Instrument Registry

若現有專案已有商品主檔／Instrument Registry 表，優先增建 `source` 欄位區分（FUTU vs FINMIND vs TWSE）來共用現有表，避免重複建模；若尚未存在，新建一張專屬於 Futu 的，後續可合併。這點需實際看到現有專案才能最終確認，本文件僅提供原則。

### 權限與安全

- **不執行交易**：Futu Adapter 只呼叫 `quote:read` 相關方法，不引入任何下單/交易 SDK 方法，也不與現有交易安全限制發生交集。
- **獨立憑證設定**：Futu 連線與認證用於自己的 `config.py`，不與現有 FinMind Token 流程/額度共用、不互相影響。
- **未驗證資料不進模型**：Roadmap 文件明確要求「不將未驗證資料直接接入預測模型」——本設計已在 B 節資料流程中以分流邏輯硬性保證。

### 暫緩範圍（明確不在本階段）

對應兩份文件「暫時不納入」清單：大規模 AI Score/XGBoost/LightGBM 正式訓練、完整起漲雷達回測、一次接入全部台股標的、在未完成資料驗證前宣稱模型能力提升。這些待 MVP-07（通過驗證後接入 Research Hub）完成後再評估。

---

## 下一步（需使用者提供才能繼續）

在真正動手實作 Futu Adapter 之前，需要先取得以下三項資訊：

1. 現有 `luna_data_engine` 的目錄結構截圖或 `tree` 輸出（不需完整程式碼，先看命名規則與分層）。
2. 現有 Instrument Registry（若有）的 schema。
3. FinMind Adapter 目前使用的資料庫（SQLite？其他？）與連線方式，以確認新表可共存於同一資料庫。

取得上述資訊後，可直接實作 Futu Adapter 的檔案骨架與單元測試，**不需再修改本規格**。

---

## 使用說明（給日後接手的工程師）

- 這份文件是**純規格**，尚未有對應程式碼落地。下次要串接富途牛牛（Futu）即時投資數據平台時，先看這份文件，再對照當時 `luna_data_engine` 的實際目錄結構調整命名，不需要重新設計流程或驗證規則。
- 驗證規則（C 節）與測試骨架（D 節）是最容易被跳過但最重要的部分——尤其是 #2（JSON 內容檢查優先於 HTTP 200）、#7（連續合約不得猜測）、#5（時間戳絕不重複轉換時區）這三條，都是先前 QC 報告中真正踩過的坑。
- 動工前務必先完成「下一步」三項資訊的蒐集，避免規格與現有系統命名/資料庫衝突。
