#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Sync mis_data from remote MariaDB into local SQLite (29 columns, CSV names).

Initial load (month by month until BACKFILL_UNTIL):
    python sync_mis_data.py backfill

Daily job:
    python sync_mis_data.py run

Logic on each `run`:
    1. Read MAX(Дата визита) in local DB
    2. Catch up missing days through yesterday (inclusive)
    3. If Monday:
       - reload and replace entire current month
       - if day 1..15: also reload and replace previous month

Environment (.env):
    KRAVIRA_DB_PASSWORD=...
    MIS_LOCAL_DB=data/mis_local.sqlite
    MIS_BACKFILL_FROM=              # empty = MIN(vdate) from server
    MIS_BACKFILL_UNTIL=2026-05-31
    MIS_INCREMENTAL_FROM=2026-06-01
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

from load_mis_data import load_mis_data_renamed
from mis_column_mapping import CSV_COLUMNS
from sql_csv_compare import (
    _load_dotenv_if_present,
    create_db_engine,
    database,
    get_db_password,
    host,
    port,
    table_name,
    user,
    with_db_retry,
)

_load_dotenv_if_present()

DATE_COL = "\u0414\u0430\u0442\u0430 \u0432\u0438\u0437\u0438\u0442\u0430"
LOCAL_TABLE = "mis_data"
LOG_TABLE = "sync_log"

DEFAULT_LOCAL_DB = os.environ.get("MIS_LOCAL_DB", "data/mis_local.sqlite")
DEFAULT_BACKFILL_UNTIL = os.environ.get("MIS_BACKFILL_UNTIL", "2026-05-31")
DEFAULT_INCREMENTAL_FROM = os.environ.get("MIS_INCREMENTAL_FROM", "2026-06-01")
DEFAULT_BACKFILL_FROM = os.environ.get("MIS_BACKFILL_FROM", "").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


def _ts(value: str | date | datetime | pd.Timestamp) -> pd.Timestamp:
    return pd.Timestamp(value).normalize()


def _format_time_value(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if isinstance(value, pd.Timedelta):
        total = int(round(value.total_seconds()))
        h, m = divmod(total // 60, 60)
        return f"{h:02d}:{m:02d}"
    if isinstance(value, (datetime, pd.Timestamp)):
        return pd.Timestamp(value).strftime("%H:%M")
    text = str(value).strip()
    if "days" in text:
        try:
            td = pd.to_timedelta(text)
            return _format_time_value(td)
        except (ValueError, TypeError):
            pass
    return text


def month_ranges(
    start_inclusive: pd.Timestamp,
    end_inclusive: pd.Timestamp,
) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """[(month_start, month_end_exclusive), ...]"""
    ranges: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    cursor = start_inclusive.replace(day=1)
    last = end_inclusive.replace(day=1)
    while cursor <= last:
        nxt = cursor + pd.offsets.MonthBegin(1)
        ranges.append((cursor, nxt))
        cursor = nxt
    return ranges


def get_remote_min_vdate(engine: Engine) -> pd.Timestamp:
    def _fetch() -> pd.Timestamp:
        with engine.connect() as conn:
            val = conn.execute(text(f"SELECT MIN(`vdate`) FROM `{table_name}`")).scalar()
        if val is None:
            raise RuntimeError(f"No rows in remote `{table_name}`")
        return _ts(val)

    return with_db_retry(_fetch, label="get_remote_min_vdate")


@dataclass
class SyncAction:
    name: str
    start: pd.Timestamp
    end_excl: pd.Timestamp


class LocalMisStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_schema(self) -> None:
        col_defs = ", ".join(f'"{c}" TEXT' for c in CSV_COLUMNS)
        with self._connect() as conn:
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {LOCAL_TABLE} (
                    {col_defs}
                )
                """
            )
            conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {LOG_TABLE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_at TEXT NOT NULL,
                    as_of_date TEXT NOT NULL,
                    action TEXT NOT NULL,
                    period_start TEXT NOT NULL,
                    period_end_excl TEXT NOT NULL,
                    rows_loaded INTEGER NOT NULL,
                    rows_deleted INTEGER NOT NULL,
                    total_rows INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                f'CREATE INDEX IF NOT EXISTS idx_mis_vdate ON {LOCAL_TABLE} ("{DATE_COL}")'
            )

    def row_count(self) -> int:
        with self._connect() as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {LOCAL_TABLE}").fetchone()[0]

    def date_range(self) -> tuple[Optional[pd.Timestamp], Optional[pd.Timestamp]]:
        with self._connect() as conn:
            row = conn.execute(
                f'SELECT MIN("{DATE_COL}"), MAX("{DATE_COL}") FROM {LOCAL_TABLE}'
            ).fetchone()
        if row[0] is None:
            return None, None
        return _ts(row[0]), _ts(row[1])

    def delete_period(self, start: pd.Timestamp, end_excl: pd.Timestamp) -> int:
        start_s = start.strftime("%Y-%m-%d")
        end_s = end_excl.strftime("%Y-%m-%d")
        with self._connect() as conn:
            cur = conn.execute(
                f"""
                DELETE FROM {LOCAL_TABLE}
                WHERE date("{DATE_COL}") >= date(?)
                  AND date("{DATE_COL}") < date(?)
                """,
                (start_s, end_s),
            )
            return cur.rowcount

    def insert_df(self, df: pd.DataFrame) -> int:
        if df.empty:
            return 0
        out = df[CSV_COLUMNS].copy()
        out[DATE_COL] = pd.to_datetime(out[DATE_COL]).dt.strftime("%Y-%m-%d")
        time_col = "\u0412\u0440\u0435\u043c\u044f \u0432\u0438\u0437\u0438\u0442\u0430"
        if time_col in out.columns:
            out[time_col] = out[time_col].apply(_format_time_value)
        with self._connect() as conn:
            out.to_sql(LOCAL_TABLE, conn, if_exists="append", index=False)
        return len(out)

    def replace_period(
        self,
        df: pd.DataFrame,
        start: pd.Timestamp,
        end_excl: pd.Timestamp,
    ) -> tuple[int, int]:
        deleted = self.delete_period(start, end_excl)
        inserted = self.insert_df(df)
        return deleted, inserted

    def log_run(
        self,
        as_of: pd.Timestamp,
        action: SyncAction,
        rows_loaded: int,
        rows_deleted: int,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                f"""
                INSERT INTO {LOG_TABLE}
                (run_at, as_of_date, action, period_start, period_end_excl,
                 rows_loaded, rows_deleted, total_rows)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    datetime.now().isoformat(timespec="seconds"),
                    as_of.strftime("%Y-%m-%d"),
                    action.name,
                    action.start.strftime("%Y-%m-%d"),
                    action.end_excl.strftime("%Y-%m-%d"),
                    rows_loaded,
                    rows_deleted,
                    self.row_count(),
                ),
            )


