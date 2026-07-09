# -*- coding: utf-8 -*-
"""Transform local mis_data -> Zoho Analytics import format (35 base columns)."""

from __future__ import annotations

import pandas as pd

from bq_schema import BQ_ALL_COLUMNS, DATE_COL, map_filial, map_specialty_to_direction
from bq_transform import (
    LOCAL_DB_DEFAULT,
    _calc_sum_materials,
    _map_pay_type,
    _parse_time,
    _short_fio,
    load_local_period,
)
from mis_column_mapping import CSV_COLUMNS

# Columns written to CSV for Zoho import (formula cols 41-51 are computed in Zoho)
ZOHO_IMPORT_COLUMNS = list(BQ_ALL_COLUMNS)


def transform_to_zoho(df: pd.DataFrame) -> pd.DataFrame:
    out = df[CSV_COLUMNS].copy()

    visit_dt = pd.to_datetime(out[DATE_COL], errors="coerce")
    bdate = pd.to_datetime(out["Дата рождения"], errors="coerce")
    times = _parse_time(out["Время визита"])

    out[DATE_COL] = visit_dt.dt.strftime("%Y-%m-%d")
    out["Дата рождения"] = bdate.dt.strftime("%Y-%m-%d")

    out["Время визита"] = pd.Series(times).apply(
        lambda t: t.strftime("%H:%M:%S") if t is not None and pd.notna(t) else ""
    )

    for col in ("ID визита", "ID врача", "ID пациента", "Счет"):
        out[col] = pd.to_numeric(out[col], errors="coerce")

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
        lambda v: "" if pd.isna(v) or str(v).strip().lower() in {"", "nan", "none"} else str(v).strip()
    )
    out["Договор"] = out["Договор"].apply(
        lambda v: "" if pd.isna(v) or str(v).strip().lower() in {"", "nan", "none"} else str(v).strip()
    )
    out["Тип оплаты"] = out["Тип оплаты"].map(_map_pay_type).fillna("")
    out["Скидка"] = pd.to_numeric(out["Скидка"], errors="coerce").map(
        lambda v: "" if pd.isna(v) else f"{float(v):.2f}"
    )

    time_str = (
        out["Время визита"]
        .astype(str)
        .replace({"nan": "00:00:00", "None": "00:00:00", "<NA>": "00:00:00", "": "00:00:00"})
    )
    visit_ts = pd.to_datetime(
        visit_dt.dt.strftime("%Y-%m-%d") + " " + time_str,
        errors="coerce",
    )
    out["Неделя визита"] = visit_ts.dt.to_period("W-MON").dt.start_time.dt.strftime("%Y-%m-%d")
    out["Месяц визита"] = visit_ts.dt.to_period("M").dt.start_time.dt.strftime("%Y-%m-%d")
    out["Сумма материалов"] = _calc_sum_materials(out)
    out["Филиал"] = out["Филиал"].map(map_filial)
    out["Направления"] = out["Специальность врача"].map(map_specialty_to_direction)
    out["ФИО"] = out["ФИО врача"].map(_short_fio).fillna("")
    out["Дата_и_время"] = visit_ts.dt.strftime("%Y-%m-%d %H:%M:%S")

    # Zoho-specific cleanup from To_Zoho notebook
    out["Кабинет"] = out["Кабинет"].astype(str).str.strip()
    out.loc[
        (out["ФИО врача"].astype(str).str.contains("Паломар", case=False, na=False))
        & (out["Кабинет"].isin(["", "nan", "None"])),
        "Кабинет",
    ] = "30 Паломар Сопрано"
    out.loc[
        (out["Специальность врача"].astype(str) == "ЭКГ+Сопрано")
        & (out["Кабинет"].isin(["", "nan", "None"])),
        "Кабинет",
    ] = "№ 1 Паломар Сопрано"
    out["Кабинет"] = out["Кабинет"].replace(
        "34 Паломар Сопрано Физио", "33 Паломар Сопрано Физио"
    )
    out["ФИО врача"] = out["ФИО врача"].astype(str).str.replace(
        "Пластическая хирургия и косметология", "Пластический хирург", regex=False
    )
    out["Кабинет"] = out["Кабинет"].replace(
        "31-33 Паломар Сопрано ( без кабинета)", "31-33 Паломар Сопрано ( каб,физио)"
    )

    return out[ZOHO_IMPORT_COLUMNS]


def load_zoho_period(
    start_date: str,
    end_date: str,
    *,
    db_path=LOCAL_DB_DEFAULT,
) -> pd.DataFrame:
    return transform_to_zoho(load_local_period(start_date, end_date, db_path=db_path))
