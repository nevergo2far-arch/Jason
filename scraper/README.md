# 台股資料批次下載器（scraper/）

給另一位負責分析的 AI／自己使用的台股資料下載工具。設計目標：**合法來源優先、批次可中斷續傳、輸出交給資料分析員好讀取**。

---

## ⚠️ 三個資料來源的定位不一樣，請先讀完這段

| 來源 | 狀態 | 說明 |
|---|---|---|
| **FinMind** | ✅ 主力來源 | 官方開放 API，本來就是設計給程式化存取用的，本工具對它沒有任何速率規避設計，只有正常的重試與節流。 |
| **TWSE / TPEx OpenAPI** | ✅ 輔助/交叉驗證 | 交易所自己的官方開放資料平台，無需授權、無 ToS 限制，一次可拿到全市場資料，適合「整個市場」規模的批次。 |
| **MOPS 公開資訊觀測站** | ⚠️ 保守使用 | 官方公開資訊，但沒有正式公開 API，本工具用的是網站自己前端呼叫的查詢路徑。已加上 robots.txt 檢查（抓不到就直接拒絕，不會賭一把）與低速率限制，**預設關閉細項查詢的批次化**，只做單筆查詢。 |
| **Goodinfo** | 🛑 預設關閉，風險自負 | **Goodinfo 服務條款明確禁止自動化擷取，且有防爬蟲機制。** 本工具刻意**不做任何「擬人化」規避偵測的技術**（不偽造瀏覽器指紋、不模擬滑鼠移動、不破解驗證碼、不輪替 Proxy）。只有：誠實標示身份的 User-Agent、固定且很慢的請求間隔（預設 12 秒/次）、每次執行的請求上限（預設 50 次）、robots.txt 檢查失敗就直接放棄。即使如此，只要 Goodinfo 條款禁止自動化存取，執行這個模組本身仍是 ToS 風險，需要你自己承擔，不是本工具幫你規避掉的。**預設不會被任何指令自動觸發**，必須手動設定 `GOODINFO_ENABLED=1` 才會運作；建議優先沿用本專案既有做法（請使用者自行截圖 Goodinfo 頁面），而不是啟用這個模組。 |

> 這個定位判斷是基於本專案既有的 `SKILL.md`（第 39 行起）已經寫明「Goodinfo 可能有防爬蟲機制無法穩定讀取」並刻意設計成截圖流程 —— 這次新增的批次下載器延續同樣的判斷，只是把「不硬闖」的原則做成程式碼裡的具體限制，而不是只寫在文件裡。

---

## 環境限制說明 / 實測結果

這套程式碼原本是在一個**完全沒有一般網路對外連線**的沙盒環境裡開發的，所以最早一版沒辦法直接打真實 API 驗證。後來用 `.github/workflows/scraper-smoke-test.yml`（PR 一有 `scraper/**` 的變更就會自動跑，GitHub-hosted runner 有正常對外連線）實際打過真實 API，結果：

| 項目 | 結果 |
|---|---|
| FinMind `TaiwanStockInfo`（股票清單） | ✅ 真實跑過，3149 檔上市+上櫃股票 |
| FinMind `TaiwanStockCashFlowsStatement`（現金流量表） | ✅ 真實跑過，2330/2454/2317 各上千筆真實資料 |
| FinMind `TaiwanStockMonthRevenue`（月營收） | ✅ 真實跑過，296 筆/檔 |
| TWSE OpenAPI `STOCK_DAY_ALL`（全市場日成交資訊） | ✅ 真實跑過，1377 檔 |
| TWSE OpenAPI `t187ap45_L`（股利分派情形） | ✅ 真實跑過，1226 筆 |
| TPEx OpenAPI `tpex_mainboard_daily_close_quotes`（上櫃日成交） | ✅ 真實跑過，11481 筆 |
| ~~TWSE/TPEx 月營收 OpenAPI~~ | ❌ 移除。實測發現兩個交易所的開放資料平台都**沒有**單純「每檔公司月營收」這種格式的 endpoint（猜測的 dataset code 打中的其實是公司基本資料表，不是月營收）；月營收一律用 FinMind 取得，程式碼已同步調整、不再猜測不存在的 endpoint |
| MOPS `/ajax_t05st03` | ⚠️ 只驗證了 robots.txt 判斷邏輯本身正確，實測發現 mopsov.twse.com.tw 的 robots.txt **確實擋掉**這個猜測路徑——`MopsClient` 因此會如預期拒絕請求（fail closed 生效），但真正該打哪個路徑仍未確認 |
| Goodinfo | 未測試（刻意跳過，見上方說明） |

實測過程中也修正了兩個原本猜錯的地方：TWSE/TPEx OpenAPI 的 base URL 原本重複了一次 `/v1`（TWSE 剛好被伺服器重導向蓋過去沒發現，TPEx 會直接 520 錯誤）；以及上面提到的月營收 endpoint 誤判。