class MisDataSync:
    def __init__(
        self,
        store: LocalMisStore,
        engine: Engine,
        *,
        incremental_from: pd.Timestamp,
    ) -> None:
        self.store = store
        self.engine = engine
        self.incremental_from = incremental_from

    def load_remote(self, start: pd.Timestamp, end_excl: pd.Timestamp) -> pd.DataFrame:
        log.info(
            "Remote load [%s, %s)...",
            start.date(),
            end_excl.date(),
        )
        df = load_mis_data_renamed(start, end_excl, engine=self.engine)
        log.info("  fetched %s rows", f"{len(df):,}")
        return df

    def apply_action(self, action: SyncAction, as_of: pd.Timestamp) -> None:
        df = self.load_remote(action.start, action.end_excl)
        deleted, inserted = self.store.replace_period(df, action.start, action.end_excl)
        self.store.log_run(as_of, action, inserted, deleted)
        log.info(
            "  %s: deleted %s, inserted %s, total %s",
            action.name,
            f"{deleted:,}",
            f"{inserted:,}",
            f"{self.store.row_count():,}",
        )

    def backfill(
        self,
        until_inclusive: pd.Timestamp,
        from_inclusive: Optional[pd.Timestamp] = None,
    ) -> None:
        if from_inclusive is None:
            from_inclusive = get_remote_min_vdate(self.engine)
        from_inclusive = from_inclusive.replace(day=1)
        until_inclusive = _ts(until_inclusive)

        log.info(
            "Backfill months: %s .. %s",
            from_inclusive.date(),
            until_inclusive.date(),
        )
        for start, end_excl in month_ranges(from_inclusive, until_inclusive):
            action = SyncAction("backfill_month", start, end_excl)
            self.apply_action(action, as_of=start)

    def plan_actions(self, as_of: pd.Timestamp) -> list[SyncAction]:
        as_of = _ts(as_of)
        yesterday = as_of - pd.Timedelta(days=1)
        actions: list[SyncAction] = []

        if yesterday < self.incremental_from:
            log.info(
                "Yesterday %s is before incremental_from=%s — nothing to sync",
                yesterday.date(),
                self.incremental_from.date(),
            )
            return actions

        _, last_date = self.store.date_range()
        if last_date is not None:
            log.info("Last date in local DB: %s", last_date.date())
        else:
            log.warning("Local DB is empty — run backfill first")

        if as_of.weekday() == 0:
            cur_start = as_of.replace(day=1)
            if as_of.day <= 15:
                prev_start = cur_start - pd.DateOffset(months=1)
                actions.append(
                    SyncAction("monday_prev_month", prev_start, cur_start)
                )
                log.info(
                    "Monday + 1st half of month: refresh previous month %s",
                    prev_start.strftime("%Y-%m"),
                )
            actions.append(
                SyncAction("monday_current_month", cur_start, as_of)
            )
            log.info(
                "Monday: refresh current month from %s through %s",
                cur_start.date(),
                yesterday.date(),
            )

        if last_date is None:
            return actions

        gap_start = max(last_date + pd.Timedelta(days=1), self.incremental_from)
        monday_covers_gap = (
            as_of.weekday() == 0 and gap_start >= as_of.replace(day=1)
        )
        if gap_start <= yesterday and not monday_covers_gap:
            actions.append(
                SyncAction("catchup_to_yesterday", gap_start, as_of)
            )
            log.info(
                "Catch up gap: %s .. %s (through yesterday)",
                gap_start.date(),
                yesterday.date(),
            )
        elif gap_start > yesterday:
            log.info("Already up to date (last=%s, yesterday=%s)", last_date.date(), yesterday.date())

        return actions

    def run_daily(self, as_of: Optional[pd.Timestamp] = None) -> None:
        as_of = _ts(as_of or pd.Timestamp.today())
        log.info("Daily sync as_of=%s", as_of.date())

        actions = self.plan_actions(as_of)
        if not actions:
            log.info("Nothing to sync.")
            return

        log.info("Planned %s action(s)", len(actions))
        for action in actions:
            self.apply_action(action, as_of)


