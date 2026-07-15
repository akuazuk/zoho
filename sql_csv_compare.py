#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Compare CSV report with mis_data for the date period found in CSV."""

from __future__ import annotations

import getpass
import logging
import os
import re
import time
from collections import Counter
from pathlib import Path
from datetime import datetime, timedelta
from typing import Any, Callable, Optional, Tuple, TypeVar

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

log = logging.getLogger(__name__)

T = TypeVar("T")

# Morning clinic systems often exhaust MariaDB max_connections (~06:30).
# Longer backoff gives the server time to free slots ("Too many connections").
DEFAULT_DB_RETRIES = int(os.environ.get("MIS_DB_RETRIES", "6"))
DEFAULT_DB_RETRY_DELAY_SEC = float(os.environ.get("MIS_DB_RETRY_DELAY_SEC", "20"))

host = "178.163.240.131"
port = 6330
database = "kravira_mc"
user = "kravira_mc_user"
table_name = "mis_data"
csv_path = "report_last.csv"
DATE_COL_CSV = "\u0414\u0430\u0442\u0430 \u0432\u0438\u0437\u0438\u0442\u0430"
DATE_COL_SQL = "vdate"
KEY_COL = "ID \u0432\u0438\u0437\u0438\u0442\u0430"

DATE_FMT_CSV = "%d.%m.%Y"
DATE_FMT_NORMALIZED = "%Y-%m-%d"
NUMERIC_DECIMALS = 2
ENV_PASSWORD_KEY = "KRAVIRA_DB_PASSWORD"
TIME_COL_CSV = "\u0412\u0440\u0435\u043c\u044f \u0432\u0438\u0437\u0438\u0442\u0430"
TIME_COL_SQL = "vtime"
CODE_COL_CSV = "\u041a\u043e\u0434 \u0443\u0441\u043b\u0443\u0433\u0438"
NAME_COL_CSV = "\u041d\u0430\u0438\u043c\u0435\u043d\u043e\u0432\u0430\u043d\u0438\u0435 \u0443\u0441\u043b\u0443\u0433\u0438"
PREFERRED_SQL_MAP = {
    "\u0422\u0438\u043f \u043e\u043f\u043b\u0430\u0442\u044b": "pay_type",
}
COMPARE_FIRST_DAY_ONLY = True
OUTPUT_XLSX_FULL = "sql_csv_columns_compare.xlsx"
OUTPUT_XLSX_DAY1 = "sql_csv_columns_compare_day1.xlsx"


def _load_dotenv_if_present() -> None:
    """Loads .env from the script directory when python-dotenv is installed."""
    env_file = Path(__file__).resolve().parent / ".env"
    if not env_file.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_file)
    except ImportError:
        pass


def get_db_password() -> str:
    """KRAVIRA_DB_PASSWORD from env/.env, else getpass prompt."""
    _load_dotenv_if_present()
    password = os.environ.get(ENV_PASSWORD_KEY, "").strip()
    if password:
        return password
    return getpass.getpass(f"DB password (or set {ENV_PASSWORD_KEY}): ")

def read_csv_report(csv_path: str) -> pd.DataFrame:
    return pd.read_csv(csv_path, encoding="cp1251", low_memory=False)


