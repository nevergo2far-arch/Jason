"""Storage layer: one SQLite database as the source of truth, with
JSON/CSV export on top for the downstream AI analyst.

Schema is deliberately generic (`raw_records` keyed by
source/dataset/ticker/record_date, payload as JSON) rather than one
strictly-typed table per dataset. Reasoning: FinMind, TWSE/TPEx open
data and MOPS each shape the "same" concept (e.g. a cash flow
statement line) slightly differently, and the set of datasets pulled
in here is expected to grow. A generic table means adding a new
dataset never requires a schema migration; a `checkpoint` table next
to it is what makes whole-market batch runs resumable.
"""
from __future__ import annotations

import csv
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS stocks (
    ticker TEXT PRIMARY KEY,
    name TEXT,
    market TEXT,
    industry TEXT,
    stock_type TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS raw_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    dataset TEXT NOT NULL,
    ticker TEXT NOT NULL,
    record_date TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE(source, dataset, ticker, record_date)
);
CREATE INDEX IF NOT EXISTS idx_raw_records_ticker_dataset
    ON raw_records(ticker, dataset);

CREATE TABLE IF NOT EXISTS checkpoint (
    ticker TEXT NOT NULL,
    dataset TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (ticker, dataset, source)
);
CREATE INDEX IF NOT EXISTS idx_checkpoint_status ON checkpoint(status);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- stocks -----------------------------------------------------
    def upsert_stock(self, ticker: str, name: str, market: str, industry: str, stock_type: str) -> None:
        self.conn.execute(
            """
            INSERT INTO stocks (ticker, name, market, industry, stock_type, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                name=excluded.name, market=excluded.market,
                industry=excluded.industry, stock_type=excluded.stock_type,
                updated_at=excluded.updated_at
            """,
            (ticker, name, market, industry, stock_type, _now()),
        )
        self.conn.commit()

    def list_tickers(self) -> list[str]:
        rows = self.conn.execute("SELECT ticker FROM stocks ORDER BY ticker").fetchall()
        return [r["ticker"] for r in rows]

    # -- raw records --------------------------------------------------
    def save_record(self, source: str, dataset: str, ticker: str, record_date: str, payload: dict[str, Any]) -> None:
        self.conn.execute(
            """
            INSERT INTO raw_records (source, dataset, ticker, record_date, payload_json, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, dataset, ticker, record_date) DO UPDATE SET
                payload_json=excluded.payload_json, fetched_at=excluded.fetched_at
            """,
            (source, dataset, ticker, record_date, json.dumps(payload, ensure_ascii=False), _now()),
        )

    def save_records(self, source: str, dataset: str, ticker: str, records: Iterable[dict[str, Any]], date_key: str) -> int:
        """Save many records for one (source, dataset, ticker) at once.

        `date_key` is the field name inside each record to use as the
        de-dup key (e.g. "date", "fiscal_year", "revenue_year_month").
        Commits once at the end — call sites should not save() per row.
        """
        count = 0
        for rec in records:
            record_date = str(rec.get(date_key, ""))
            if not record_date:
                continue
            self.save_record(source, dataset, ticker, record_date, rec)
            count += 1
        self.conn.commit()
        return count

    def get_records(self, ticker: str, dataset: str, source: str | None = None) -> list[dict[str, Any]]:
        if source:
            rows = self.conn.execute(
                "SELECT payload_json FROM raw_records WHERE ticker=? AND dataset=? AND source=? ORDER BY record_date",
                (ticker, dataset, source),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT payload_json FROM raw_records WHERE ticker=? AND dataset=? ORDER BY record_date",
                (ticker, dataset),
            ).fetchall()
        return [json.loads(r["payload_json"]) for r in rows]

    def datasets_for_ticker(self, ticker: str) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT dataset FROM raw_records WHERE ticker=? ORDER BY dataset", (ticker,)
        ).fetchall()
        return [r["dataset"] for r in rows]

    # -- checkpoint / resume -----------------------------------------
    def init_checkpoint_jobs(self, jobs: Iterable[tuple[str, str, str]]) -> int:
        """jobs: iterable of (ticker, dataset, source). Existing rows
        (already pending/done/error from a prior run) are left alone —
        that's what makes resuming safe."""
        now = _now()
        rows = [(t, d, s, "pending", now) for t, d, s in jobs]
        self.conn.executemany(
            """
            INSERT INTO checkpoint (ticker, dataset, source, status, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(ticker, dataset, source) DO NOTHING
            """,
            rows,
        )
        self.conn.commit()
        return len(rows)

    def mark_checkpoint(self, ticker: str, dataset: str, source: str, status: str, error: str | None = None) -> None:
        self.conn.execute(
            """
            UPDATE checkpoint
            SET status=?, last_error=?, attempts=attempts+1, updated_at=?
            WHERE ticker=? AND dataset=? AND source=?
            """,
            (status, error, _now(), ticker, dataset, source),
        )
        self.conn.commit()

    def pending_jobs(self, source: str | None = None) -> list[tuple[str, str, str]]:
        if source:
            rows = self.conn.execute(
                "SELECT ticker, dataset, source FROM checkpoint WHERE status='pending' AND source=? ORDER BY ticker",
                (source,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT ticker, dataset, source FROM checkpoint WHERE status='pending' ORDER BY ticker"
            ).fetchall()
        return [(r["ticker"], r["dataset"], r["source"]) for r in rows]

    def checkpoint_summary(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) c FROM checkpoint GROUP BY status").fetchall()
        return {r["status"]: r["c"] for r in rows}

    # -- export --------------------------------------------------------
    def export_ticker(self, ticker: str, export_dir: Path) -> dict[str, int]:
        """Write one JSON + one CSV file per dataset for this ticker
        under export_dir/{ticker}/. Returns {dataset: record_count}."""
        ticker_dir = export_dir / ticker
        ticker_dir.mkdir(parents=True, exist_ok=True)
        counts: dict[str, int] = {}
        for dataset in self.datasets_for_ticker(ticker):
            records = self.get_records(ticker, dataset)
            counts[dataset] = len(records)
            (ticker_dir / f"{dataset}.json").write_text(
                json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            _write_csv(ticker_dir / f"{dataset}.csv", records)
        return counts

    def export_all(self, export_dir: Path) -> dict[str, dict[str, int]]:
        result = {}
        for ticker in self.list_tickers():
            result[ticker] = self.export_ticker(ticker, export_dir)
        return result

    def build_manifest(self, export_dir: Path) -> Path:
        """Writes export_dir/index.json describing what's available,
        so the downstream AI analyst can discover coverage without
        scanning the whole tree."""
        manifest: dict[str, Any] = {"generated_at": _now(), "tickers": {}}
        rows = self.conn.execute(
            """
            SELECT ticker, dataset, COUNT(*) n, MAX(fetched_at) latest
            FROM raw_records GROUP BY ticker, dataset
            """
        ).fetchall()
        per_ticker: dict[str, dict[str, Any]] = defaultdict(dict)
        for r in rows:
            per_ticker[r["ticker"]][r["dataset"]] = {"record_count": r["n"], "last_fetched": r["latest"]}
        stock_rows = self.conn.execute("SELECT ticker, name, market, industry FROM stocks").fetchall()
        stock_info = {r["ticker"]: dict(r) for r in stock_rows}
        for ticker, datasets in per_ticker.items():
            manifest["tickers"][ticker] = {
                "name": stock_info.get(ticker, {}).get("name"),
                "market": stock_info.get(ticker, {}).get("market"),
                "industry": stock_info.get(ticker, {}).get("industry"),
                "datasets": datasets,
            }
        export_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = export_dir / "index.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        return manifest_path


def _write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        path.write_text("", encoding="utf-8")
        return
    fieldnames: list[str] = []
    seen = set()
    for rec in records:
        for k in rec.keys():
            if k not in seen:
                seen.add(k)
                fieldnames.append(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for rec in records:
            writer.writerow(rec)
