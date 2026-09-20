"""
LUNA Data Engine — 儲存層

分兩層，這是任務書明確要求的「原始資料與清洗後資料分層保存」：
  1. raw層：完整保留FinMind的原始回應（JSON檔 + raw_responses表的metadata），
     這樣之後如果清洗邏輯發現有問題，可以回頭重新清洗，不用重新打API。
  2. clean層：每個dataset一張表(clean_<dataset>)，型別轉換過、去重過，
     是後續條件計算/回測真正會用到的表。

另外維護：
  - data_availability：每個(dataset, stock_id)實際涵蓋的日期範圍與筆數，
    對應任務書「每個資料集記錄實際可用日期範圍」的要求。
  - download_log：每一次API呼叫的結果，成功/失敗都留痕，不是只記成功的。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from .finmind_client import APIResult, Outcome

# 每個dataset的「自然鍵」——清洗表去重跟upsert都靠這個
NATURAL_KEYS: dict[str, list[str]] = {
    "TaiwanStockInfo": ["stock_id", "industry_category"],
    "TaiwanStockPrice": ["date", "stock_id"],
    "TaiwanStockPriceAdj": ["date", "stock_id"],
    "TaiwanStockTotalReturnIndex": ["date", "stock_id"],
    "TaiwanStockInstitutionalInvestorsBuySell": ["date", "stock_id", "name"],
    "TaiwanStockMonthRevenue": ["date", "stock_id"],
    "TaiwanStockFinancialStatements": ["date", "stock_id", "type"],
    "TaiwanStockBalanceSheet": ["date", "stock_id", "type"],
    "TaiwanStockCashFlowsStatement": ["date", "stock_id", "type"],
    "TaiwanStockDividend": ["date", "stock_id"],
}


def get_connection(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL;")  # 長時間下載中途被中斷也不容易壞檔
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dataset TEXT NOT NULL,
            data_id TEXT,
            start_date TEXT,
            end_date TEXT,
            fetched_at TEXT NOT NULL,
            outcome TEXT NOT NULL,
            http_status INTEGER,
            json_status INTEGER,
            msg TEXT,
            row_count INTEGER,
            raw_file_path TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS download_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            dataset TEXT NOT NULL,
            data_id TEXT,
            start_date TEXT,
            end_date TEXT,
            outcome TEXT NOT NULL,
            http_status INTEGER,
            json_status INTEGER,
            msg TEXT,
            row_count INTEGER,
            attempts INTEGER,
            error_detail TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS data_availability (
            dataset TEXT NOT NULL,
            stock_id TEXT NOT NULL,
            min_date TEXT,
            max_date TEXT,
            row_count INTEGER,
            last_updated TEXT NOT NULL,
            PRIMARY KEY (dataset, stock_id)
        )
        """
    )
    conn.commit()


def save_raw_response(
    conn: sqlite3.Connection,
    raw_dir: Path,
    result: APIResult,
) -> Optional[Path]:
    """把原始回應存成一個JSON檔，並在raw_responses表留一筆metadata索引。"""
    raw_dir.mkdir(parents=True, exist_ok=True)
    data_id_part = result.data_id or "ALL"
    fname = f"{result.dataset}__{data_id_part}__{result.start_date}_{result.end_date}.json"
    fpath = raw_dir / result.dataset / fname
    fpath.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "dataset": result.dataset,
        "data_id": result.data_id,
        "start_date": result.start_date,
        "end_date": result.end_date,
        "outcome": result.outcome.value,
        "http_status": result.http_status,
        "json_status": result.json_status,
        "msg": result.msg,
        "row_count": result.row_count,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "data": result.data,
    }
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=None)

    conn.execute(
        """
        INSERT INTO raw_responses
            (dataset, data_id, start_date, end_date, fetched_at, outcome,
             http_status, json_status, msg, row_count, raw_file_path)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            result.dataset,
            result.data_id,
            result.start_date,
            result.end_date,
            payload["fetched_at"],
            result.outcome.value,
            result.http_status,
            result.json_status,
            result.msg,
            result.row_count,
            str(fpath),
        ),
    )
    conn.commit()
    return fpath


def log_download(conn: sqlite3.Connection, result: APIResult) -> None:
    conn.execute(
        """
        INSERT INTO download_log
            (ts, dataset, data_id, start_date, end_date, outcome,
             http_status, json_status, msg, row_count, attempts, error_detail)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            datetime.now(timezone.utc).isoformat(),
            result.dataset,
            result.data_id,
            result.start_date,
            result.end_date,
            result.outcome.value,
            result.http_status,
            result.json_status,
            result.msg,
            result.row_count,
            result.attempts,
            result.error_detail,
        ),
    )
    conn.commit()


def upsert_clean_table(conn: sqlite3.Connection, dataset: str, records: list[dict[str, Any]]) -> int:
    """把清洗過的資料寫進 clean_<dataset> 表，依自然鍵去重(先刪同鍵再插入，等同upsert)。"""
    if not records:
        return 0
    df = pd.DataFrame.from_records(records)
    table = f"clean_{dataset}"
    keys = NATURAL_KEYS.get(dataset, list(df.columns[:2]))
    keys = [k for k in keys if k in df.columns]

    df = df.drop_duplicates(subset=keys, keep="last") if keys else df.drop_duplicates()

    existing_tables = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    if table not in existing_tables:
        df.to_sql(table, conn, if_exists="replace", index=False)
        return len(df)

    if keys:
        placeholders = " AND ".join([f'"{k}"=?' for k in keys])
        cur = conn.cursor()
        for _, row in df.iterrows():
            cur.execute(f'DELETE FROM "{table}" WHERE {placeholders}', tuple(row[k] for k in keys))
        conn.commit()

    df.to_sql(table, conn, if_exists="append", index=False)
    return len(df)


def update_data_availability(
    conn: sqlite3.Connection, dataset: str, stock_id: str, records: list[dict[str, Any]]
) -> None:
    if not records:
        return
    dates = [r.get("date") for r in records if r.get("date")]
    if not dates:
        return
    min_d, max_d = min(dates), max(dates)
    cur = conn.execute(
        "SELECT row_count FROM data_availability WHERE dataset=? AND stock_id=?",
        (dataset, stock_id),
    ).fetchone()
    new_count = len(records) if cur is None else cur[0] + len(records)
    conn.execute(
        """
        INSERT INTO data_availability (dataset, stock_id, min_date, max_date, row_count, last_updated)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(dataset, stock_id) DO UPDATE SET
            min_date = MIN(min_date, excluded.min_date),
            max_date = MAX(max_date, excluded.max_date),
            row_count = ?,
            last_updated = excluded.last_updated
        """,
        (dataset, stock_id, min_d, max_d, new_count, datetime.now(timezone.utc).isoformat(), new_count),
    )
    conn.commit()