def _parse_csv_dates(series: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(series, format=DATE_FMT_CSV, errors="coerce")
    if parsed.isna().any():
        mask = parsed.isna()
        parsed.loc[mask] = pd.to_datetime(series.loc[mask], errors="coerce", dayfirst=True)
    return parsed


def get_date_period_from_csv(
    df_csv: pd.DataFrame,
    date_col: str = DATE_COL_CSV,
) -> Tuple[pd.Timestamp, pd.Timestamp, pd.Timestamp]:
    dates = _parse_csv_dates(df_csv[date_col])
    valid = dates.dropna()
    if valid.empty:
        raise ValueError(f"Could not parse dates in column {date_col!r}")

    start_date = valid.min().normalize()
    end_inclusive = valid.max().normalize()
    end_plus_1 = end_inclusive + pd.Timedelta(days=1)
    return start_date, end_inclusive, end_plus_1


def filter_csv_by_period(
    df_csv: pd.DataFrame,
    start_date: pd.Timestamp,
    end_plus_1: pd.Timestamp,
    date_col: str = DATE_COL_CSV,
) -> pd.DataFrame:
    dates = _parse_csv_dates(df_csv[date_col])
    mask = (dates >= start_date) & (dates < end_plus_1)
    return df_csv.loc[mask].copy()


def filter_first_day(
    df: pd.DataFrame,
    period_start: pd.Timestamp,
    end_plus_1: pd.Timestamp,
    date_col: str = DATE_COL_CSV,
) -> pd.DataFrame:
    """Only the first calendar day of the CSV period (period_start)."""
    day_end = period_start + pd.Timedelta(days=1)
    return filter_csv_by_period(df, period_start, min(day_end, end_plus_1), date_col)


def with_db_retry(
    operation: Callable[[], T],
    *,
    retries: int = DEFAULT_DB_RETRIES,
    base_delay_sec: float = DEFAULT_DB_RETRY_DELAY_SEC,
    label: str = "MariaDB operation",
) -> T:
    """Retry transient MariaDB connection/query failures."""
    last_exc: OperationalError | None = None
    for attempt in range(1, retries + 1):
        try:
            return operation()
        except OperationalError as exc:
            last_exc = exc
            if attempt >= retries:
                break
            delay = base_delay_sec * attempt
            log.warning(
                "%s failed (attempt %s/%s): %s; retry in %.0fs",
                label,
                attempt,
                retries,
                exc.orig if getattr(exc, "orig", None) else exc,
                delay,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


def create_db_engine(
    host: str,
    port: int,
    database: str,
    user: str,
    password: str,
) -> Engine:
    url = (
        f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}"
        "?charset=utf8mb4"
    )
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_recycle=3600,
        connect_args={
            "connect_timeout": int(os.environ.get("MIS_DB_CONNECT_TIMEOUT", "30")),
            "read_timeout": int(os.environ.get("MIS_DB_READ_TIMEOUT", "300")),
            "write_timeout": int(os.environ.get("MIS_DB_WRITE_TIMEOUT", "300")),
        },
    )


def load_sql_period(
    engine: Engine,
    table_name: str,
    start_date: pd.Timestamp,
    end_date_plus_1: pd.Timestamp,
    sql_date_col: str = DATE_COL_SQL,
) -> pd.DataFrame:
    query = text(
        f"""
        SELECT *
        FROM `{table_name}`
        WHERE `{sql_date_col}` >= :start_date
          AND `{sql_date_col}` < :end_date_plus_1
        """
    )
    params = {
        "start_date": start_date.to_pydatetime(),
        "end_date_plus_1": end_date_plus_1.to_pydatetime(),
    }

    def _fetch() -> pd.DataFrame:
        with engine.connect() as conn:
            return pd.read_sql(query, conn, params=params)

    return with_db_retry(
        _fetch,
        label=f"load_sql_period [{start_date.date()}, {end_date_plus_1.date()})",
    )


def _try_parse_date_string(value: str) -> Optional[str]:
    if not value:
        return ""
    for fmt in (
        DATE_FMT_CSV,
        DATE_FMT_NORMALIZED,
        "%Y-%m-%d %H:%M:%S",
        "%d/%m/%Y",
        "%Y.%m.%d.",
        "%Y.%m.%d.%H.",
        "%Y.%m.%d",
    ):
        try:
            dt = datetime.strptime(value, fmt)
            return dt.strftime(DATE_FMT_NORMALIZED)
        except ValueError:
            continue
    ts = pd.to_datetime(value, errors="coerce", dayfirst=True)
    if pd.notna(ts):
        return pd.Timestamp(ts).strftime(DATE_FMT_NORMALIZED)
    return None


