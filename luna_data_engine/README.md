# LUNA Data Engine — Download Skeleton V1.1

**v1.1更新（2026-09-20，依LUNA實測回饋修正）**：
- 新增 `run_connectivity_test.py`——跑POC前先跑這個，幾秒鐘確認token/連線沒問題，
  不用等一整輪POC跑到超時才知道問題出在哪。
- `run_poc.py` / `run_full.py` 加上軟性時間上限（`--max-minutes`，POC預設20分鐘），
  快到時間時downloader會優雅停止排新請求，不會被執行環境強制砍斷。
- 中斷或發生例外時，**一律照樣輸出目前為止的品質報告與`poc_status.json`**（多一個
  `interrupted: true`欄位），不會像v1.0那樣中斷後什麼都沒留下、也不會誤標成通過。
- POC結束日改用固定預設值`2026-09-18`（`config.DEFAULT_POC_END_DATE`），不再悄悄跟著
  「執行當天」漂移，重跑結果才可重現。

實測回報的根因判斷：POC會被中斷，最可能不是FinMind連不上，而是如果token失效或某資料集
需要付費層級，320+次請求會**每次都跑滿5次重試、每次backoff疊加到62秒**，這樣的總耗時很容易
超過執行環境給的時間上限。`run_connectivity_test.py`就是用來在幾秒鐘內先排除這個可能性。

給 LUNA × Claude 短線5~10日研究系統用的資料下載骨架。目的只有一個：把資料下載、
清洗、品質檢查這條地基做扎實，**這個版本不做單條件回測、不做模型訓練、不產生投資評分或買賣訊號**。

## 這個骨架涵蓋什麼

依 2026-09-20 的資料工程可行性審查（33項條件，可23／待確認2／不可8），本skeleton
下載「可」的23項條件所需的10個FinMind資料集，見 `config/datasets.yaml`。

8項「不可」條件（含大戶持股，因FinMind該資料集需付費Backer/Sponsor層級，LUNA已決策暫不
升級）**不在本skeleton下載範圍**，保留在 `config/datasets.yaml` 的 `excluded_datasets` /
`unresolved_condition_ids` 供之後研究替代指標時參考，不代表刪除研究假說。

## 專案目錄結構

```
luna_data_engine/
├── .env.example          複製成 .env 並填入 FinMind token
├── requirements.txt
├── config/
│   ├── datasets.yaml      10個資料集設定 + 條件對應 + 已知限制
│   └── stock_pool_poc.yaml  POC用10檔股票池
├── src/
│   ├── config.py          設定載入（.env + yaml）
│   ├── finmind_client.py  FinMind API client：HTTP+JSON雙重檢查、重試、rate limit
│   ├── downloader.py      下載排程、續傳progress
│   ├── storage.py         SQLite儲存（raw層 + clean層 + data_availability）
│   ├── quality_checks.py  缺失/重複/異常值/交易日完整性檢查、報告產生
│   └── logger_setup.py    logging設定
├── run_poc.py             POC執行入口（10檔，2023~今）
├── run_full.py             全量執行入口（會檢查POC是否通過才放行）
├── data/
│   ├── raw/                原始API回應JSON，依dataset/股票分資料夾
│   └── luna_data_engine.db SQLite資料庫（raw_responses/clean_*/data_availability/download_log）
├── logs/                   每次執行的engine.log與error.log
└── reports/                品質報告(Markdown)、data_availability.csv、失敗請求清單、poc_status.json
```

## 使用的資料集與欄位對照

| Condition_ID | FinMind Dataset | 關鍵欄位 | 備註 |
|---|---|---|---|
| ST-TECH-001/002/003 | TaiwanStockPrice | open/max/min/close/Trading_Volume | 免費(需data_id) |
| ST-TECH-004/005/007 | TaiwanStockPriceAdj | open/max/min/close(還原權息) | 免費(需data_id) |
| ST-TECH-006 | TaiwanStockTotalReturnIndex | price | data_id=TAIEX(上市)/TPEx(上櫃) |
| ST-TECH-010, ST-FLOW-002 | TaiwanStockInstitutionalInvestorsBuySell | buy/sell/name | name含Foreign_Investor/Investment_Trust等 |
| ST-RANK-001~003, ST-REV-001~003 | TaiwanStockMonthRevenue | revenue/revenue_month/revenue_year | **公告日≠資料日期**，見下方Point-in-Time |
| ST-FUND-001, ST-FUND-002/003(分子), ST-CF-001(NI) | TaiwanStockFinancialStatements | type/value（如IncomeAfterTaxes/EPS） | 已確認無研發費用科目 |
| ST-FUND-002/003(分母), ST-FUND-004 | TaiwanStockBalanceSheet | type/value（TotalAssets/Inventories） | |
| ST-FUND-005/006, ST-CF-001(OCF) | TaiwanStockCashFlowsStatement | type/value（PropertyAndPlantAndEquipment代表Capex） | |
| ST-EVENT-004 | TaiwanStockDividend | StockExDividendTradingDate/CashExDividendTradingDate | |
| （股票池主檔） | TaiwanStockInfo | stock_id/stock_name/industry_category | 全市場一次查詢 |

## 安裝

```bash
python -m venv venv
# macOS/Linux:
source venv/bin/activate
# Windows (PowerShell)：
venv\Scripts\Activate.ps1
# Windows (cmd)：
venv\Scripts\activate.bat

pip install -r requirements.txt
cp .env.example .env   # Windows用 copy .env.example .env
# 打開.env，填入 FINMIND_API_TOKEN=你的token
```

