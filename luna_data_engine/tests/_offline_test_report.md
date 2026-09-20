# LUNA Data Engine 資料品質報告 — offline_test

產出時間：2026-09-20T04:04:12.210817Z

## 1. 下載結果彙總

| outcome | 次數 |
|---|---|
| success | 6 |
| gave_up | 1 |

總請求數：7，成功：6，失敗（含最終放棄）：1


## 2. 失敗/異常請求明細（最新50筆）

| 時間 | dataset | data_id | 區間 | outcome | http | json_status | msg |
|---|---|---|---|---|---|---|---|
| 2026-09-20T04:04:12.181162+00:00 | TaiwanStockPrice | 2330 | 2026-09-01~2026-09-01 | gave_up | 200 | 402 | Data search too fast, please try again in an hour or purchas |

## 3. 各資料集實際可用日期範圍（data_availability）

| dataset | stock_id | min_date | max_date | row_count |
|---|---|---|---|---|
| TaiwanStockBalanceSheet | 2330 | 2025-12-31 | 2025-12-31 | 2 |
| TaiwanStockCashFlowsStatement | 2330 | 2025-03-31 | 2026-06-30 | 2 |
| TaiwanStockDividend | 2330 | 2025-03-24 | 2026-03-23 | 2 |
| TaiwanStockFinancialStatements | 2330 | 2025-03-31 | 2026-06-30 | 3 |
| TaiwanStockInstitutionalInvestorsBuySell | 2330 | 2026-09-01 | 2026-09-01 | 2 |

## 4. 逐股/逐資料集品質檢查

| dataset | stock_id | 筆數 | 日期範圍 | 重複筆數 | 缺失交易日數 | 異常值數 | 備註 |
|---|---|---|---|---|---|---|---|
| TaiwanStockInfo | 2330 | 2 | 2026-09-20~2026-09-20 | 0 | 0 | 0 |  |
| TaiwanStockInstitutionalInvestorsBuySell | 2330 | 2 | 2026-09-01~2026-09-01 | 0 | 0 | 0 |  |
| TaiwanStockFinancialStatements | 2330 | 3 | 2025-03-31~2026-06-30 | 0 | 0 | 0 |  |
| TaiwanStockBalanceSheet | 2330 | 2 | 2025-12-31~2025-12-31 | 0 | 0 | 0 |  |
| TaiwanStockCashFlowsStatement | 2330 | 2 | 2025-03-31~2026-06-30 | 0 | 0 | 0 |  |
| TaiwanStockDividend | 2330 | 2 | 2025-03-24~2026-03-23 | 0 | 0 | 0 |  |

## 7. 已知限制（本skeleton尚未處理，需要下一階段補上）

- 交易日曆是用已下載股票的TaiwanStockPrice聯集反推，不是官方交易日曆，若剛好某天全部樣本股都缺資料會偵測不到。
- 股票代號變更、上市櫃狀態異動（如新光金併入台新金）尚未做自動比對，需要人工核對TaiwanStockInfo的type欄位與代號沿革。
- 除權息還原方法尚未跟TaiwanStockPriceAdj的實際還原基準日交叉驗證。
- 財報/月營收的Point-in-Time對齊（用公告日而非資料所屬期間）尚未在下載階段實作，目前存的是FinMind回傳的date欄位（通常是財報所屬期間末日），條件計算階段務必額外處理公告延遲，否則會有未來資訊洩漏風險。