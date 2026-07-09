#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Load mis_data from MariaDB/MySQL: only mapped columns, renamed to CSV names.

Usage as module:
    from load_mis_data import load_mis_data_renamed
    df = load_mis_data_renamed("2026-05-01", "2026-06-01")

CLI:
    python load_mis_data.py --from-csv
    python load_mis_data.py --start 2026-05-01 --end 2026-05-02
    python load_mis_data.py --from-csv --output mis_may.parquet
"""

from __future__ import annotations

import argparse
from datetime import datetime
from typing import Optional, Union

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from mis_column_mapping import CSV_COLUMNS, DATE_COL_SQL, SQL_COLUMNS, SQL_TO_CSV
from sql_csv_compare import (
    create_db_engine,
    csv_path,
    database,
    get_date_period_from_csv,
    get_db_password,
    host,
    port,
    read_csv_report,
    table_name,
    user,
    with_db_retry,
)

DateLike = Union[str, datetime, pd.Timestamp]


def _to_timestamp(value: DateLike) -> pd.Timestamp:
    return pd.Timestamp(value).normalize()


def load_mis_data_renamed(
    start_date: DateLike,
    end_date_plus_1: DateLike,
    *,
    engine: Optional[Engine] = None,
    table: str = table_name,
    sql_date_col: str = DATE_COL_SQL,
) -> pd.DataFrame:
    """
    Load rows for [start_date, end_date_plus_1) with only mapped SQL columns,
    renamed to CSV column names.

    end_date_plus_1 is exclusive (same as vdate < end_date_plus_1).
    """
    start = _to_timestamp(start_date)
    end_excl = _to_timestamp(end_date_plus_1)

    cols_sql = ", ".join(f"`{c}`" for c in SQL_COLUMNS)
    query = text(
        f"""
        SELECT {cols_sql}
        FROM `{table}`
        WHERE `{sql_date_col}` >= :start_date
          AND `{sql_date_col}` < :end_date_plus_1
        """
    )
    params = {
        "start_date": start.to_pydatetime(),
        "end_date_plus_1": end_excl.to_pydatetime(),
    }

    own_engine = engine is None
    if own_engine:
        engine = create_db_engine(host, port, database, user, get_db_password())

    def _fetch() -> pd.DataFrame:
        with engine.connect() as conn:
            return pd.read_sql(query, conn, params=params)

    try:
        df = with_db_retry(
            _fetch,
            label=f"load_mis_data [{start.date()}, {end_excl.date()})",
        )
    finally:
        if own_engine:
            engine.dispose()

    return df.rename(columns=SQL_TO_CSV)[CSV_COLUMNS]


def period_from_csv(csv_file: str = csv_path) -> tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    """(start, end_inclusive, end_plus_1) from local CSV report."""
    df_csv = read_csv_report(csv_file)
    return get_date_period_from_csv(df_csv)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Load mis_data with CSV column names for a date range."
    )
    period = parser.add_mutually_exclusive_group(required=True)
    period.add_argument(
        "--from-csv",
        action="store_true",
        help=f"Date range from {csv_path} (min..max date in file)",
    )
    period.add_argument("--start", type=str, help="Start date YYYY-MM-DD (inclusive)")
    parser.add_argument(
        "--end",
        type=str,
        help="End date YYYY-MM-DD inclusive; for --start only",
    )
    parser.add_argument(
        "--end-plus-1",
        type=str,
        help="Exclusive upper bound YYYY-MM-DD (overrides --end)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="",
        help="Save result: .parquet, .csv, .xlsx (by extension)",
    )
    parser.add_argument(
        "--csv-path",
        type=str,
        default=csv_path,
        help="CSV file for --from-csv",
    )
    return parser.parse_args()


def _save_df(df: pd.DataFrame, path: str) -> None:
    ext = path.rsplit(".", 1)[-1].lower()
    if ext == "parquet":
        df.to_parquet(path, index=False)
    elif ext in ("csv", "txt"):
        df.to_csv(path, index=False, encoding="utf-8-sig")
    elif ext in ("xlsx", "xls"):
        df.to_excel(path, index=False)
    else:
        raise ValueError(f"Unsupported output format: {path}")


def main() -> None:
    args = _parse_args()

    if args.from_csv:
        start, end_incl, end_plus_1 = period_from_csv(args.csv_path)
        print(f"Period from CSV: {start.date()} .. {end_incl.date()}")
    else:
        if not args.start:
            raise SystemExit("--start is required without --from-csv")
        start = _to_timestamp(args.start)
        if args.end_plus_1:
            end_plus_1 = _to_timestamp(args.end_plus_1)
            end_incl = end_plus_1 - pd.Timedelta(days=1)
        elif args.end:
            end_incl = _to_timestamp(args.end)
            end_plus_1 = end_incl + pd.Timedelta(days=1)
        else:
            raise SystemExit("Use --end or --end-plus-1 with --start")

    print(
        f"Loading {table_name}: {len(SQL_COLUMNS)} columns, "
        f"[{start.date()}, {end_plus_1.date()})..."
    )
    df = load_mis_data_renamed(start, end_plus_1)
    print(f"Loaded {len(df):,} rows x {len(df.columns)} columns")

    if args.output:
        _save_df(df, args.output)
        print(f"Saved: {args.output}")
    else:
        with pd.option_context("display.max_columns", 8, "display.width", 200):
            print(df.head())


if __name__ == "__main__":
    main()