`sources/mops_client.py` 的 `QUERY_PATH` 目前**確認會被 robots.txt 擋下**，也就是說用預設設定執行 MOPS 細項查詢會直接被跳過、不會真的送出請求——這符合本專案「不確定就不硬闖」的設計，但也代表這個路徑本身需要重新找一個 robots.txt 允許的正確查詢方式才有實際用途，目前只是安全地什麼都不做。

想重跑這個驗證（例如之後 FinMind/TWSE 改版），到 GitHub 的 Actions 分頁手動觸發 `Scraper smoke test (real network)`，或對這個 PR 的 `scraper/**` 路徑推新的 commit 就會自動跑一次。

---

## 安裝

```bash
cd scraper
pip install -r requirements.txt
```

FinMind 免費額度不需要 token 也能用，但額度很低；建議到 https://finmindtrade.com/ 註冊拿一組免費 token，再設定：

```bash
export FINMIND_TOKEN="你的token"
```

---

## 使用方式

### 全市場批次下載（FinMind，最常用的路徑）

```bash
python run_batch_download.py all
```

這個指令依序做：
1. `stock-list`：從 FinMind 拉全部上市＋上櫃股票清單，寫入 `stocks` 表
2. `queue --source finmind --datasets all`：把每檔股票 × 每個資料集（現金流量表／損益表／資產負債表／月營收／股利／股價）排進 `checkpoint` 表
3. `run --source finmind`：依序抓取，每完成一筆就寫入 SQLite 並標記 checkpoint 完成
4. `export`：把 SQLite 內容匯出成每檔股票一個資料夾的 JSON/CSV，並產生 `data/exports/index.json` 總覽清單

**全市場約 2500+ 檔股票 × 6 個資料集，預設節流是 `FINMIND_RPM=50`（每分鐘 50 次請求），粗估要跑數小時。**這是刻意的——寧可跑久一點，也不要用超過官方額度的速率轟炸 API。

### 分步驟執行（建議先小範圍測試）

```bash
python run_batch_download.py stock-list
python run_batch_download.py queue --source finmind --datasets cash_flow_statement monthly_revenue --limit 5
python run_batch_download.py run --source finmind
python run_batch_download.py export
```

### 只抓近期一段時間（技術面／籌碼面分析常用）

`price`／`institutional_investors`／`margin_trading` 這幾個資料集預設會抓 FinMind 有的全部歷史（`start_date` 預設 `2000-01-01`），全市場全歷史資料量非常大也很慢。只要近 N 個月，用 `run --start-date` 縮小區間即可（`queue` 不用重新下，同一批 job 用不同 start_date 重新 `run` 也可以）：

```bash
python run_batch_download.py stock-list
python run_batch_download.py queue --source finmind --datasets price institutional_investors margin_trading
python run_batch_download.py run --source finmind --start-date 2026-06-19   # 近 3 個月
python run_batch_download.py export
```

**注意**：`--start-date` 只影響 FinMind 資料集，對 `market-wide`（TWSE/TPEx OpenAPI）沒有作用——那幾個 endpoint 本身就只回傳「最新一個交易日」，沒有查歷史區間的參數，想要逐日累積歷史，需要每天另外跑一次 `market-wide` 自行累積。

### 中斷後續傳

批次下載中途 Ctrl-C 沒關係，已完成的 (股票, 資料集) 組合已經記錄在 `checkpoint` 表裡；直接重新執行 `run --source finmind` 就會跳過已完成的，只補沒做完的部分。想看目前進度：

```python
from config import CONFIG
from core.storage import Storage
with Storage(CONFIG.db_path) as s:
    print(s.checkpoint_summary())  # 例如 {'done': 8000, 'pending': 6500, 'error': 12}
```

### Goodinfo（預設關閉，需明確啟用）

```bash
GOODINFO_ENABLED=1 python run_batch_download.py queue --source goodinfo --datasets cash_flow monthly_revenue_chart --limit 10
GOODINFO_ENABLED=1 python run_batch_download.py run --source goodinfo
```

啟用前務必先讀 `sources/goodinfo_client.py` 檔案開頭的說明。這個 client 只抓原始 HTML 存起來，**不做任何頁面解析**——因為沒有網路環境沒辦法對照真實頁面結構寫解析邏輯，避免我憑印象亂猜 DOM 結構。要用這些資料，需要你自己另外寫解析程式，或直接用截圖流程取代。

### MOPS 細項查詢（單筆，非批次迴圈）

```python
from config import CONFIG
from sources.mops_client import MopsClient
client = MopsClient(CONFIG.mops)
result = client.fetch_financial_statement_detail("2330", "2024", "4")
```

沒有做成 `queue`/`run` 批次指令，是刻意的：MOPS 沒有官方 API，不想鼓勵對它做「整個市場」規模的迴圈請求。

