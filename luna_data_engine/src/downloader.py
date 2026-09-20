"""
LUNA Data Engine — Downloader

負責：把「哪些(dataset, stock, 日期區間)要下載」展開成一串請求，
每個請求打完就立刻落地(raw+clean+log)，並更新progress檔——
中斷後重跑，已成功的區塊會被跳過，這就是「續傳機制」。

分年chunk的理由：
  1. 避免單一請求回傳範圍太大、太難重跑。
  2. progress的最小單位是「一個(dataset,stock,年)區塊」，
     中斷後可以精確知道卡在哪一年，不用整個資料集重來。
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from .finmind_client import APIResult, FinMindClient, Outcome
from . import storage


def year_chunks(start_date: str, end_date: str) -> list[tuple[str, str]]:
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    chunks = []
    cur_year = start.year
    while cur_year <= end.year:
        chunk_start = max(start, date(cur_year, 1, 1))
        chunk_end = min(end, date(cur_year, 12, 31))
        chunks.append((chunk_start.isoformat(), chunk_end.isoformat()))
        cur_year += 1
    return chunks


class ProgressStore:
    """JSON檔記錄每個請求區塊的下載狀態，跑很久的全量下載中斷後靠這個接續。"""

    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, dict[str, Any]] = {}
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                self._data = json.load(f)

    @staticmethod
    def _key(dataset: str, data_id: Optional[str], start_date: str, end_date: str) -> str:
        return f"{dataset}|{data_id or 'ALL'}|{start_date}|{end_date}"

    def is_done(self, dataset: str, data_id: Optional[str], start_date: str, end_date: str) -> bool:
        entry = self._data.get(self._key(dataset, data_id, start_date, end_date))
        return bool(entry and entry.get("outcome") in ("success", "success_empty"))

    def mark(self, result: APIResult) -> None:
        key = self._key(result.dataset, result.data_id, result.start_date, result.end_date)
        self._data[key] = {
            "outcome": result.outcome.value,
            "row_count": result.row_count,
            "updated_at": datetime.utcnow().isoformat(),
        }
        self._flush()

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for entry in self._data.values():
            counts[entry["outcome"]] = counts.get(entry["outcome"], 0) + 1
        return counts


def run_download(
    client: FinMindClient,
    conn,
    raw_dir: Path,
    progress: ProgressStore,
    datasets_cfg: dict[str, Any],
    stocks: list[dict[str, str]],
    start_date: str,
    end_date: str,
    logger,
    force: bool = False,
    max_duration_seconds: Optional[float] = None,
) -> dict[str, int]:
    """
    回傳一個簡單的執行摘要 {outcome_name: count}。
    詳細結果都已經寫進 download_log / raw_responses / clean_* 表，
    這裡的回傳值只是給呼叫端印一行摘要用。

    max_duration_seconds：軟性時間上限。實測發現POC在有大量環境（如token失效或某dataset
    需要付費層級）會讓每個請求都跑滿重試+backoff，累積起來很容易撞到執行環境自己的時間上限，
    導致程式被砍斷、什麼報告都沒留下。這裡改成主動在時間快到時「優雅停下」，
    已下載的部分照樣落地、照樣能產出品質報告——中斷不再等於什麼都沒有。
    """
    outcome_counts: dict[str, int] = {}
    start_time = time.monotonic()
    deadline_hit = False

    def _deadline_exceeded() -> bool:
        nonlocal deadline_hit
        if max_duration_seconds is None:
            return False
        if time.monotonic() - start_time >= max_duration_seconds:
            if not deadline_hit:
                logger.warning(
                    f"已達時間上限({max_duration_seconds:.0f}秒)，停止排程新的下載請求，"
                    f"目前為止已完成的部分會照常寫入資料庫並可產出品質報告。"
                )
            deadline_hit = True
            return True
        return False

    def _handle_result(result: APIResult) -> None:
        storage.log_download(conn, result)
        outcome_counts[result.outcome.value] = outcome_counts.get(result.outcome.value, 0) + 1
        if result.is_usable:
            storage.save_raw_response(conn, raw_dir, result)
            if result.data:
                storage.upsert_clean_table(conn, result.dataset, result.data)
                if result.data_id:
                    storage.update_data_availability(conn, result.dataset, result.data_id, result.data)
            progress.mark(result)
        else:
            # 失敗不寫progress為done，下次重跑會再試——這正是「不得將API錯誤誤判為空資料」的具體實作：
            # 失敗的區塊永遠不會被標成success，之後的品質報告與data_availability也不會出現這段區間的資料，
            # 而不是被靜默地當成「這段時間本來就沒資料」。
            logger.error(
                f"[{result.dataset}/{result.data_id}/{result.start_date}~{result.end_date}] "
                f"最終失敗 outcome={result.outcome.value} msg={result.msg or result.error_detail}"
            )

    # 1) TaiwanStockInfo：靜態、全市場，只抓一次（用第一檔股票代碼即可拿到全市場清單）
    info_cfg = datasets_cfg["datasets"].get("TaiwanStockInfo")
    if info_cfg is not None and not _deadline_exceeded():
        key_start, key_end = "static", "static"
        if force or not progress.is_done("TaiwanStockInfo", None, key_start, key_end):
            logger.info("下載 TaiwanStockInfo（股票基本資料）")
            result = client.fetch("TaiwanStockInfo")
            result.start_date, result.end_date = key_start, key_end
            _handle_result(result)

    # 2) TaiwanStockTotalReturnIndex：只需要 TAIEX / TPEx 兩個 data_id，不用逐股下載
    index_cfg = datasets_cfg["datasets"].get("TaiwanStockTotalReturnIndex")
    if index_cfg is not None:
        for idx_id in index_cfg.get("data_id_values", ["TAIEX", "TPEx"]):
            for c_start, c_end in year_chunks(start_date, end_date):
                if _deadline_exceeded():
                    return outcome_counts
                if not force and progress.is_done("TaiwanStockTotalReturnIndex", idx_id, c_start, c_end):
                    continue
                logger.info(f"下載 TaiwanStockTotalReturnIndex data_id={idx_id} {c_start}~{c_end}")
                result = client.fetch("TaiwanStockTotalReturnIndex", idx_id, c_start, c_end)
                _handle_result(result)

    # 3) 其餘逐股資料集
    per_stock_datasets = [
        name
        for name, cfg in datasets_cfg["datasets"].items()
        if cfg.get("scope") == "single_stock_free"
    ]

    for stock in stocks:
        stock_id = stock["stock_id"]
        for dataset in per_stock_datasets:
            for c_start, c_end in year_chunks(start_date, end_date):
                if _deadline_exceeded():
                    return outcome_counts
                if not force and progress.is_done(dataset, stock_id, c_start, c_end):
                    continue
                logger.info(f"下載 {dataset} stock_id={stock_id} {c_start}~{c_end}")
                result = client.fetch(dataset, stock_id, c_start, c_end)
                _handle_result(result)

    return outcome_counts
