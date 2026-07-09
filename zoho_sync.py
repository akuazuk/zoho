# -*- coding: utf-8 -*-
"""
Sync local SQLite -> Zoho Analytics.

Daily `run`:
  1. Read MAX(Дата визита) in Zoho
  2. If it is before yesterday -> delete & reload from that date through yesterday
  3. On Monday: additionally refresh previous calendar week

Commands:
  python zoho_sync.py status
  python zoho_sync.py delete-range --start 2026-06-01 --end 2026-06-30
  python zoho_sync.py import-range --start 2026-06-01 --end 2026-06-08
  python zoho_sync.py reload-june-2026
  python zoho_sync.py run [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from bq_schema import DATE_COL
from bq_transform import LOCAL_DB_DEFAULT
from zoho_analytics import date_range_criteria, delete_rows, get_max_visit_date, import_csv, zoho_config
from zoho_transform import load_zoho_period

MANIFEST_DIR = Path(__file__).resolve().parent / "data" / "zoho_sync"
MANIFEST_PATH = MANIFEST_DIR / "latest_manifest.json"
SUM_COL = "Стоимость услуги со скидкой"


def _yesterday() -> date:
    return (datetime.now().date() - timedelta(days=1))


def _local_max_date(db_path: Path) -> date | None:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(f'SELECT MAX(date("{DATE_COL}")) FROM mis_data').fetchone()
    if not row or row[0] is None:
        return None
    return date.fromisoformat(row[0])


def _save_manifest(data: dict) -> None:
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def delete_date_range(start: str, end: str, *, dry_run: bool = False) -> dict | None:
    criteria = date_range_criteria(DATE_COL, start, end)
    print(f"Delete Zoho rows: {start} .. {end}")
    print(f"  criteria: {criteria}")
    if dry_run:
        return None
    result = delete_rows(criteria=criteria)
    deleted = result.get("data", {}).get("deletedRows", 0)
    print(f"  deleted: {deleted:,} rows")
    return result


def import_date_range(
    start: str,
    end: str,
    *,
    db_path: Path = LOCAL_DB_DEFAULT,
    dry_run: bool = False,
) -> dict | None:
    df = load_zoho_period(start, end, db_path=db_path)
    if df.empty:
        print(f"No local data for {start} .. {end}")
        return None
    print(f"Import {start} .. {end}: {len(df):,} rows, sum={df[SUM_COL].sum():.2f}")
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    csv_path = MANIFEST_DIR / f"import_{start}_{end}.csv"
    df.to_csv(csv_path, index=False, encoding="utf-8")
    print(f"  CSV: {csv_path} ({csv_path.stat().st_size / 1024 / 1024:.1f} MB)")
    if dry_run:
        return {"rows": len(df), "csv": str(csv_path)}
    result = import_csv(csv_path)
    summary = result.get("data", {}).get("importSummary", {})
    print(
        f"  imported: {summary.get('successRowCount', '?')}/{summary.get('totalRowCount', '?')} rows"
    )
    if result.get("data", {}).get("importErrors"):
        print(f"  errors: {result['data']['importErrors']}")
    return result


def reload_june_2026(*, db_path: Path = LOCAL_DB_DEFAULT, dry_run: bool = False) -> None:
    delete_date_range("2026-06-01", "2026-06-30", dry_run=dry_run)
    local_max = _local_max_date(db_path) or date(2026, 6, 8)
    end = min(local_max, date(2026, 6, 30)).isoformat()
    import_date_range("2026-06-01", end, db_path=db_path, dry_run=dry_run)
    if not dry_run:
        _save_manifest(
            {
                "last_sync_at": datetime.now(timezone.utc).isoformat(),
                "zoho_max_date": end,
                "action": "reload_june_2026",
            }
        )


def _week_bounds(d: date) -> tuple[date, date]:
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def cmd_status(*, db_path: Path = LOCAL_DB_DEFAULT) -> None:
    yesterday = _yesterday()
    local_max = _local_max_date(db_path)
    zoho_max = get_max_visit_date(DATE_COL)
    print(f"Yesterday:   {yesterday}")
    print(f"Local max:   {local_max}")
    print(f"Zoho max:    {zoho_max}")
    if zoho_max and zoho_max >= yesterday:
        print("Status:      up to date")
    elif zoho_max:
        print(f"Status:      need sync {zoho_max} .. {yesterday}")
    else:
        print("Status:      Zoho empty or no dates in window")


def run_daily(*, db_path: Path = LOCAL_DB_DEFAULT, dry_run: bool = False) -> None:
    today = datetime.now().date()
    yesterday = _yesterday()
    local_max = _local_max_date(db_path)

    if local_max is None:
        print("Local DB is empty.")
        return

    target_end = min(yesterday, local_max)

    # Monday: refresh previous calendar week (updated corrections)
    if today.weekday() == 0:
        prev_mon, prev_sun = _week_bounds(today - timedelta(days=7))
        prev_sun = min(prev_sun, target_end)
        if prev_mon <= prev_sun:
            print(f"Monday refresh: reload week {prev_mon} .. {prev_sun}")
            delete_date_range(prev_mon.isoformat(), prev_sun.isoformat(), dry_run=dry_run)
            import_date_range(prev_mon.isoformat(), prev_sun.isoformat(), db_path=db_path, dry_run=dry_run)

    print("Checking last date in Zoho ...")
    zoho_max = get_max_visit_date(DATE_COL)
    print(f"Zoho max date: {zoho_max}, yesterday: {yesterday}, local max: {local_max}")

    if zoho_max is None:
        # Empty Zoho: load current month from local
        upload_start = target_end.replace(day=1)
        print(f"Zoho has no recent data — load {upload_start} .. {target_end}")
        delete_date_range(upload_start.isoformat(), target_end.isoformat(), dry_run=dry_run)
        import_date_range(upload_start.isoformat(), target_end.isoformat(), db_path=db_path, dry_run=dry_run)
    elif zoho_max >= target_end:
        print("Zoho is up to date.")
        if not dry_run:
            _save_manifest(
                {
                    "last_sync_at": datetime.now(timezone.utc).isoformat(),
                    "zoho_max_date": zoho_max.isoformat(),
                    "action": "run",
                    "status": "up_to_date",
                }
            )
        return
    else:
        # From last Zoho date through yesterday (inclusive), replace to avoid duplicates
        upload_start = zoho_max
        print(f"Catch-up: reload {upload_start} .. {target_end}")
        delete_date_range(upload_start.isoformat(), target_end.isoformat(), dry_run=dry_run)
        import_date_range(upload_start.isoformat(), target_end.isoformat(), db_path=db_path, dry_run=dry_run)

    if not dry_run:
        new_max = get_max_visit_date(DATE_COL)
        _save_manifest(
            {
                "last_sync_at": datetime.now(timezone.utc).isoformat(),
                "zoho_max_date": (new_max or target_end).isoformat(),
                "action": "run",
                "synced_range": [upload_start.isoformat(), target_end.isoformat()],
            }
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync mis_data to Zoho Analytics")
    parser.add_argument("--db", type=Path, default=LOCAL_DB_DEFAULT)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="Show Zoho vs local dates")
    p_del = sub.add_parser("delete-range", help="Delete date range in Zoho")
    p_del.add_argument("--start", required=True)
    p_del.add_argument("--end", required=True)
    p_del.add_argument("--dry-run", action="store_true")

    p_imp = sub.add_parser("import-range", help="Import date range from local SQLite")
    p_imp.add_argument("--start", required=True)
    p_imp.add_argument("--end", required=True)
    p_imp.add_argument("--dry-run", action="store_true")

    p_june = sub.add_parser("reload-june-2026", help="Delete June 2026 in Zoho and reload from local")
    p_june.add_argument("--dry-run", action="store_true")

    p_run = sub.add_parser("run", help="Daily sync job")
    p_run.add_argument("--dry-run", action="store_true")

    args = parser.parse_args(argv)
    if not zoho_config().get("refresh_token"):
        print("ZOHO_REFRESH_TOKEN missing in .env", file=sys.stderr)
        return 1

    try:
        if args.cmd == "status":
            cmd_status(db_path=args.db)
            return 0
        if args.cmd == "delete-range":
            delete_date_range(args.start, args.end, dry_run=getattr(args, "dry_run", False))
            return 0
        if args.cmd == "import-range":
            import_date_range(
                args.start, args.end, db_path=args.db, dry_run=getattr(args, "dry_run", False)
            )
            return 0
        if args.cmd == "reload-june-2026":
            reload_june_2026(db_path=args.db, dry_run=getattr(args, "dry_run", False))
            return 0
        if args.cmd == "run":
            run_daily(db_path=args.db, dry_run=getattr(args, "dry_run", False))
            return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
