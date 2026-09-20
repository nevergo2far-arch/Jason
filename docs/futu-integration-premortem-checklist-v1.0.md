# 實作前資訊收集清單 — 統一 Market Data Adapter Interface（FinMind + Futu）

> 📌 **用途**：在把 FinMind、Futu（及未來其他資料來源）收斂到共同的 `BaseMarketDataAdapter` 介面之前，先在 `luna_data_engine` 專案內盤點現況。**本階段只做盤點與設計，不修改既有 FinMind 程式、不開 PR。**
>
> 對應 [`futu-realtime-data-adapter-spec-v1.0.md`](./futu-realtime-data-adapter-spec-v1.0.md) 文末「下一步」三項，本文件把那三項展開成可直接在專案裡跑的檢查清單與指令。

---

## 如何使用本文件

1. 在 `luna_data_engine` 專案根目錄下，依序執行「Part 1 / 2 / 3」的指令。
2. 每一部分執行完，把輸出貼回（或整理成文字檔提供），我會依據實際輸出接著產出：
   - `BaseMarketDataAdapter` 共同介面草案
   - FinMind 與 Futu 的欄位差異表
   - 共用 Schema 與來源專屬欄位的分工
   - 對既有 FinMind 的影響評估
   - Futu Adapter MVP-01～MVP-04 實作計畫
3. 若某條指令在你的環境找不到對應工具（例如沒裝 `tree`、資料庫不是 SQLite），清單裡都附了替代做法，不用整個跳過該項目。
4. 標示【必做】的指令是時間有限時的最小可行盤點範圍；其餘為補充項目，有餘裕再跑。

> ⚠️ **安全提醒**：Part 3 涉及 Token／連線設定時，**只需要確認「有哪些欄位、用什麼方式設定」，不要把實際 Token 值、密碼、連線字串貼出來**。指令設計上已盡量只印出 key 名稱／設定方式，若某條指令印出了實際敏感值，請自行遮蔽後再提供。

---

## Part 1 — 專案目錄結構

### 盤點項目
- [ ] 專案根目錄下主要目錄與檔案一覽
- [ ] 現有 Adapter 相關目錄（是否已有 `adapters/`、`sources/`、`connectors/` 之類命名）
- [ ] 現有資料層目錄（ORM models、schema 定義、DB migration 目錄）
- [ ] 現有測試目錄結構與命名慣例（`tests/`、`test_*.py` 還是 `*_test.py`）
- [ ] 目前的分層規則（例如是否已有 `core/` 放抽象介面、`storage/` 放資料表定義）
- [ ] 現有 FinMind 模組確切路徑（後面比對欄位/邏輯要用）

### 執行指令

```bash
# 進入 luna_data_engine 專案根目錄後執行

# 【必做】1. 整體目錄樹（忽略常見雜訊目錄，深度限制 4 層避免洗版）
if command -v tree >/dev/null 2>&1; then
  tree -L 4 -I '__pycache__|.git|.venv|venv|node_modules|*.egg-info|.pytest_cache'
else
  find . -maxdepth 4 \
    \( -name '__pycache__' -o -name '.git' -o -name '.venv' -o -name 'venv' \
       -o -name 'node_modules' -o -name '.pytest_cache' \) -prune -o -print
fi

# 【必做】2. 找出所有跟 adapter/source/connector 相關的目錄或檔名，以及現有測試目錄
find . -type d \( -iname '*adapter*' -o -iname '*source*' -o -iname '*connector*' \) \
  -not -path '*/.git/*' -not -path '*/.venv/*'
find . -type d -iname '*test*' -not -path '*/.git/*' -not -path '*/.venv/*'
find . -type d \( -iname '*storage*' -o -iname '*repository*' -o -iname '*db*' -o -iname '*data*' \) \
  -not -path '*/.git/*' -not -path '*/.venv/*' | head -30

# 3. 找出所有跟 FinMind 相關的檔案（確認現有模組完整範圍）
grep -rli 'finmind' --include='*.py' . 2>/dev/null

# 4. 找出現有的抽象基底類/介面定義（Protocol、ABC、abstractmethod）
grep -rn 'class.*Protocol\|class.*ABC\|@abstractmethod' --include='*.py' . 2>/dev/null

# 5. 測試檔案命名慣例
find . -type f -name 'test_*.py' -o -name '*_test.py' | head -50

# 6. 現有 schema / model 定義檔（pydantic、dataclass、SQLAlchemy、ORM）
grep -rln 'class.*BaseModel\|@dataclass\|class.*Base):' --include='*.py' . 2>/dev/null

# 7. 命名與分層規則：頂層目錄、是否有架構說明文件
ls -la
find . -maxdepth 1 -iname 'README*' -o -maxdepth 1 -iname 'ARCHITECTURE*' -o -maxdepth 1 -iname 'CONTRIBUTING*'

# 8. 專案的套件管理與相依（確認 futu-api 是否已裝、目前用的 DB/ORM 套件）
cat pyproject.toml 2>/dev/null || cat setup.py 2>/dev/null || cat requirements.txt 2>/dev/null || cat Pipfile 2>/dev/null
```