---

## 輸出格式

### 1. SQLite（`data/tw_stock_data.db`）—— 資料的唯一真實來源

- `stocks`：股票代號、名稱、市場別（TWSE/TPEx）、產業別
- `raw_records`：每一筆抓到的原始資料，欄位是 `source / dataset / ticker / record_date / payload_json / fetched_at`。**採用通用 schema 而非每個資料集一張表**，好處是新增資料集不需要改資料庫結構；壞處是要用 SQL 直接查數值需要 `json_extract(payload_json, '$.欄位名')`。
- `checkpoint`：批次下載的進度追蹤表，`(ticker, dataset, source)` 唯一鍵，`status` 為 `pending/done/error/skipped`。

### 2. JSON / CSV（`data/exports/{股票代號}/{資料集}.json` 與 `.csv`）—— 給另一位 AI 分析員直接讀

執行 `export` 之後，每檔股票一個資料夾，每個資料集各一份 JSON（陣列，保留原始欄位）與 CSV（同樣內容，方便試算表或 pandas 開）。

`data/exports/index.json` 是總覽清單，格式：

```json
{
  "generated_at": "2026-09-19T...",
  "tickers": {
    "2330": {
      "name": "台積電",
      "market": "TWSE",
      "industry": "半導體業",
      "datasets": {
        "cash_flow_statement": {"record_count": 312, "last_fetched": "2026-09-19T..."},
        "monthly_revenue": {"record_count": 96, "last_fetched": "..."}
      }
    }
  }
}
```

另一位 AI 分析員可以先讀 `index.json` 知道哪些股票、哪些資料集已經有資料、資料筆數與最後更新時間，再決定要讀哪個檔案，不需要掃過整個資料夾樹。

---

## 資料集欄位對照（對應既有 `SKILL.md` 六步驟分析框架的 A/B/C/D/E 定義）

`raw_records` 裡每個資料集的 `payload_json` 直接是 FinMind 原始回傳格式，重點欄位對照：

| dataset | 對應 SKILL.md 概念 | 關鍵欄位（FinMind 原始命名） |
|---|---|---|
| `cash_flow_statement` | A（營業）/B（投資）/C（融資）現金流 | `type`（線項名稱，如 `CashFlowsFromOperatingActivities`）＋ `value`；每個 (date, type) 一筆，需自行依 type 分類彙總成 A/B/C |
| `income_statement` | D（稅前/稅後淨利）、EPS | `type` 含 `IncomeAfterTaxes`、`PreTaxIncome`、`EPS` 等 |
| `balance_sheet` | E（期末現金餘額）之交叉驗證 | `type` 含 `CashAndCashEquivalents` 等 |
| `monthly_revenue` | 第四步月營收趨勢 | `revenue`、`revenue_year`、`revenue_month`、年增率相關欄位 |
| `dividend` | 殖利率相關基本資訊 | `CashEarningsDistribution`、`StockEarningsDistribution`、公告/除權息日期 |
| `price` | 股價（52週高低等）、技術面時間序列 | `open/max/min/close/Trading_Volume`，每個 (ticker, date) 一筆，可用 `--start-date` 取一段區間 |
| `institutional_investors` | 籌碼面：三大法人買賣超 | `name`（`Foreign_Investor`/`Investment_Trust`/`Dealer_*` 等分類）＋ `buy`/`sell`；長格式，每個 (date, name) 一筆 |
| `margin_trading` | 籌碼面：融資融券餘額 | `MarginPurchaseTodayBalance`、`ShortSaleTodayBalance` 等，每個 (ticker, date) 一筆 |

**注意**：`cash_flow_statement`／`income_statement`／`balance_sheet` 是「長格式」（一個日期會展開成多筆，每筆對應一個 `type` 線項），不是每個日期一列多欄；另一位 AI 分析員在彙總 A/B/C/D/E 時要先依 `type` 篩選、依 `date` 分組加總，不能直接把整份 JSON 陣列的 `value` 加總。`type` 的確切字串需對照 FinMind 目前文件核實（不同版本 API 曾經調整過命名）。

---

## 已知限制 / 待辦

- MOPS 的細項查詢端點目前確認會被 robots.txt 擋下（因此不會實際送出請求），還沒找到 robots.txt 允許的正確查詢路徑，見上方「環境限制說明 / 實測結果」。
- Goodinfo client 只存原始 HTML，未寫解析器。
- 未處理股票分割／減資造成的每股數字基期不一致問題（`SKILL.md` 裡對此有詳細規則，若要讓下游 AI 分析員自動判斷，需要額外寫檢查邏輯，目前留給分析階段人工/AI 判斷）。
- 沒有寫排程（cron／Airflow 之類），如需要定期自動更新，需自行外掛排程工具呼叫 `run_batch_download.py all`。
