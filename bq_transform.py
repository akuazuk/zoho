# -*- coding: utf-8 -*-
"""Transform local mis_data rows to combined_report_temp format."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pandas as pd

from bq_schema import (
    BQ_ALL_COLUMNS,
    BQ_BASE_COLUMNS,
    DATE_COL,
    PAY_TYPE_MAP,
    map_filial,
    map_specialty_to_direction,
)
from mis_column_mapping import CSV_COLUMNS

LOCAL_DB_DEFAULT = Path(__file__).resolve().parent / "data" / "mis_local.sqlite"


def _parse_time(series: pd.Series) -> pd.Series:
    s = series.astype(str).str.strip()
    s = s.replace({"nan": "", "None": "", "<NA>": ""})
    parsed = pd.to_datetime(s, format="%H:%M", errors="coerce")
    mask = parsed.isna() & s.ne("")
    if mask.any():
        parsed.loc[mask] = pd.to_datetime(s.loc[mask], errors="coerce")
    return parsed.dt.time


def _short_fio(name: object) -> str | None:
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return None
    text = str(name).strip()
    if not text:
        return None
    parts = text.split()
    if len(parts) == 1:
        return parts[0]
    initials = "".join(f"{p[0]}." for p in parts[1:3])
    return f"{parts[0]} {initials}".strip()


def _map_pay_type(value: object) -> str | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = str(value).strip()
    if text in PAY_TYPE_MAP:
        return PAY_TYPE_MAP[text]
    if text.lower() in {"полис", "терминал", "наличный", "договор"}:
        return text.lower()
    num = pd.to_numeric(text, errors="coerce")
    if pd.notna(num):
        key = str(int(num)) if float(num) == int(num) else str(num)
        return PAY_TYPE_MAP.get(key, text)
    return text or None


def _calc_sum_materials(df: pd.DataFrame) -> pd.Series:
    m1 = pd.to_numeric(df["Материалы без НДС"], errors="coerce").fillna(0)
    m2 = pd.to_numeric(df["Материалы с НДС 10%"], errors="coerce").fillna(0)
    m3 = pd.to_numeric(df["Материалы с НДС 20%"], errors="coerce").fillna(0)
    total = m1 + m2 + m3
    # In BQ: non-zero only when sum of all material columns is reported (>0 block)
    return total.where(total > 0, 0.0)


def transform_to_bq(df: pd.DataFrame) -> pd.DataFrame:
    """Local/SQLite dataframe -> combined_report_temp layout."""
    out = df[CSV_COLUMNS].copy()

    out[DATE_COL] = pd.to_datetime(out[DATE_COL], errors="coerce").dt.date
    out["Дата рождения"] = pd.to_datetime(out["Дата рождения"], errors="coerce").dt.date

    times = _parse_time(out["Время визита"])
    out["Время визита"] = times

    for col in ("ID визита", "ID врача", "ID пациента", "Счет"):
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("Int64")

    for col in (
        "Общая стоимость визита со скидкой",
        "Время приема в минутах",
        "Кол-во",
        "Материалы без НДС",
        "Материалы с НДС 10%",
        "Материалы с НДС 20%",
        "Услуга без НДС",
        "Услуга с НДС",
        "Услуга сторонних организаций",
        "Стоимость услуги без скидки",
        "Стоимость услуги со скидкой",
    ):
        out[col] = pd.to_numeric(out[col], errors="coerce")

    out["Диагноз МКБ-10"] = out["Диагноз МКБ-10"].apply(
        lambda v: None if pd.isna(v) or str(v).strip().lower() in {"", "nan", "none"} else str(v).strip()
    )
    out["Договор"] = out["Договор"].apply(
        lambda v: None if pd.isna(v) or str(v).strip().lower() in {"", "nan", "none"} else str(v).strip()
    )
    out["Тип оплаты"] = out["Тип оплаты"].map(_map_pay_type)

    out["Скидка"] = pd.to_numeric(out["Скидка"], errors="coerce").map(
        lambda v: None if pd.isna(v) else f"{float(v):.2f}"
    )

    visit_ts = pd.to_datetime(out[DATE_COL].astype(str) + " " + pd.Series(times).astype(str), errors="coerce")
    out["Неделя визита"] = (visit_ts.dt.to_period("W-MON").dt.start_time.dt.date)
    out["Месяц визита"] = visit_ts.dt.to_period("M").dt.start_time.dt.date
    out["Сумма материалов"] = _calc_sum_materials(out)
    out["Филиал"] = out["Филиал"].map(map_filial)
    out["Направления"] = out["Специальность врача"].map(map_specialty_to_direction)
    out["ФИО"] = out["ФИО врача"].map(_short_fio)
    out["Дата_и_время"] = visit_ts.dt.tz_localize("UTC")

    return out[BQ_ALL_COLUMNS]


def load_local_period(
    start_date: str | None = None,
    end_date: str | None = None,
    *,
    db_path: str | Path = LOCAL_DB_DEFAULT,
) -> pd.DataFrame:
    cols = ", ".join(f'"{c}"' for c in CSV_COLUMNS)
    query = f"SELECT {cols} FROM mis_data"
    clauses = []
    params: list[str] = []
    if start_date:
        clauses.append(f'date("{DATE_COL}") >= date(?)')
        params.append(start_date)
    if end_date:
        clauses.append(f'date("{DATE_COL}") <= date(?)')
        params.append(end_date)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    with sqlite3.connect(db_path) as conn:
        return pd.read_sql(query, conn, params=params)