---

## Part 2 — Instrument Registry（商品主檔）

### 盤點項目
- [ ] 是否已存在 Instrument Registry（商品主檔）表或模組
- [ ] 若存在：資料表名稱、schema 定義位置（ORM model 檔或 migration 檔）
- [ ] 欄位是否已有 `symbol`、`market`、`source`、`contract`（或等義欄位，命名可能不同）
- [ ] 主鍵設計（`symbol` 單一欄位，還是 `symbol + source` 複合鍵）
- [ ] 目前是否已支援多來源（有沒有 `source` 或 `provider` 這類欄位），或目前預設只有 FinMind 一種來源
- [ ] 是否有現成的新增/查詢 API（供 Futu Adapter 之後複用，而不是重新寫一套）

### 執行指令

```bash
# 【必做】1. 找關鍵字：instrument / registry / symbol_master / security_master 等常見命名
grep -rli 'instrument' --include='*.py' . 2>/dev/null | grep -v '/.git/'
grep -rli 'registry' --include='*.py' . 2>/dev/null | grep -v '/.git/'
grep -rn 'class.*Instrument\|class.*Symbol\|class.*Registry\|class.*Security' --include='*.py' . 2>/dev/null | grep -v '/.git/'

# 【必做】2. 若用 SQLite，找出資料庫檔並列出所有表與 schema
find . -iname '*.db' -o -iname '*.sqlite' -o -iname '*.sqlite3' 2>/dev/null | grep -v '/.git/'
# 找到檔案後（把 <db_path> 換成實際路徑）：
# sqlite3 <db_path> '.tables'
# sqlite3 <db_path> '.schema'

# 【必做】3. 若用 SQLAlchemy / ORM，找 model 定義
grep -rn 'class .*\(Base\)\|class .*\(db.Model\)\|__tablename__' --include='*.py' . 2>/dev/null | grep -v '/.git/'

# 4. 若用 Alembic，列出 migration 目錄與最新幾筆
find . -type d -iname 'migrations' -not -path '*/.git/*'
find . -path '*migrations*' -name '*.py' | sort | tail -20

# 5. 確認現有欄位是否已包含 Symbol/Market/Source/Contract 類似概念
#    （把 <model_file.py> 換成上一步找到的實際檔名）
# grep -n 'Column\|:.*str\|:.*int' <model_file.py>
```

> 若指令 1、2 都沒有任何結果，代表目前專案**尚未有 Instrument Registry**，Part 2 的盤點結論直接記為「不存在，需新建」，不用勉強找。

---

## Part 3 — FinMind 資料庫、連線方式與 Raw/Clean 資料流

### 盤點項目
- [ ] 使用哪種資料庫（SQLite 檔案／PostgreSQL／MySQL／其他）
- [ ] 資料庫連線設定方式（`.env`、`config.py`、環境變數、還是寫死在程式裡）
- [ ] 目前 FinMind 相關資料表有哪些、各自的欄位結構
- [ ] FinMind Adapter 目前的輸入（呼叫參數）與輸出（回傳的資料型別/欄位）
- [ ] Raw（原始 API 回應）與 Clean（標準化後）資料是否已分開儲存，或目前是合併處理、寫入同一張表
- [ ] Token／API Key 的設定方式（環境變數名稱、設定檔位置——**不需要值本身**）
- [ ] 目前主回補作業（backfill job）怎麼呼叫 FinMind Adapter（排程方式、進入點檔案）