def _try_parse_number(value: str) -> Optional[float]:
    cleaned = value.replace(" ", "").replace(",", ".")
    if cleaned in ("", "-", "."):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _format_number(num: float) -> str:
    rounded = round(num, NUMERIC_DECIMALS)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.{NUMERIC_DECIMALS}f}".rstrip("0").rstrip(".")


def _collapse_text(text_val: str) -> str:
    text_val = text_val.replace(",", " ")
    return re.sub(r"\s+", " ", text_val).strip()


def _format_time_from_day_fraction(value: float) -> str:
    total_minutes = int(round(value * 24 * 60))
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours:02d}:{minutes:02d}"


def _try_parse_time_string(value: str) -> Optional[str]:
    value = _collapse_text(value)
    if not value:
        return ""
    match = re.match(r"^(\d{1,2}):(\d{2})(?::\d{2})?$", value)
    if match:
        return f"{int(match.group(1)):02d}:{match.group(2)}"
    if "days" in value:
        try:
            td = pd.to_timedelta(value)
            total_minutes = int(round(td.total_seconds() / 60))
            hours, minutes = divmod(total_minutes, 60)
            return f"{hours:02d}:{minutes:02d}"
        except (ValueError, TypeError):
            pass
    return None


def _is_time_column(col_name: str) -> bool:
    base = col_name.removesuffix("_csv_dup").removesuffix("_sql_dup")
    return base in {TIME_COL_CSV, TIME_COL_SQL}


def _normalize_scalar(value: Any, *, for_key: bool = False, as_time: bool = False) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if pd.isna(value):
        return ""

    if isinstance(value, pd.Timestamp):
        return value.strftime("%H:%M") if as_time else value.strftime(DATE_FMT_NORMALIZED)
    if isinstance(value, datetime):
        return value.strftime("%H:%M") if as_time else value.strftime(DATE_FMT_NORMALIZED)
    if isinstance(value, timedelta):
        total_minutes = int(round(value.total_seconds() / 60))
        hours, minutes = divmod(total_minutes, 60)
        return f"{hours:02d}:{minutes:02d}"

    if isinstance(value, bool):
        return str(value).lower()

    if isinstance(value, (int,)) and not isinstance(value, bool):
        return str(value) if for_key else _format_number(float(value))

    if isinstance(value, float):
        if as_time and 0 <= value < 1:
            return _format_time_from_day_fraction(value)
        if for_key and value == int(value):
            return str(int(value))
        return _format_number(value)

    text_val = _collapse_text(str(value))
    if text_val.lower() in ("nan", "none", "<na>"):
        return ""

    if as_time:
        time_norm = _try_parse_time_string(text_val)
        if time_norm is not None:
            return time_norm

    if for_key:
        num = _try_parse_number(text_val)
        if num is not None and num == int(num):
            return str(int(num))
        return text_val

    if not as_time:
        date_norm = _try_parse_date_string(text_val)
        if date_norm is not None:
            return date_norm

    num = _try_parse_number(text_val)
    if num is not None:
        return _format_number(num)

    return text_val


def normalize_series_for_compare(
    series: pd.Series,
    *,
    for_key: bool = False,
    as_time: bool = False,
) -> pd.Series:
    return series.map(lambda v: _normalize_scalar(v, for_key=for_key, as_time=as_time))


