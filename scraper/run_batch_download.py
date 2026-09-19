#!/usr/bin/env python3
"""Batch downloader CLI.

Designed for a whole-market run that takes hours, gets interrupted,
and picks back up later — every fetch is checkpointed in SQLite before
the next one starts, so re-running `run` after a Ctrl-C or a crash
only re-fetches what wasn't marked done.

Typical full run, from scratch:

    python run_batch_download.py stock-list
    python run_batch_download.py queue --source finmind --datasets all
    python run_batch_download.py run --source finmind
    python run_batch_download.py export

Or all four in one go:

    python run_batch_download.py all

Goodinfo and the MOPS detail fallback are never included in `all` —
run them explicitly (and, for Goodinfo, only after setting
GOODINFO_ENABLED=1 and reading sources/goodinfo_client.py's module
docstring) if you've decided you want them:

    GOODINFO_ENABLED=1 python run_batch_download.py queue --source goodinfo --datasets cash_flow,monthly_revenue_chart
    GOODINFO_ENABLED=1 python run_batch_download.py run --source goodinfo
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import CONFIG
from core.logger import get_logger
from core.rate_limiter import RunBudgetExceeded
from core.storage import Storage
from sources.finmind_client import FINMIND_DATASETS, FinMindClient
from sources.goodinfo_client import PAGES as GOODINFO_PAGES
from sources.goodinfo_client import GoodinfoClient

logger = get_logger("batch", CONFIG.log_dir)


def cmd_stock_list(storage: Storage, args) -> None:
    client = FinMindClient(CONFIG.finmind)
    logger.info("Fetching full TWSE+TPEx ticker universe from FinMind (TaiwanStockInfo)...")
    stocks = client.fetch_stock_list()
    seen = set()
    for rec in stocks:
        ticker = rec.get("stock_id")
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        storage.upsert_stock(
            ticker=ticker,
            name=rec.get("stock_name", ""),
            market="TPEx" if rec.get("type") == "tpex" else "TWSE",
            industry=rec.get("industry_category", ""),
            stock_type=rec.get("type", ""),
        )
    logger.info("Upserted %d tickers into the stocks table.", len(seen))


def cmd_queue(storage: Storage, args) -> None:
    tickers = _resolve_tickers(storage, args)
    if args.source == "finmind":
        datasets = list(FINMIND_DATASETS.keys()) if args.datasets == ["all"] else args.datasets
        unknown = set(datasets) - set(FINMIND_DATASETS.keys())
        if unknown:
            raise SystemExit(f"Unknown FinMind dataset(s): {unknown}. Valid: {list(FINMIND_DATASETS.keys())}")
        jobs = [(t, d, "finmind") for t in tickers for d in datasets]
    elif args.source == "goodinfo":
        datasets = list(GOODINFO_PAGES.keys()) if args.datasets == ["all"] else args.datasets
        unknown = set(datasets) - set(GOODINFO_PAGES.keys())
        if unknown:
            raise SystemExit(f"Unknown Goodinfo page(s): {unknown}. Valid: {list(GOODINFO_PAGES.keys())}")
        jobs = [(t, d, "goodinfo") for t in tickers for d in datasets]
    else:
        raise SystemExit(f"--source {args.source} has no queue support yet (mops is fetched ad-hoc, not batch-queued).")

    n = storage.init_checkpoint_jobs(jobs)
    logger.info("Queued %d (ticker, dataset, source) jobs (existing ones left untouched).", n)
    logger.info("Checkpoint summary: %s", storage.checkpoint_summary())


def cmd_run(storage: Storage, args) -> None:
    pending = storage.pending_jobs(source=args.source)
    if not pending:
        logger.info("No pending jobs for source=%s. Run `queue` first.", args.source)
        return
    logger.info("%d pending jobs for source=%s", len(pending), args.source)

    if args.source == "finmind":
        client = FinMindClient(CONFIG.finmind)
        _run_finmind(storage, client, pending)
    elif args.source == "goodinfo":
        client = GoodinfoClient(CONFIG.goodinfo)
        if not CONFIG.goodinfo.enabled:
            logger.warning(
                "GOODINFO_ENABLED is not set — every job below will be skipped, not fetched. "
                "Read sources/goodinfo_client.py before setting it."
            )
        _run_goodinfo(storage, client, pending)
    else:
        raise SystemExit(f"--source {args.source} not supported by `run`.")


def _run_finmind(storage: Storage, client: FinMindClient, jobs: list[tuple[str, str, str]]) -> None:
    method_by_dataset = {
        "cash_flow_statement": client.cash_flow_statement,
        "income_statement": client.income_statement,
        "balance_sheet": client.balance_sheet,
        "monthly_revenue": client.monthly_revenue,
        "dividend": client.dividend,
        "price": client.price,
    }
    done = errored = 0
    for ticker, dataset, source in jobs:
        try:
            pairs = method_by_dataset[dataset](ticker)
            for record_date, payload in pairs:
                storage.save_record(source, dataset, ticker, record_date, payload)
            storage.conn.commit()
            storage.mark_checkpoint(ticker, dataset, source, "done")
            done += 1
            if done % 50 == 0:
                logger.info("progress: %d done, %d errored, %d remaining", done, errored, len(jobs) - done - errored)
        except KeyboardInterrupt:
            logger.warning("Interrupted — already-completed jobs stay checkpointed; re-run `run` to resume.")
            raise
        except Exception as exc:
            logger.error("failed ticker=%s dataset=%s: %s", ticker, dataset, exc)
            storage.mark_checkpoint(ticker, dataset, source, "error", str(exc))
            errored += 1
    logger.info("finmind run complete: %d done, %d errored", done, errored)


def _run_goodinfo(storage: Storage, client: GoodinfoClient, jobs: list[tuple[str, str, str]]) -> None:
    done = errored = skipped = 0
    for ticker, page_key, source in jobs:
        try:
            result = client.fetch_page(page_key, ticker)
            if result is None:
                storage.mark_checkpoint(ticker, page_key, source, "skipped")
                skipped += 1
                continue
            storage.save_record(source, page_key, ticker, ticker, result)
            storage.conn.commit()
            storage.mark_checkpoint(ticker, page_key, source, "done")
            done += 1
        except RunBudgetExceeded as exc:
            logger.warning("%s — stopping this run; already-fetched pages are saved.", exc)
            break
        except KeyboardInterrupt:
            logger.warning("Interrupted — already-completed jobs stay checkpointed; re-run `run` to resume.")
            raise
        except Exception as exc:
            logger.error("failed ticker=%s page=%s: %s", ticker, page_key, exc)
            storage.mark_checkpoint(ticker, page_key, source, "error", str(exc))
            errored += 1
    logger.info("goodinfo run complete: %d done, %d errored, %d skipped", done, errored, skipped)


def cmd_export(storage: Storage, args) -> None:
    counts = storage.export_all(CONFIG.export_dir)
    manifest_path = storage.build_manifest(CONFIG.export_dir)
    total_records = sum(sum(d.values()) for d in counts.values())
    logger.info(
        "Exported %d tickers, %d total records, to %s. Manifest: %s",
        len(counts), total_records, CONFIG.export_dir, manifest_path,
    )


def cmd_all(storage: Storage, args) -> None:
    cmd_stock_list(storage, args)
    args.source = "finmind"
    args.datasets = ["all"]
    cmd_queue(storage, args)
    cmd_run(storage, args)
    cmd_export(storage, args)


def _resolve_tickers(storage: Storage, args) -> list[str]:
    if args.tickers_file:
        tickers = [line.strip() for line in Path(args.tickers_file).read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        tickers = storage.list_tickers()
        if not tickers:
            raise SystemExit("No tickers in the stocks table yet — run `stock-list` first, or pass --tickers-file.")
    if args.limit:
        tickers = tickers[: args.limit]
    return tickers


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("stock-list", help="Refresh the full TWSE+TPEx ticker universe from FinMind.")

    p_queue = sub.add_parser("queue", help="Build the (ticker, dataset, source) job matrix.")
    p_queue.add_argument("--source", choices=["finmind", "goodinfo"], required=True)
    p_queue.add_argument("--datasets", nargs="+", default=["all"])
    p_queue.add_argument("--tickers-file", help="One ticker per line; default is every ticker in the stocks table.")
    p_queue.add_argument("--limit", type=int, help="Only queue the first N tickers (useful for a test run).")

    p_run = sub.add_parser("run", help="Process pending jobs for one source.")
    p_run.add_argument("--source", choices=["finmind", "goodinfo"], required=True)

    sub.add_parser("export", help="Export the SQLite DB to per-ticker JSON/CSV + a manifest.")

    p_all = sub.add_parser("all", help="stock-list + queue(finmind, all datasets, full market) + run + export.")

    args = parser.parse_args()

    with Storage(CONFIG.db_path) as storage:
        {
            "stock-list": cmd_stock_list,
            "queue": cmd_queue,
            "run": cmd_run,
            "export": cmd_export,
            "all": cmd_all,
        }[args.command](storage, args)


if __name__ == "__main__":
    main()