## POC執行方式

**建議第一步先跑連線測試**（幾秒鐘結束，確認token/連線沒問題再進入正式POC）：

```bash
python run_connectivity_test.py
```

看到 `✅ 連線正常，可以放心跑 run_poc.py。` 才繼續下一步；如果看到 `API_ERROR`，先照它印出的
診斷訊息處理（通常是token沒填對或額度問題），不要直接硬跑POC，否則很容易在大量請求上疊加
重試時間，撞到執行環境的時間上限。

```bash
python run_poc.py
# 指定時間上限（分鐘），預設20分鐘，快到時會優雅停止而不是被強制砍斷：
python run_poc.py --max-minutes 15
# 中斷後重跑會自動跳過已成功的部分；要強制全部重抓：
python run_poc.py --force
```

執行完會在 `reports/` 產出：
- `poc_<時間戳>_quality_report.md`：完整品質報告（下載成功率、失敗明細、各資料集可用日期範圍、缺失/重複/異常值）
- `poc_<時間戳>_data_availability.csv`
- `poc_<時間戳>_failed_requests.csv`
- `poc_status.json`：記錄這次POC是否通過門檻（預設成功率≥90%），`run_full.py`會檢查這個檔案

## 全量執行方式

**只有在POC通過（`poc_status.json` 的 `passed: true`）之後才建議執行**：

```bash
python run_full.py
```

`run_full.py` 會先讀 `reports/poc_status.json`，沒過就直接中止並印出原因，不會悶著頭硬跑全市場。
若要跳過檢查（不建議）：`python run_full.py --skip-poc-check`。

全量預設沿用POC的10檔股票池（`config/stock_pool_full.yaml`不存在時的fallback）。要換成完整研究
股票池，建議流程是：先用 `TaiwanStockInfo` 抓到的全市場清單篩選你要的範圍，另存成
`config/stock_pool_full.yaml`（格式參考 `stock_pool_poc.yaml`），不要手動key約1,970檔。

## 續傳機制

`data/download_progress.json` 記錄每個 `(dataset, stock_id, 年度區塊)` 的下載狀態，只有
`success`/`success_empty` 才算完成。程式意外中斷後直接重跑同一支腳本，會自動跳過已完成的區塊，
只補沒做完的部分。失敗的區塊**不會**被標記為完成，所以不會被誤當成「這段時間本來就沒資料」。

## 錯誤處理設計（對應任務書「FinMind回應HTTP 200但內部status/msg顯示錯誤」的已知問題）

`src/finmind_client.py` 的判斷順序：

1. HTTP status ≠ 200 → `HTTP_ERROR`
2. HTTP 200，但JSON parse失敗 → `PARSE_ERROR`
3. HTTP 200，JSON status≠200 或 msg≠"success" → `API_ERROR`（**這就是額度用盡等情況，不會被當成空資料**）
4. HTTP 200 + JSON status=200 + msg=success，但data陣列為空 → `SUCCESS_EMPTY`（真實無資料）
5. 以上都正常 → `SUCCESS`

只有 `SUCCESS` / `SUCCESS_EMPTY` 會被寫進clean表與data_availability；其餘一律進
`download_log` 並觸發重試（exponential backoff，預設最多5次），重試仍失敗則記錄為`GAVE_UP`，
不會靜默略過。

## 速率限制

`RateLimiter` 用滑動一小時視窗自我節流（預設550次/小時，低於FinMind免費額度600次/小時留緩衝），
接近上限會主動睡到視窗騰出空間，而不是等被FinMind拒絕才反應。

## 已知限制／尚未解決的問題

1. **這個skeleton尚未在真實FinMind環境跑過完整POC**——開發時的沙盒環境對外連線政策
   擋掉了對 `api.finmindtrade.com` 的直接連線，所以邏輯是用「本次對話已經用WebFetch實際打過
   API拿到的真實回應樣本」做離線測試驗證（見 `tests/test_offline_pipeline.py` 與
   `tests/fixtures/`），**不是**用捏造的假資料測試。但這不等於在你的環境跑起來一定順利，
   第一次跑 `run_poc.py` 請務必看過 `reports/*_quality_report.md` 的失敗明細再判斷要不要繼續。
2. 交易日曆是用已下載股票的 `TaiwanStockPrice` 聯集反推，不是外部官方交易日曆，見品質報告
   第7節說明。
3. 股票代號變更、上市櫃狀態異動（如新光金併入台新金）目前沒有自動比對，需人工核對
   `TaiwanStockInfo` 的欄位與代號沿革。
4. 月營收/財報類資料集目前存的是FinMind回傳的 `date` 欄位（通常是所屬期間末日），
   **不是**實際公告日。條件計算階段（下一階段工作）務必另外處理Point-in-Time對齊，
   否則有未來資訊洩漏風險——這點品質報告會固定提醒，故意不讓人忘記。
5. `config/stock_pool_full.yaml` 沒有內建，需要你之後自己從 `TaiwanStockInfo` 篩選補上。

## 下一階段（本skeleton不做）

條件公式標準化 → 單條件回測 → 條件組合回測 → 成本與風險分析 → 樣本外驗證 → Forecast Ledger →
統計校準 → 模型訓練。這些都要等資料品質報告確認沒有嚴重問題之後再開始。
