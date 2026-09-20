"""
LUNA Data Engine — FinMind API Client

任務書明確要求（原文）：
  「FinMind額度用完時，可能回傳HTTP 200，但JSON body內部status/msg欄位顯示錯誤。
   因此不能只依賴HTTP status code判斷下載成功，必須同時檢查HTTP狀態、JSON格式、
   status欄位、msg錯誤訊息、實際資料筆數、日期覆蓋範圍。若失敗，必須留下錯誤紀錄，
   不能將錯誤回應當成空資料。」

這個模組就是把這條規則寫死成程式邏輯，不是靠人工記得檢查。

設計重點：
- APIResult 是唯一的回傳型別，呼叫端永遠看 result.outcome 決定要不要當成功，
  不會有「HTTP 200就當成功」這種捷徑。
- transport 參數可以注入假的請求函式，離線測試不需要真的連網路
  （這個沙盒環境連不到FinMind，offline測試就是靠這個機制）。
- 速率限制用簡單的滑動視窗計數器，自己在接近上限前就放慢，
  不是等被FinMind拒絕才反應。
"""
from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

import requests


class Outcome(str, Enum):
    SUCCESS = "success"            # HTTP 200 + json status 200 + msg success（不論data是否為空）
    SUCCESS_EMPTY = "success_empty"  # 同上，但data陣列確實是空的（真實無資料，非錯誤）
    API_ERROR = "api_error"        # HTTP 200 但 json status/msg 顯示錯誤（含額度用盡）—— 這正是任務書點名的陷阱
    HTTP_ERROR = "http_error"      # HTTP status != 200
    NETWORK_ERROR = "network_error"  # 連線逾時/斷線/DNS等
    PARSE_ERROR = "parse_error"    # 回應不是合法JSON
    GAVE_UP = "gave_up"            # 重試多次後仍失敗


@dataclass
class APIResult:
    outcome: Outcome
    dataset: str
    data_id: Optional[str]
    start_date: Optional[str]
    end_date: Optional[str]
    http_status: Optional[int] = None
    json_status: Optional[int] = None
    msg: Optional[str] = None
    row_count: int = 0
    data: list[dict[str, Any]] = field(default_factory=list)
    attempts: int = 0
    error_detail: Optional[str] = None

    @property
    def is_usable(self) -> bool:
        """只有這兩種outcome代表回應內容可以放心寫進資料庫。"""
        return self.outcome in (Outcome.SUCCESS, Outcome.SUCCESS_EMPTY)


# transport的型別：輸入params，回傳 (http_status_code, response_text)
Transport = Callable[[str, dict[str, Any], int], tuple[int, str]]


def _default_transport(base_url: str, params: dict[str, Any], timeout: int) -> tuple[int, str]:
    resp = requests.get(base_url, params=params, timeout=timeout)
    return resp.status_code, resp.text


class RateLimiter:
    """滑動一小時視窗的簡單節流器：接近額度上限就主動睡，不等被拒絕。"""

    def __init__(self, max_per_hour: int):
        self.max_per_hour = max_per_hour
        self._timestamps: deque[float] = deque()

    def wait_if_needed(self) -> None:
        now = time.time()
        window_start = now - 3600
        while self._timestamps and self._timestamps[0] < window_start:
            self._timestamps.popleft()
        if len(self._timestamps) >= self.max_per_hour:
            sleep_for = self._timestamps[0] + 3600 - now
            if sleep_for > 0:
                time.sleep(sleep_for)
        self._timestamps.append(time.time())


class FinMindClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        requests_per_hour: int = 550,
        timeout_seconds: int = 20,
        max_retries: int = 5,
        transport: Transport = _default_transport,
        logger=None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.transport = transport
        self.rate_limiter = RateLimiter(requests_per_hour)
        self.logger = logger
        self.sleep_fn = sleep_fn

    def _log(self, level: str, msg: str) -> None:
        if self.logger:
            getattr(self.logger, level)(msg)

    def fetch(
        self,
        dataset: str,
        data_id: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> APIResult:
        params: dict[str, Any] = {"dataset": dataset, "token": self.token}
        if data_id:
            params["data_id"] = data_id
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date

        last_result: Optional[APIResult] = None
        for attempt in range(1, self.max_retries + 1):
            self.rate_limiter.wait_if_needed()
            result = self._single_request(dataset, data_id, start_date, end_date, params, attempt)
            last_result = result

            if result.is_usable:
                return result

            if result.outcome in (Outcome.HTTP_ERROR, Outcome.NETWORK_ERROR, Outcome.API_ERROR):
                backoff = min(2 ** attempt, 60)
                self._log(
                    "warning",
                    f"[{dataset}/{data_id}] attempt {attempt}/{self.max_retries} "
                    f"outcome={result.outcome.value} msg={result.msg or result.error_detail} "
                    f"→ 等待{backoff}s後重試",
                )
                self.sleep_fn(backoff)
                continue

            # PARSE_ERROR 通常不是暫時性問題（回應格式本身有問題），一樣重試但不多耗時間
            self._log(
                "warning",
                f"[{dataset}/{data_id}] attempt {attempt}/{self.max_retries} "
                f"outcome={result.outcome.value} → 重試",
            )
            self.sleep_fn(min(2 * attempt, 20))

        assert last_result is not None
        last_result.outcome = Outcome.GAVE_UP
        self._log(
            "error",
            f"[{dataset}/{data_id}] 重試{self.max_retries}次後仍失敗，"
            f"最後一次outcome={last_result.outcome.value} "
            f"http={last_result.http_status} json_status={last_result.json_status} msg={last_result.msg} "
            f"— 記錄為失敗，不當成空資料",
        )
        return last_result

    def _single_request(
        self,
        dataset: str,
        data_id: Optional[str],
        start_date: Optional[str],
        end_date: Optional[str],
        params: dict[str, Any],
        attempt: int,
    ) -> APIResult:
        base = dict(dataset=dataset, data_id=data_id, start_date=start_date, end_date=end_date, attempts=attempt)

        try:
            http_status, text = self.transport(self.base_url, params, self.timeout_seconds)
        except requests.exceptions.RequestException as e:
            return APIResult(outcome=Outcome.NETWORK_ERROR, error_detail=str(e), **base)

        if http_status != 200:
            return APIResult(outcome=Outcome.HTTP_ERROR, http_status=http_status, error_detail=text[:500], **base)

        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return APIResult(
                outcome=Outcome.PARSE_ERROR, http_status=http_status, error_detail=text[:500], **base
            )

        json_status = payload.get("status")
        msg = payload.get("msg")
        data = payload.get("data")

        # 這是任務書點名的核心規則：HTTP 200不代表成功，要看JSON內部的status/msg
        if json_status != 200 or (isinstance(msg, str) and msg.lower() != "success"):
            return APIResult(
                outcome=Outcome.API_ERROR,
                http_status=http_status,
                json_status=json_status,
                msg=msg,
                **base,
            )

        if data is None:
            return APIResult(
                outcome=Outcome.API_ERROR,
                http_status=http_status,
                json_status=json_status,
                msg=msg,
                error_detail="status/msg顯示成功但沒有data欄位，視為異常回應非空資料",
                **base,
            )

        if len(data) == 0:
            return APIResult(
                outcome=Outcome.SUCCESS_EMPTY,
                http_status=http_status,
                json_status=json_status,
                msg=msg,
                row_count=0,
                data=[],
                **base,
            )

        return APIResult(
            outcome=Outcome.SUCCESS,
            http_status=http_status,
            json_status=json_status,
            msg=msg,
            row_count=len(data),
            data=data,
            **base,
        )
