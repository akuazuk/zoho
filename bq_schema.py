# -*- coding: utf-8 -*-
"""BigQuery target schema for kravira_last.combined_report_temp."""

from __future__ import annotations

import re

from google.cloud import bigquery

from mis_column_mapping import CSV_COLUMNS

# 29 base columns from MIS + 6 derived (as in combined_report_temp)
BQ_BASE_COLUMNS = list(CSV_COLUMNS)

BQ_DERIVED_COLUMNS = [
    "Сумма материалов",
    "Неделя визита",
    "Месяц визита",
    "Направления",
    "ФИО",
    "Дата_и_время",
]

BQ_ALL_COLUMNS = BQ_BASE_COLUMNS + BQ_DERIVED_COLUMNS

DATE_COL = "Дата визита"

# pay_type code (local SQLite) -> label (BigQuery / CSV)
PAY_TYPE_MAP: dict[str, str] = {
    "0": "наличный",
    "0.0": "наличный",
    "2": "терминал",
    "2.0": "терминал",
    "3": "полис",
    "3.0": "полис",
    "12": "договор",
    "12.0": "договор",
}

# specialty -> direction (canonical keys; lookup is case-insensitive)
SPECIALTY_TO_DIRECTION: dict[str, str] = {
    "Аллерголог": "Терапия",
    "Гастроэнтеролог": "Терапия",
    "Гинеколог": "Гинекология",
    "Дерматолог": "Терапия",
    "Детский кардиолог": "Педиатрия",
    "Детский невролог": "Педиатрия",
    "Детский стоматолог": "Стоматология",
    "Детский хирург": "Хирургия",
    "Кардиолог": "Терапия",
    "Кардиолог-аритмолог": "Терапия",
    "Косметолог": "Терапия",
    "Косметолог/дерматолог": "Терапия",
    "Лазерный хирург": "Хирургия",
    "ЛОР-врач": "Хирургия",
    "Логопед": "Педиатрия",
    "Маммолог": "Терапия",
    "массаж": "Терапия",
    "медицинская сестра": "Функциональная диагностика",
    "Невролог": "Терапия",
    "Онколог": "Терапия",
    "Ортодонт": "Стоматология",
    "Ортопед-травматолог": "Хирургия",
    "Офтальмолог": "Терапия",
    "Педиатр": "Педиатрия",
    "Пластический хирург": "Хирургия",
    "Проктолог": "Хирургия",
    "процедурная медсестра": "Лаборатория",
    "Психолог": "Терапия",
    "Психотерапевт": "Терапия",
    "Ревматолог": "Терапия",
    "Рентгенлаборант": "Стоматология",
    "Стоматолог": "Стоматология",
    "Стоматолог-терапевт": "Стоматология",
    "Стоматолог-хирург": "Стоматология",
    "стоматолог-ортопед": "Стоматология",
    "Терапевт": "Терапия",
    "Уролог": "Хирургия",
    "Физиотерапевтическая медсестра": "Терапия",
    "Физиотерапия": "Терапия",
    "Флеболог": "Терапия",
    "фельдшер выездной бригады": "Терапия",
    "Хирург": "Хирургия",
    "Хирург-онколог": "Хирургия",
    "врач УЗД": "Функциональная диагностика",
    "ЭКГ+Сопрано": "Функциональная диагностика",
    "эндоскопия": "Эндоскопия",
    "Эндокринолог": "Терапия",
    "Эндоскопист": "Эндоскопия",
}

_SPECIALTY_LOOKUP: dict[str, str] = {
    key.strip().lower(): value for key, value in SPECIALTY_TO_DIRECTION.items()
}


def map_specialty_to_direction(spec: object) -> str:
    """Map «Специальность врача» to «Направления» (case-insensitive)."""
    if spec is None:
        return ""
    text = str(spec).strip()
    if not text or text.lower() in {"nan", "none"}:
        return ""
    if text in SPECIALTY_TO_DIRECTION:
        return SPECIALTY_TO_DIRECTION[text]
    return _SPECIALTY_LOOKUP.get(text.lower(), "")


# Canonical «Филиал» labels (legacy Zoho / report_last format).
# MariaDB may use commas: «ул. Захарова, 50Д» vs «ул. Захарова  50Д».
_FILIAL_CANONICAL: dict[str, str] = {
    "ул. захарова 50д": "ул. Захарова  50Д",
    "пр-т победителей 45": "пр-т Победителей 45",
    "ул. скрипникова 11б": "ул. Скрипникова  11Б",
    "стоматология победителей 45": "Стоматология Победителей 45",
    "операционная победителей 45": "Операционная Победителей  45",
    "операционная скрипникова 11б": "Операционная Скрипникова  11Б",
    "выездная служба": "Выездная служба",
}


def _filial_lookup_key(value: str) -> str:
    text = re.sub(r"\s+", " ", value.replace(",", " ").strip())
    return text.casefold()


def map_filial(value: object) -> str:
    """Normalize «Филиал» to canonical spelling (ignore commas/extra spaces)."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none"}:
        return ""
    canonical = _FILIAL_CANONICAL.get(_filial_lookup_key(text))
    return canonical if canonical is not None else text


def bq_table_schema() -> list[bigquery.SchemaField]:
    return [
        bigquery.SchemaField("ID визита", "INTEGER"),
        bigquery.SchemaField("Дата визита", "DATE"),
        bigquery.SchemaField("Время визита", "TIME"),
        bigquery.SchemaField("ID врача", "INTEGER"),
        bigquery.SchemaField("ФИО врача", "STRING"),
        bigquery.SchemaField("Специальность врача", "STRING"),
        bigquery.SchemaField("ID пациента", "INTEGER"),
        bigquery.SchemaField("Дата рождения", "DATE"),
        bigquery.SchemaField("Диагноз МКБ-10", "STRING"),
        bigquery.SchemaField("Счет", "INTEGER"),
        bigquery.SchemaField("Тип оплаты", "STRING"),
        bigquery.SchemaField("Компания", "STRING"),
        bigquery.SchemaField("Договор", "STRING"),
        bigquery.SchemaField("Общая стоимость визита со скидкой", "FLOAT"),
        bigquery.SchemaField("Филиал", "STRING"),
        bigquery.SchemaField("Кабинет", "STRING"),
        bigquery.SchemaField("Время приема в минутах", "FLOAT"),
        bigquery.SchemaField("Код услуги", "STRING"),
        bigquery.SchemaField("Наименование услуги", "STRING"),
        bigquery.SchemaField("Кол-во", "FLOAT"),
        bigquery.SchemaField("Скидка", "STRING"),
        bigquery.SchemaField("Материалы без НДС", "FLOAT"),
        bigquery.SchemaField("Материалы с НДС 10%", "FLOAT"),
        bigquery.SchemaField("Материалы с НДС 20%", "FLOAT"),
        bigquery.SchemaField("Услуга без НДС", "FLOAT"),
        bigquery.SchemaField("Услуга с НДС", "FLOAT"),
        bigquery.SchemaField("Услуга сторонних организаций", "FLOAT"),
        bigquery.SchemaField("Стоимость услуги без скидки", "FLOAT"),
        bigquery.SchemaField("Стоимость услуги со скидкой", "FLOAT"),
        bigquery.SchemaField("Сумма материалов", "FLOAT"),
        bigquery.SchemaField("Неделя визита", "DATE"),
        bigquery.SchemaField("Месяц визита", "DATE"),
        bigquery.SchemaField("Направления", "STRING"),
        bigquery.SchemaField("ФИО", "STRING"),
        bigquery.SchemaField("Дата_и_время", "TIMESTAMP"),
    ]