def compare_columns(
    csv_series: pd.Series,
    sql_series: pd.Series,
    key_csv: Optional[pd.Series] = None,
    key_sql: Optional[pd.Series] = None,
) -> Tuple[bool, int, int, float]:
    csv_norm = normalize_series_for_compare(csv_series.reset_index(drop=True))
    sql_norm = normalize_series_for_compare(sql_series.reset_index(drop=True))

    if key_csv is not None and key_sql is not None:
        left = pd.DataFrame(
            {
                "key": normalize_series_for_compare(key_csv.reset_index(drop=True), for_key=True),
                "val": csv_norm,
            }
        )
        right = pd.DataFrame(
            {
                "key": normalize_series_for_compare(key_sql.reset_index(drop=True), for_key=True),
                "val": sql_norm,
            }
        )
        left["_seq"] = left.groupby("key", sort=False).cumcount()
        right["_seq"] = right.groupby("key", sort=False).cumcount()
        left = left.sort_values(["key", "_seq"], kind="mergesort").reset_index(drop=True)
        right = right.sort_values(["key", "_seq"], kind="mergesort").reset_index(drop=True)

        total = max(len(left), len(right))
        if total == 0:
            return True, 0, 0, 100.0

        compare_len = min(len(left), len(right))
        matched_mask = left.iloc[:compare_len]["val"] == right.iloc[:compare_len]["val"]
        matched = int(matched_mask.sum())
        mismatched = total - matched
        pct = 100.0 * matched / total if total else 0.0
        full_match = (
            len(left) == len(right)
            and mismatched == 0
            and left["key"].tolist() == right["key"].tolist()
        )
        return full_match, matched, mismatched, pct

    c_csv = Counter(csv_norm.tolist())
    c_sql = Counter(sql_norm.tolist())
    if not c_csv and not c_sql:
        return True, 0, 0, 100.0

    matched = sum(min(c_csv[k], c_sql[k]) for k in c_csv.keys() | c_sql.keys())
    total = max(len(csv_norm), len(sql_norm))
    mismatched = total - matched
    pct = 100.0 * matched / total if total else 0.0
    full_match = c_csv == c_sql
    return full_match, matched, mismatched, pct


def _row_alignment_key(df: pd.DataFrame, *, is_csv: bool) -> pd.Series:
    """Key for aligning service-level rows: visit + service code + service name."""
    if is_csv:
        visit_col, code_col, name_col = KEY_COL, CODE_COL_CSV, NAME_COL_CSV
    else:
        visit_col, code_col, name_col = "visit_id", "code", "serv_name"
    return (
        normalize_series_for_compare(df[visit_col], for_key=True)
        + "|"
        + normalize_series_for_compare(df[code_col], for_key=True)
        + "|"
        + normalize_series_for_compare(df[name_col], for_key=True)
    )


def align_csv_sql_rows(df_csv: pd.DataFrame, df_sql: pd.DataFrame) -> pd.DataFrame:
    """Inner-join CSV and SQL on service-level key (with duplicate sequence)."""
    left = df_csv.copy()
    right = df_sql.copy()
    left["_align_key"] = _row_alignment_key(left, is_csv=True)
    right["_align_key"] = _row_alignment_key(right, is_csv=False)
    left["_align_seq"] = left.groupby("_align_key", sort=False).cumcount()
    right["_align_seq"] = right.groupby("_align_key", sort=False).cumcount()
    return left.merge(
        right,
        on=["_align_key", "_align_seq"],
        how="inner",
        suffixes=("_csv_dup", "_sql_dup"),
    )


def _match_stats_on_series(csv_series: pd.Series, sql_series: pd.Series) -> Tuple[bool, int, int, float]:
    a = normalize_series_for_compare(csv_series.reset_index(drop=True)).to_numpy()
    b = normalize_series_for_compare(sql_series.reset_index(drop=True)).to_numpy()
    total = max(len(a), len(b))
    if total == 0:
        return True, 0, 0, 100.0
    compare_len = min(len(a), len(b))
    matched = int((a[:compare_len] == b[:compare_len]).sum())
    mismatched = total - matched
    pct = 100.0 * matched / total if total else 0.0
    full_match = len(a) == len(b) and mismatched == 0
    return full_match, matched, mismatched, pct