### 執行指令

```bash
# 【必做】1. 找到 FinMind Adapter 本體
grep -rli 'finmind' --include='*.py' . 2>/dev/null | grep -v '/.git/'

# 【必做】2. 判斷使用哪種資料庫引擎
grep -rln 'sqlite3\|create_engine\|psycopg2\|pymysql\|MongoClient' --include='*.py' . 2>/dev/null | grep -v '/.git/'
find . -iname '*.db' -o -iname '*.sqlite' -o -iname '*.sqlite3' 2>/dev/null | grep -v '/.git/'
# 找到 SQLite 檔案後（把 <db_path> 換成實際路徑）：
# sqlite3 <db_path> '.tables'
# sqlite3 <db_path> '.schema'

# 3. 查看 FinMind Adapter 的輸入/輸出函式簽名（不看實作細節，只看介面）
#    （把 <finmind_file.py> 換成上一步找到的實際檔名）
# grep -n 'def ' <finmind_file.py>

# 4. 確認 Raw／Clean 資料是否分開存放（目錄名或表名是否有 raw/clean 字樣）
grep -rli 'raw_data\|raw_record\|raw_store\|clean_data\|normalized' --include='*.py' . 2>/dev/null | grep -v '/.git/'
find . -iname '*raw*' -o -iname '*clean*' 2>/dev/null | grep -v '/.git/' | grep -v '__pycache__'

# 【必做】5. Token／連線設定方式（只確認結構，不要輸出實際值）
find . -iname '.env*' -o -iname 'config*.py' -o -iname 'settings*.py' 2>/dev/null | grep -v '/.git/'
# 只看有哪些變數名，不輸出值：
grep -oE '^[A-Z_]+=' .env 2>/dev/null
grep -n 'os.environ\|os.getenv' --include='*.py' -r . 2>/dev/null | grep -v '/.git/' | head -20

# 6. 找主回補作業（backfill）的排程或進入點
grep -rli 'backfill\|scheduler\|cron' --include='*.py' . 2>/dev/null | grep -v '/.git/'
```

---

## 回報格式建議

把三個部分的輸出整理成文字（可直接貼指令輸出，或整理成條列重點），依這個順序給我即可，不用額外排版：

```
## Part 1 目錄結構
<貼上 tree/find 輸出或重點摘要>

## Part 2 Instrument Registry
<有/沒有；若有，貼上 schema>

## Part 3 FinMind DB 與連線
<資料庫類型、資料表 schema、Adapter 輸入輸出、Raw/Clean 儲存方式、Token 設定方式（不含實際值）>
```

若某區塊指令回傳空（例如沒有 Instrument Registry），直接告訴我「不存在」即可，不需自行推斷或用其他資訊補全。

---

## 收集完成後，我會提出的五項設計產出（目前尚未開始，先列出範圍供確認）

1. **`BaseMarketDataAdapter` 共同介面草案**——根據實際 FinMind 現有函式簽名與 Futu 規格，重新設計一套雙邊都能實作的抽象介面（取代 [`futu-realtime-data-adapter-spec-v1.0.md`](./futu-realtime-data-adapter-spec-v1.0.md) 中未經驗證的版本）。
2. **FinMind 與 Futu 的欄位差異表**——並列兩邊實際欄位名/型別/語義，標出名稱不一致或語義不對等的地方。
3. **共用 Schema 與來源專屬欄位的分工**——哪些欄位進 Market Snapshot V1.1 統一格式，哪些保留在來源專屬區塊。
4. **對現有 FinMind 的影響評估**——明確列出若日後要讓 FinMind 也改用共同介面，需改動哪些地方、風險在哪裡（本階段不改，但先評估以利日後決策）。
5. **Futu Adapter MVP-01～MVP-04 實作計畫**——在既有目錄結構下的具體檔案清單與順序，接上已寫好的驗證規則與測試骨架。

我會**只根據你提供的實際指令輸出**產出以上五項，不臆測現有程式內容。

> ✅ **此階段確認一件事**：只盤點與設計，不改現有 FinMind 程式，也不開 PR，待共同介面確認後再進入實作。
