"""
LUNA Data Engine — 資料品質檢查

任務書要求檢查：缺失日期、重複資料、股票代號變更、上市櫃狀態、除權息還原、
成交量單位、財報發布時間、資料來源一致性、API額度與錯誤回應、斷線續傳與重複下載。

這個模組專注在「下載完之後、可以自動化檢驗」的那部分：
  - 缺失值 / 重複值 / 異常值
  - 交易日完整性(用已下載的TaiwanStockPrice本身反推交易日曆，不假設外部日曆一定準)
  - 各資料集/各股票實際可用日期範圍
  - download_log 的錯誤彙總(API額度、逾時等，不能只看成功筆數)

股票代號變更、上市櫃狀態、除權息還原方法等，屬於需要人工對照公告或跨期比對才能
下定論的題目，這裡先把「資料是否完整/乾淨」的基本盤顧好，那些留在報告的
「尚未涵蓋」段落明講，不假裝已經處理。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from .storage import NATURAL_KEYS


@dataclass
class DatasetQualityResult:
    dataset: str
    stock_id: str
    row_count: int = 0
    min_date: Optional[str] = None
    max_date: Optional[str] = None
    duplicate_count: int = 0
    missing_trading_days: int = 0
    missing_trading_day_list: list[str] = field(default_factory=list)
    outlier_count: int = 0
    outlier_examples: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def build_trading_calendar(conn: sqlite3.Connection) -> list[str]:
    """用已下載的TaiwanStockPrice全部股票的date聯集，當作「有交易的日期」基準。
    這是保守作法：只有真的抓到資料的股票才會貢獻交易日，不去外部抓官方行事曆，
    避免這個skeleton多一個外部依賴；缺點是如果所有股票剛好都缺同一天，
    這個方法看不出來——report裡會註明這個限制。
    """
    if not _table_exists(conn, "clean_TaiwanStockPrice"):
        return []
    df = pd.read_sql("SELECT DISTINCT date FROM clean_TaiwanStockPrice", conn)
    return sorted(df["date"].tolist())


def check_dataset_for_stock(
    conn: sqlite3.Connection,
    dataset: str,
    stock_id: str,
    trading_calendar: list[str],
) -> Optional[DatasetQualityResult]:
    table = f"clean_{dataset}"
    if not _table_exists(conn, table):
        return None

    df = pd.read_sql(f'SELECT * FROM "{table}" WHERE stock_id=?', conn, params=(stock_id,))
    if df.empty:
        return DatasetQualityResult(dataset=dataset, stock_id=stock_id, row_count=0,
                                     notes=["此股票在此資料集沒有任何已下載資料"])

    result = DatasetQualityResult(dataset=dataset, stock_id=stock_id, row_count=len(df))

    if "date" in df.columns:
        result.min_date = df["date"].min()
        result.max_date = df["date"].max()

    # 重複值：用跟clean表upsert時「同一把」自然鍵判斷，確保去重定義前後一致，
    # 不會發生「clean表沒重複，品質報告卻說重複」這種自相矛盾的結果。
    key_cols = [c for c in NATURAL_KEYS.get(dataset, ["date", "stock_id"]) if c in df.columns]
    if key_cols:
        dup_mask = df.duplicated(subset=key_cols, keep=False)
        result.duplicate_count = int(dup_mask.sum())

    # 缺失交易日：只對日頻資料集做，且只看該股票自己資料涵蓋的min~max範圍內
    if dataset in ("TaiwanStockPrice", "TaiwanStockPriceAdj") and "date" in df.columns and trading_calendar:
        stock_dates = set(df["date"])
        relevant_calendar = [d for d in trading_calendar if result.min_date <= d <= result.max_date]
        missing = sorted(set(relevant_calendar) - stock_dates)
        result.missing_trading_days = len(missing)
        result.missing_trading_day_list = missing[:20]  # 報告裡只列前20筆，避免爆版面

    # 異常值：價格類資料集才檢查
    if dataset in ("TaiwanStockPrice", "TaiwanStockPriceAdj") and {"close", "date"}.issubset(df.columns):
        df_sorted = df.sort_values("date")
        non_positive = df_sorted[df_sorted["close"] <= 0]
        if not non_positive.empty:
            result.outlier_count += len(non_positive)
            result.outlier_examples += [
                f"{r['date']} close={r['close']} (非正值)" for _, r in non_positive.head(5).iterrows()
            ]

        df_sorted["prev_close"] = df_sorted["close"].shift(1)
        df_sorted["ret"] = (df_sorted["close"] - df_sorted["prev_close"]) / df_sorted["prev_close"]
        # 台股漲跌幅限制通常為10%，超過10.5%的單日報酬視為可疑（除權息缺調整或資料錯誤）
        suspicious = df_sorted[df_sorted["ret"].abs() > 0.105]
        if not suspicious.empty:
            result.outlier_count += len(suspicious)
            result.outlier_examples += [
                f"{r['date']} 單日報酬={r['ret']:.1%} (超過±10.5%，需人工核對是否為除權息或資料錯誤)"
                for _, r in suspicious.head(5).iterrows()
            ]

    return result


def summarize_download_log(conn: sqlite3.Connection) -> pd.DataFrame:
    if not _table_exists(conn, "download_log"):
        return pd.DataFrame()
    df = pd.read_sql("SELECT outcome, COUNT(*) as n FROM download_log GROUP BY outcome ORDER BY n DESC", conn)
    return df


def list_failed_requests(conn: sqlite3.Connection, limit: int = 50) -> pd.DataFrame:
    if not _table_exists(conn, "download_log"):
        return pd.DataFrame()
    return pd.read_sql(
        """
        SELECT ts, dataset, data_id, start_date, end_date, outcome, http_status, json_status, msg, attempts, error_detail
        FROM download_log
        WHERE outcome NOT IN ('success', 'success_empty')
        ORDER BY ts DESC
        LIMIT ?
        """,
        conn,
        params=(limit,),
    )


def data_availability_table(conn: sqlite3.Connection) -> pd.DataFrame:
    if not _table_exists(conn, "data_availability"):
        return pd.DataFrame()
    return pd.read_sql("SELECT * FROM data_availability ORDER BY dataset, stock_id", conn)


def run_full_quality_check(
    conn: sqlite3.Connection, datasets: list[str], stocks: list[dict[str, str]]
) -> list[DatasetQualityResult]:
    calendar = build_trading_calendar(conn)
    results: list[DatasetQualityResult] = []
    for dataset in datasets:
        for stock in stocks:
            r = check_dataset_for_stock(conn, dataset, stock["stock_id"], calendar)
            if r is not None:
                results.append(r)
    return results


def render_markdown_report(
    run_name: str,
    results: list[DatasetQualityResult],
    download_summary: pd.DataFrame,
    failed_requests: pd.DataFrame,
    availability: pd.DataFrame,
) -> str:
    lines: list[str] = []
    lines.append(f"# LUNA Data Engine 資料品質報告 — {run_name}")
    lines.append(f"\n產出時間：{datetime.utcnow().isoformat()}Z\n")

    lines.append("## 1. 下載結果彙總\n")
    if download_summary.empty:
        lines.append("（沒有download_log資料，可能尚未執行過下載）\n")
    else:
        lines.append("| outcome | 次數 |")
        lines.append("|---|---|")
        for _, row in download_summary.iterrows():
            lines.append(f"| {row['outcome']} | {row['n']} |")
        total = download_summary["n"].sum()
        success = download_summary[download_summary["outcome"].isin(["success", "success_empty"])]["n"].sum()
        fail = total - success
        lines.append(f"\n總請求數：{total}，成功：{success}，失敗（含最終放棄）：{fail}\n")

    lines.append("\n## 2. 失敗/異常請求明細（最新50筆）\n")
    if failed_requests.empty:
        lines.append("（無失敗請求，或尚未執行下載）\n")
    else:
        lines.append("| 時間 | dataset | data_id | 區間 | outcome | http | json_status | msg |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for _, r in failed_requests.iterrows():
            lines.append(
                f"| {r['ts']} | {r['dataset']} | {r['data_id']} | {r['start_date']}~{r['end_date']} "
                f"| {r['outcome']} | {r['http_status']} | {r['json_status']} | {str(r['msg'])[:60]} |"
            )

    lines.append("\n## 3. 各資料集實際可用日期範圍（data_availability）\n")
    if availability.empty:
        lines.append("（尚無資料）\n")
    else:
        lines.append("| dataset | stock_id | min_date | max_date | row_count |")
        lines.append("|---|---|---|---|---|")
        for _, r in availability.iterrows():
            lines.append(f"| {r['dataset']} | {r['stock_id']} | {r['min_date']} | {r['max_date']} | {r['row_count']} |")

    lines.append("\n## 4. 逐股/逐資料集品質檢查\n")
    lines.append("| dataset | stock_id | 筆數 | 日期範圍 | 重複筆數 | 缺失交易日數 | 異常值數 | 備註 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for r in results:
        date_range = f"{r.min_date}~{r.max_date}" if r.min_date else "—"
        note = "; ".join(r.notes) if r.notes else ""
        lines.append(
            f"| {r.dataset} | {r.stock_id} | {r.row_count} | {date_range} | {r.duplicate_count} "
            f"| {r.missing_trading_days} | {r.outlier_count} | {note} |"
        )

    outlier_lines = []
    for r in results:
        if r.outlier_examples:
            outlier_lines.append(f"### {r.dataset} / {r.stock_id}")
            outlier_lines.extend([f"- {ex}" for ex in r.outlier_examples])
    if outlier_lines:
        lines.append("\n## 5. 異常值明細\n")
        lines.extend(outlier_lines)

    missing_lines = []
    for r in results:
        if r.missing_trading_day_list:
            missing_lines.append(f"### {r.dataset} / {r.stock_id}（僅列前20筆）")
            missing_lines.append(", ".join(r.missing_trading_day_list))
    if missing_lines:
        lines.append("\n## 6. 缺失交易日明細\n")
        lines.extend(missing_lines)

    lines.append("\n## 7. 已知限制（本skeleton尚未處理，需要下一階段補上）\n")
    lines.append("- 交易日曆是用已下載股票的TaiwanStockPrice聯集反推，不是官方交易日曆，"
                  "若剛好某天全部樣本股都缺資料會偵測不到。")
    lines.append("- 股票代號變更、上市櫃狀態異動（如新光金併入台新金）尚未做自動比對，"
                  "需要人工核對TaiwanStockInfo的type欄位與代號沿革。")
    lines.append("- 除權息還原方法尚未跟TaiwanStockPriceAdj的實際還原基準日交叉驗證。")
    lines.append("- 財報/月營收的Point-in-Time對齊（用公告日而非資料所屬期間）尚未在下載階段實作，"
                  "目前存的是FinMind回傳的date欄位（通常是財報所屬期間末日），"
                  "條件計算階段務必額外處理公告延遲，否則會有未來資訊洩漏風險。")

    return "\n".join(lines)