def build_column_mapping(
    df_csv: pd.DataFrame,
    df_sql: pd.DataFrame,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Finds SQL column for each CSV column by content (all SQL columns), not by position.
    Rows are aligned on visit_id + code + serv_name before comparison.
    """
    n_csv = len(df_csv.columns)
    n_sql = len(df_sql.columns)
    if n_sql < n_csv:
        raise ValueError(
            f"SQL has fewer columns ({n_sql}) than CSV ({n_csv}). Expected SQL >= CSV."
        )

    merged = align_csv_sql_rows(df_csv, df_sql)
    if merged.empty:
        raise ValueError(
            "No aligned rows between CSV and SQL. "
            "Check visit_id/code/serv_name keys."
        )

    sql_cols = list(df_sql.columns)
    norm_cache: dict[str, Any] = {}

    def norm_col(col_name: str) -> Any:
        if col_name not in norm_cache:
            norm_cache[col_name] = normalize_series_for_compare(
                merged[col_name].reset_index(drop=True),
                as_time=_is_time_column(col_name),
            ).to_numpy()
        return norm_cache[col_name]

    candidates: list[Tuple[float, str, str, bool, int, int]] = []

    for csv_name in df_csv.columns:
        csv_in = csv_name if csv_name in merged.columns else f"{csv_name}_csv_dup"
        if csv_in not in merged.columns:
            continue
        a = norm_col(csv_in)
        for sql_name in sql_cols:
            sql_in = sql_name if sql_name in merged.columns else f"{sql_name}_sql_dup"
            if sql_in not in merged.columns:
                continue
            b = norm_col(sql_in)
            total = max(len(a), len(b))
            if total == 0:
                candidates.append((100.0, csv_name, sql_name, True, 0, 0))
                continue
            compare_len = min(len(a), len(b))
            matched = int((a[:compare_len] == b[:compare_len]).sum())
            mismatched = total - matched
            pct = 100.0 * matched / total
            full_match = len(a) == len(b) and mismatched == 0
            candidates.append((pct, csv_name, sql_name, full_match, matched, mismatched))

    mapping: dict[str, Tuple[str, bool, int, int, float]] = {}
    assigned_sql: set[str] = set()
    candidate_index = {
        (csv_name, sql_name): (full_match, matched, mismatched, pct)
        for pct, csv_name, sql_name, full_match, matched, mismatched in candidates
    }

    for csv_name, sql_name in PREFERRED_SQL_MAP.items():
        if csv_name not in df_csv.columns or sql_name not in sql_cols:
            continue
        if sql_name in assigned_sql:
            continue
        stats = candidate_index.get((csv_name, sql_name))
        if stats:
            full_match, matched, mismatched, pct = stats
        else:
            full_match, matched, mismatched, pct = False, 0, len(merged), 0.0
        mapping[csv_name] = (sql_name, full_match, matched, mismatched, pct)
        assigned_sql.add(sql_name)

    # Each remaining CSV column gets one SQL column; highest match first.
    while len(mapping) < len(df_csv.columns):
        best_pair: Optional[Tuple[str, str, float, bool, int, int]] = None
        for pct, csv_name, sql_name, full_match, matched, mismatched in candidates:
            if csv_name in mapping or sql_name in assigned_sql:
                continue
            if best_pair is None or pct > best_pair[2]:
                best_pair = (csv_name, sql_name, pct, full_match, matched, mismatched)
        if best_pair is None:
            break
        csv_name, sql_name, pct, full_match, matched, mismatched = best_pair
        mapping[csv_name] = (sql_name, full_match, matched, mismatched, pct)
        assigned_sql.add(sql_name)

    for csv_name in df_csv.columns:
        if csv_name in mapping:
            continue
        best = None
        for pct, c_name, sql_name, full_match, matched, mismatched in candidates:
            if c_name != csv_name or sql_name in assigned_sql:
                continue
            if best is None or pct > best[0]:
                best = (pct, sql_name, full_match, matched, mismatched)
        if best:
            mapping[csv_name] = (best[1], best[2], best[3], best[4], best[0])
            assigned_sql.add(best[1])

    rows = []
    for pos, csv_name in enumerate(df_csv.columns, start=1):
        if csv_name not in mapping:
            rows.append(
                {
                    "position": pos,
                    "csv_column": csv_name,
                    "sql_original_name": None,
                    "sql_new_name": csv_name,
                    "column_mapped": False,
                    "data_match": False,
                    "matched_count": 0,
                    "mismatched_count": len(merged),
                    "match_percent": 0.0,
                    "aligned_rows": len(merged),
                }
            )
            continue
        sql_name, full_match, matched, mismatched, pct = mapping[csv_name]
        rows.append(
            {
                "position": pos,
                "csv_column": csv_name,
                "sql_original_name": sql_name,
                "sql_new_name": csv_name,
                "column_mapped": True,
                "data_match": full_match,
                "matched_count": matched,
                "mismatched_count": mismatched,
                "match_percent": round(pct, 2),
                "aligned_rows": len(merged),
            }
        )

    report = pd.DataFrame(rows)
    sql_order = [mapping[c][0] for c in df_csv.columns if c in mapping]
    df_sql_trimmed = df_sql[sql_order].copy()
    df_sql_trimmed.columns = list(df_csv.columns)
    return df_sql_trimmed, report


def apply_column_mapping(
    df_sql_trimmed: pd.DataFrame,
    mapping_report: pd.DataFrame,
) -> pd.DataFrame:
    # df_sql_trimmed already uses CSV column names in build_column_mapping
    return df_sql_trimmed.copy()


def save_compare_result(
    output_path: str,
    df_csv_period: pd.DataFrame,
    df_sql_period: pd.DataFrame,
    df_sql_final: pd.DataFrame,
    columns_mapping_report: pd.DataFrame,
) -> None:
    with pd.ExcelWriter(output_path, engine="openpyxl") as writer:
        df_csv_period.to_excel(writer, sheet_name="csv_period", index=False)
        df_sql_period.to_excel(writer, sheet_name="sql_period_original", index=False)
        df_sql_final.to_excel(writer, sheet_name="sql_final_renamed", index=False)
        columns_mapping_report.to_excel(writer, sheet_name="columns_mapping_report", index=False)


def print_summary(
    start_date: pd.Timestamp,
    end_inclusive: pd.Timestamp,
    df_csv_period: pd.DataFrame,
    df_sql_period: pd.DataFrame,
    columns_mapping_report: pd.DataFrame,
    df_sql_final: pd.DataFrame,
) -> None:
    print("=" * 60)
    print("\u041f\u0435\u0440\u0438\u043e\u0434 \u0434\u0430\u0442 \u0438\u0437 CSV:")
    print(f"  \u0441 {start_date.strftime(DATE_FMT_NORMALIZED)}")
    print(f"  \u043f\u043e {end_inclusive.strftime(DATE_FMT_NORMALIZED)} (\u0432\u043a\u043b\u044e\u0447\u0438\u0442\u0435\u043b\u044c\u043d\u043e)")
    print(
        f"\u0420\u0430\u0437\u043c\u0435\u0440 CSV \u0437\u0430 \u043f\u0435\u0440\u0438\u043e\u0434: "
        f"{len(df_csv_period):,} \u0441\u0442\u0440\u043e\u043a x {len(df_csv_period.columns)} \u043a\u043e\u043b\u043e\u043d\u043e\u043a"
    )
    print(
        f"\u0420\u0430\u0437\u043c\u0435\u0440 SQL \u0437\u0430 \u043f\u0435\u0440\u0438\u043e\u0434: "
        f"{len(df_sql_period):,} \u0441\u0442\u0440\u043e\u043a x {len(df_sql_period.columns)} \u043a\u043e\u043b\u043e\u043d\u043e\u043a"
    )
    print(f"\u041a\u043e\u043b\u043e\u043d\u043e\u043a CSV: {len(df_csv_period.columns)}")
    print(f"\u041a\u043e\u043b\u043e\u043d\u043e\u043a SQL (\u0438\u0441\u0445\u043e\u0434\u043d\u044b\u0445): {len(df_sql_period.columns)}")
    print("-" * 60)
    mapped = int(columns_mapping_report.get("column_mapped", pd.Series(dtype=bool)).sum())
    full = int(columns_mapping_report["data_match"].sum())
    print(
        f"\u041a\u043e\u043b\u043e\u043d\u043e\u043a \u0441\u043e\u043f\u043e\u0441\u0442\u0430\u0432\u043b\u0435\u043d\u043e (column_mapped): {mapped}"
    )
    print(f"\u041a\u043e\u043b\u043e\u043d\u043e\u043a \u0441 100% \u0434\u0430\u043d\u043d\u044b\u0445 (data_match): {full}")
    print("\u041e\u0442\u0447\u0451\u0442 \u0441\u043e\u043f\u043e\u0441\u0442\u0430\u0432\u043b\u0435\u043d\u0438\u044f \u043a\u043e\u043b\u043e\u043d\u043e\u043a (columns_mapping_report):")
    print(columns_mapping_report.to_string(index=False))
    print("-" * 60)
    print("\u041f\u0435\u0440\u0432\u044b\u0435 \u0441\u0442\u0440\u043e\u043a\u0438 df_sql_final:")
    with pd.option_context("display.max_columns", 10, "display.width", 200):
        print(df_sql_final.head())
    print("=" * 60)


def main() -> None:
    password = get_db_password()

    print(f"\u0427\u0442\u0435\u043d\u0438\u0435 CSV: {csv_path}")
    df_csv = read_csv_report(csv_path)

    start_date, end_inclusive, end_plus_1 = get_date_period_from_csv(df_csv)
    if COMPARE_FIRST_DAY_ONLY:
        df_csv_period = filter_first_day(df_csv, start_date, end_plus_1)
        sql_start, sql_end = start_date, start_date + pd.Timedelta(days=1)
        report_end = start_date
        output_xlsx = OUTPUT_XLSX_DAY1
        print(
            f"\u0420\u0435\u0436\u0438\u043c: \u0442\u043e\u043b\u044c\u043a\u043e \u043f\u0435\u0440\u0432\u044b\u0439 \u0434\u0435\u043d\u044c "
            f"({start_date.strftime(DATE_FMT_NORMALIZED)})"
        )
    else:
        df_csv_period = filter_csv_by_period(df_csv, start_date, end_plus_1)
        sql_start, sql_end = start_date, end_plus_1
        report_end = end_inclusive
        output_xlsx = OUTPUT_XLSX_FULL

    print("\u041f\u043e\u0434\u043a\u043b\u044e\u0447\u0435\u043d\u0438\u0435 \u043a \u0431\u0430\u0437\u0435 \u0434\u0430\u043d\u043d\u044b\u0445...")
    engine = create_db_engine(host, port, database, user, password)

    print(
        f"\u0417\u0430\u0433\u0440\u0443\u0437\u043a\u0430 SQL \u0438\u0437 {table_name} "
        f"\u0437\u0430 \u043f\u0435\u0440\u0438\u043e\u0434 [{sql_start.date()}, {sql_end.date()})..."
    )
    df_sql_period = load_sql_period(engine, table_name, sql_start, sql_end)
    engine.dispose()

    df_sql_trimmed, columns_mapping_report = build_column_mapping(df_csv_period, df_sql_period)
    df_sql_final = apply_column_mapping(df_sql_trimmed, columns_mapping_report)
    save_compare_result(
        output_xlsx,
        df_csv_period,
        df_sql_period,
        df_sql_final,
        columns_mapping_report,
    )

    print_summary(
        start_date,
        report_end,
        df_csv_period,
        df_sql_period,
        columns_mapping_report,
        df_sql_final,
    )
    print(f"\u0420\u0435\u0437\u0443\u043b\u044c\u0442\u0430\u0442\u044b \u0441\u043e\u0445\u0440\u0430\u043d\u0435\u043d\u044b \u0432 {output_xlsx}")


if __name__ == "__main__":
    main()
