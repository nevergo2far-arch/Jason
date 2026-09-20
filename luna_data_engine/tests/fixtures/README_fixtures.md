# Fixture資料來源說明

這個沙盒環境的shell無法直連 `api.finmindtrade.com`（egress白名單擋掉403），但同一次對話中
用 WebFetch 工具實際呼叫過FinMind API並拿到過真實回應。以下fixture檔案是根據那些真實呼叫
重建的：

- `taiwan_stock_info.json`：`TaiwanStockInfo` — WebFetch當時回傳完整原始JSON文字，這份是**逐字**照抄，
  未經任何修改（2330台積電，industry_category兩筆：半導體業/電子工業）。
- `institutional_investors.json`：`TaiwanStockInstitutionalInvestorsBuySell` — WebFetch當時用摘要方式
  回報欄位與2筆範例記錄(2026-09-01, Investment_Trust buy=631374/sell=527505；
  Foreign_Investor buy=26004465/sell=20273602)，這裡依相同dataset的官方欄位規格
  (date/stock_id/name/buy/sell)重建成JSON結構，數值是WebFetch回報的真實數值，非憑空捏造。
- `financial_statements.json`：`TaiwanStockFinancialStatements` — 同上，用WebFetch回報的3筆範例
  記錄(Revenue=839,253,664,000 @2025-03-31、EPS=15.36 @2025-06-30、
  OperatingIncome=766,602,651,000 @2026-06-30)重建。
- `balance_sheet.json`：`TaiwanStockBalanceSheet` — 用WebFetch回報的範例(TotalAssets=7,933.0億元
  @2025-12-31、Inventories=288.1億元 @2025-12-31)重建，單位換算為FinMind原始欄位慣用的元。
- `cash_flow.json`：`TaiwanStockCashFlowsStatement` — 用WebFetch回報的PropertyAndPlantAndEquipment
  範例值(-330,826,730,000 @2025-03-31、-846,764,746,000 @2026-06-30)重建。
- `dividend.json`：`TaiwanStockDividend` — 用WebFetch回報的除權息日期範例重建
  (2025-03-24分配/除息2025-03-18/發放2025-04-10；2026-03-23分配/除息2026-03-17/發放2026-04-09)。

- `api_error_quota.json`：**這份是合成的**，不是本次對話實際抓到的錯誤回應（本次對話測試時
  沒有真的把額度用完）。內容依LUNA任務書描述的已知現象（HTTP 200但JSON status非200）與FinMind
  常見的額度錯誤訊息格式編寫，用途只是測試 `finmind_client.py` 能不能正確把這種情況判成
  `API_ERROR` 而非誤判成空資料。之後在真實環境如果撞到真的額度錯誤，建議把那次的原始回應
  存一份進來取代這個合成版本。

**這些fixture只夠測試「parsing → clean table → data_availability → quality report」這條pipeline邏輯
是否正確，不是完整的歷史資料。真正的POC下載仍必須在能連上FinMind的環境執行。**
