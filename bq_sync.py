# -*- coding: utf-8 -*-
"""
Sync local mis_data (SQLite) -> BigQuery combined_report_temp.

Before each sync:
  1. Read MAX(Дата визита) in BigQuery
  2. Upload only local dates AFTER that max (no historical backfill)
  3. Delete from BQ dates older than the 1st day of the max-date month

Commands:
  compare          — compare row counts / sums per date (overlap)
  sync --dry-run   — show what would be uploaded/deleted
  sync             — backup BQ table, delete stale dates, upload new
  rollback         — restore from last backup (manifest in data/bq_sync/)
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from google.cloud import bigquery

from bq_schema import BQ_ALL_COLUMNS, DATE_COL, bq_table_schema
from bq_transform import LOCAL_DB_DEFAULT, load_local_period, transform_to_bq

load_dotenv()

PROJECT_ID = os.getenv("BQ_PROJECT_ID", "carbide-datum-383616")
DATASET_ID = os.getenv("BQ_DATASET_ID", "kravira_last")
TABLE_ID = os.getenv("BQ_TABLE_ID", "combined_report_temp")
CREDENTIALS_PATH = os.getenv(
    "GOOGLE_APPLICATION_CREDENTIALS",
    "/Users/pavel/Kravira_Work/Jupyter/PL/carbide-datum-383616-8960c7f83f5b.json",
)

MANIFEST_DIR = Path(__file__).resolve().parent / "data" / "bq_sync"
SUM_COL = "Стоимость услуги со скидкой"
FULL_TABLE = f"{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}"


def _ensure_credentials() -> None:
    if CREDENTIALS_PATH and Path(CREDENTIALS_PATH).exists():
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = CREDENTIALS_PATH


def get_client() -> bigquery.Client:
    _ensure_credentials()
    return bigquery.Client(project=PROJECT_ID)


def _local_date_stats(db_path: Path) -> pd.DataFrame:
    with sqlite3.connect(db_path) as conn:
        q = f'''
        SELECT date("{DATE_COL}") AS d,
               COUNT(*) AS cnt,
               ROUND(SUM(CAST("{SUM_COL}" AS REAL)), 2) AS s
        FROM mis_data
        GROUP BY 1
        ORDER BY 1
        '''
        return pd.read_sql(q, conn)


def _bq_max_date(client: bigquery.Client) -> date | None:
    q = f"SELECT MAX(`{DATE_COL}`) AS d FROM `{FULL_TABLE}`"
    row = list(client.query(q).result())
    if not row or row[0].d is None:
        return None
    return row[0].d


def _bq_date_stats(client: bigquery.Client) -> pd.DataFrame:
    q = f"""
    SELECT `{DATE_COL}` AS d, COUNT(*) AS cnt,
           ROUND(SUM(`{SUM_COL}`), 2) AS s
    FROM `{FULL_TABLE}`
    GROUP BY 1
    ORDER BY 1
    """
    return client.query(q).to_dataframe()


def compare(db_path: Path = LOCAL_DB_DEFAULT) -> dict:
    client = get_client()
    loc = _local_date_stats(db_path)
    bq = _bq_date_stats(client)

    loc["d"] = pd.to_datetime(loc["d"]).dt.date
    bq["d"] = pd.to_datetime(bq["d"]).dt.date

    merged = loc.merge(bq, on="d", how="outer", suffixes=("_loc", "_bq"))
    overlap = merged[merged.cnt_loc.notna() & merged.cnt_bq.notna()].copy()
    overlap["cnt_diff"] = overlap.cnt_loc - overlap.cnt_bq
    overlap["sum_diff"] = (overlap.s_loc.fillna(0) - overlap.s_bq.fillna(0)).round(2)

    only_loc = merged[merged.cnt_bq.isna()]["d"].tolist()
    only_bq = merged[merged.cnt_loc.isna()]["d"].tolist()

    exact_overlap = overlap[(overlap.cnt_diff == 0) & (overlap.sum_diff == 0)]
    mismatch_overlap = overlap[(overlap.cnt_diff != 0) | (overlap.sum_diff != 0)]

    result = {
        "local_dates": len(loc),
        "bq_dates": len(bq),
        "local_rows": int(loc.cnt.sum()),
        "bq_rows": int(bq.cnt.sum()) if len(bq) else 0,
        "overlap_dates": len(overlap),
        "exact_overlap_dates": len(exact_overlap),
        "mismatch_overlap_dates": len(mismatch_overlap),
        "only_local_dates": len(only_loc),
        "only_bq_dates": len(only_bq),
        "local_min": str(loc.d.min()),
        "local_max": str(loc.d.max()),
        "bq_min": str(bq.d.min()) if len(bq) else None,
        "bq_max": str(bq.d.max()) if len(bq) else None,
        "mismatches": mismatch_overlap[["d", "cnt_loc", "cnt_bq", "cnt_diff", "s_loc", "s_bq", "sum_diff"]]
        .astype(str)
        .to_dict("records"),
    }
    return result


def _plan_sync(db_path: Path) -> dict:
    """
    1. MAX(Дата визита) in BQ -> upload only local dates strictly after it
    2. Delete BQ rows with dates before the 1st day of that max-date month
    3. Never backfill full local history into BQ
    """
    client = get_client()
    loc = _local_date_stats(db_path)
    bq = _bq_date_stats(client)

    loc["d"] = pd.to_datetime(loc["d"]).dt.date
    bq["d"] = pd.to_datetime(bq["d"]).dt.date

    bq_max = _bq_max_date(client)

    if bq_max is None:
        # Empty BQ: seed current month from local only (not full backfill)
        loc_max = loc.d.max()
        window_start = loc_max.replace(day=1)
        upload = sorted(loc.loc[loc.d >= window_start, "d"].tolist())
        return {
            "bq_min": None,
            "bq_max": None,
            "window_start": window_start,
            "dates_to_upload": upload,
            "dates_to_delete": [],
        }

    window_start = bq_max.replace(day=1)
    bq_min = bq.d.min() if not bq.empty else None
    delete = sorted(bq.loc[bq.d < window_start, "d"].unique().tolist())

    merged = loc.merge(bq, on="d", how="left", suffixes=("_loc", "_bq"))
    after_max = merged[merged.d > bq_max]
    upload = after_max[
        after_max.cnt_bq.isna()
        | (after_max.cnt_loc != after_max.cnt_bq)
        | (after_max.s_loc.fillna(0).round(2) != after_max.s_bq.fillna(0).round(2))
    ]["d"].tolist()
    upload = sorted(set(upload))

    return {
        "bq_min": bq_min,
        "bq_max": bq_max,
        "window_start": window_start,
        "dates_to_upload": upload,
        "dates_to_delete": delete,
    }


def _dates_to_sync(db_path: Path) -> list[date]:
    return _plan_sync(db_path)["dates_to_upload"]


def _backup_table(client: bigquery.Client) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_id = f"{TABLE_ID}_backup_{ts}"
    backup_full = f"{PROJECT_ID}.{DATASET_ID}.{backup_id}"
    q = f"CREATE TABLE `{backup_full}` AS SELECT * FROM `{FULL_TABLE}`"
    client.query(q).result()
    tbl = client.get_table(backup_full)
    print(f"Backup: {backup_full} ({tbl.num_rows:,} rows)")
    return backup_id


def _delete_dates(client: bigquery.Client, dates: list[date], *, chunk: int = 200) -> int:
    if not dates:
        return 0
    total = 0
    date_strs = [d.isoformat() for d in dates]
    for i in range(0, len(date_strs), chunk):
        batch = date_strs[i : i + chunk]
        literals = ", ".join(f"DATE '{d}'" for d in batch)
        q = f"DELETE FROM `{FULL_TABLE}` WHERE `{DATE_COL}` IN ({literals})"
        job = client.query(q)
        job.result()
        total += job.num_dml_affected_rows or 0
        print(f"  deleted batch {i // chunk + 1}: {job.num_dml_affected_rows or 0:,} rows")
    return total


def _upload_chunk(client: bigquery.Client, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    table_ref = f"{DATASET_ID}.{TABLE_ID}"
    job_config = bigquery.LoadJobConfig(
        schema=bq_table_schema(),
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
    )
    job = client.load_table_from_dataframe(df, table_ref, job_config=job_config)
    job.result()
    return len(df)


def _month_ranges(dates: list[date]) -> list[tuple[str, str]]:
    if not dates:
        return []
    s = sorted(dates)
    by_month: dict[tuple[int, int], list[date]] = {}
    for d in s:
        by_month.setdefault((d.year, d.month), []).append(d)
    ranges = []
    for (y, m), ds in sorted(by_month.items()):
        ranges.append((min(ds).isoformat(), max(ds).isoformat()))
    return ranges


def sync(
    db_path: Path = LOCAL_DB_DEFAULT,
    *,
    dry_run: bool = False,
    month_chunk: bool = True,
) -> dict | None:
    plan = _plan_sync(db_path)
    upload_dates = plan["dates_to_upload"]
    delete_dates = plan["dates_to_delete"]

    if not upload_dates and not delete_dates:
        print("Nothing to sync — BQ is up to date.")
        return None

    if plan["bq_max"]:
        print(f"BQ window: {plan['bq_min']} .. {plan['bq_max']}")
    if delete_dates:
        print(f"Dates to delete from BQ (before window): {len(delete_dates)} ({delete_dates[0]} .. {delete_dates[-1]})")
    if upload_dates:
        print(f"Dates to upload (after {plan['bq_max']}): {len(upload_dates)} ({upload_dates[0]} .. {upload_dates[-1]})")
    else:
        print("No new dates to upload.")

    ranges = _month_ranges(upload_dates) if month_chunk and upload_dates else []
    if upload_dates and not month_chunk:
        ranges = [(upload_dates[0].isoformat(), upload_dates[-1].isoformat())]

    row_estimate = 0
    with sqlite3.connect(db_path) as conn:
        for start, end in ranges:
            q = f'SELECT COUNT(*) FROM mis_data WHERE date("{DATE_COL}") BETWEEN date(?) AND date(?)'
            row_estimate += conn.execute(q, (start, end)).fetchone()[0]
    if upload_dates:
        print(f"Rows to upload (estimate): {row_estimate:,}")

    if dry_run:
        print("DRY RUN — no changes made.")
        return {
            "dates_to_upload": [d.isoformat() for d in upload_dates],
            "dates_to_delete": [d.isoformat() for d in delete_dates],
            "rows_estimate": row_estimate,
        }

    client = get_client()
    before = client.get_table(FULL_TABLE)
    rows_before = before.num_rows

    backup_id = _backup_table(client)
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = MANIFEST_DIR / f"manifest_{backup_id.replace(TABLE_ID + '_backup_', '')}.json"

    deleted = 0
    if delete_dates:
        print("Deleting stale dates from BQ ...")
        deleted += _delete_dates(client, delete_dates)

    uploaded = 0
    for start, end in ranges:
        print(f"Upload {start} .. {end} ...")
        raw = load_local_period(start, end, db_path=db_path)
        bq_df = transform_to_bq(raw)
        n = _upload_chunk(client, bq_df)
        uploaded += n
        print(f"  uploaded {n:,} rows")

    after = client.get_table(FULL_TABLE)
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "project": PROJECT_ID,
        "dataset": DATASET_ID,
        "table": TABLE_ID,
        "backup_table": backup_id,
        "backup_full": f"{PROJECT_ID}.{DATASET_ID}.{backup_id}",
        "rows_before": rows_before,
        "rows_after": after.num_rows,
        "rows_deleted": deleted,
        "rows_uploaded": uploaded,
        "bq_window": {"min": str(plan["bq_min"]), "max": str(plan["bq_max"])},
        "synced_dates": [d.isoformat() for d in upload_dates],
        "deleted_dates": [d.isoformat() for d in delete_dates],
        "date_ranges": ranges,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    latest_path = MANIFEST_DIR / "latest_manifest.json"
    latest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Sync done. Rows: {rows_before:,} -> {after.num_rows:,}")
    print(f"Manifest: {manifest_path}")
    return manifest


def rollback(manifest_path: Path | None = None) -> None:
    client = get_client()
    if manifest_path is None:
        manifest_path = MANIFEST_DIR / "latest_manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"No manifest: {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    backup_full = manifest["backup_full"]
    print(f"Rolling back {FULL_TABLE} from {backup_full} ...")

    q = f"CREATE OR REPLACE TABLE `{FULL_TABLE}` AS SELECT * FROM `{backup_full}`"
    client.query(q).result()
    tbl = client.get_table(FULL_TABLE)
    print(f"Rollback complete: {tbl.num_rows:,} rows restored.")


def _print_compare(result: dict) -> None:
    print("=== BigQuery vs local SQLite ===")
    print(f"Local: {result['local_rows']:,} rows, {result['local_dates']} dates ({result['local_min']} .. {result['local_max']})")
    print(f"BQ:    {result['bq_rows']:,} rows, {result['bq_dates']} dates ({result['bq_min']} .. {result['bq_max']})")
    print(f"Overlap: {result['overlap_dates']} dates — exact match: {result['exact_overlap_dates']}, mismatch: {result['mismatch_overlap_dates']}")
    print(f"Only local: {result['only_local_dates']} dates | Only BQ: {result['only_bq_dates']} dates")
    if result["mismatches"]:
        print("Mismatches:")
        for m in result["mismatches"][:20]:
            print(f"  {m}")
        if len(result["mismatches"]) > 20:
            print(f"  ... and {len(result['mismatches']) - 20} more")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync mis_data to BigQuery combined_report_temp")
    parser.add_argument("--db", type=Path, default=LOCAL_DB_DEFAULT, help="SQLite path")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("compare", help="Compare local vs BQ by date")
    p_sync = sub.add_parser("sync", help="Backup, delete old dates, upload missing")
    p_sync.add_argument("--dry-run", action="store_true", help="Show plan only")
    p_rollback = sub.add_parser("rollback", help="Restore table from last backup")
    p_rollback.add_argument("--manifest", type=Path, default=None, help="Manifest JSON path")

    args = parser.parse_args(argv)

    if args.cmd == "compare":
        _print_compare(compare(args.db))
        return 0
    if args.cmd == "sync":
        sync(args.db, dry_run=args.dry_run)
        return 0
    if args.cmd == "rollback":
        rollback(args.manifest)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