def cmd_backfill(args: argparse.Namespace) -> None:
    engine = create_db_engine(host, port, database, user, get_db_password())
    try:
        store = LocalMisStore(args.local_db)
        sync = MisDataSync(
            store,
            engine,
            incremental_from=_ts(args.incremental_from),
        )
        from_date = _ts(args.from_date) if args.from_date else None
        sync.backfill(_ts(args.until), from_date)
        lo, hi = store.date_range()
        log.info("Backfill done. Rows: %s, dates: %s .. %s", f"{store.row_count():,}", lo, hi)
    finally:
        engine.dispose()


def cmd_run(args: argparse.Namespace) -> None:
    engine = create_db_engine(host, port, database, user, get_db_password())
    try:
        store = LocalMisStore(args.local_db)
        sync = MisDataSync(
            store,
            engine,
            incremental_from=_ts(args.incremental_from),
        )
        as_of = _ts(args.date) if args.date else _ts(pd.Timestamp.today())
        sync.run_daily(as_of)
        lo, hi = store.date_range()
        log.info("Run done. Rows: %s, dates: %s .. %s", f"{store.row_count():,}", lo, hi)
    finally:
        engine.dispose()


def cmd_status(args: argparse.Namespace) -> None:
    store = LocalMisStore(args.local_db)
    lo, hi = store.date_range()
    print(f"DB: {store.db_path}")
    print(f"Rows: {store.row_count():,}")
    print(f"Date range: {lo} .. {hi}")
    with store._connect() as conn:
        rows = conn.execute(
            f"SELECT run_at, as_of_date, action, period_start, period_end_excl, "
            f"rows_loaded, rows_deleted, total_rows "
            f"FROM {LOG_TABLE} ORDER BY id DESC LIMIT {args.last}"
        ).fetchall()
    if rows:
        print("\nLast sync runs:")
        for r in rows:
            print("  ", r)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Sync mis_data to local SQLite")
    p.add_argument("--local-db", default=DEFAULT_LOCAL_DB)
    p.add_argument("--incremental-from", default=DEFAULT_INCREMENTAL_FROM)
    sub = p.add_subparsers(dest="command", required=True)

    bf = sub.add_parser("backfill", help="Load history month-by-month until --until")
    bf.add_argument("--until", default=DEFAULT_BACKFILL_UNTIL, help="Last date inclusive")
    bf.add_argument("--from-date", default=DEFAULT_BACKFILL_FROM or None, help="First month start")
    bf.set_defaults(func=cmd_backfill)

    run = sub.add_parser("run", help="Daily/weekly/monthly incremental sync")
    run.add_argument("--date", default=None, help="As-of date YYYY-MM-DD (default: today)")
    run.set_defaults(func=cmd_run)

    st = sub.add_parser("status", help="Show local DB stats and recent log")
    st.add_argument("--last", type=int, default=10)
    st.set_defaults(func=cmd_status)

    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
