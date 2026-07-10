# -*- coding: utf-8 -*-
"""
Read Zoho Analytics table «Прогноз_CF» and push two sums to Google Sheets.

Sum 1 (current week):
  Всего за неделю (факт+прогноз+страховые) + Сумма по Вероятности текущая неделя

Sum 2 (next week):
  Прогноз на 5 дней след. недели + Прогноз страховые след. неделя
  + Сумма по Вероятности следующая неделя

CLI:
  python zoho_prognosis_sheets.py run
  python zoho_prognosis_sheets.py run --dry-run
  python zoho_prognosis_sheets.py show
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from zoho_analytics import run_async_sql_export

_PROJECT_DIR = Path(__file__).resolve().parent
_PROGNOSIS_CONFIG = _PROJECT_DIR / "cloud" / "prognosis-config.env"

load_dotenv()
# Non-secret targets (cells, sheet, Zoho table): single file for Mac + GCP
if _PROGNOSIS_CONFIG.exists():
    load_dotenv(_PROGNOSIS_CONFIG, override=True)

ZOHO_PROGNOSIS_TABLE = os.getenv("ZOHO_PROGNOSIS_TABLE", "Прогноз_CF")

COL_WEEK_TOTAL = "Всего за неделю (факт+прогноз+страховые)"
COL_PROB_CURRENT = "Сумма по Вероятности текущая неделя"
COL_FORECAST_5D = "Прогноз на 5 дней след. недели"
COL_INSURANCE_NEXT = "Прогноз страховые след. неделя"
COL_PROB_NEXT = "Сумма по Вероятности следующая неделя"

DEFAULT_BQ_CREDENTIALS = (
    "/Users/pavel/Kravira_Work/Jupyter/PL/carbide-datum-383616-8960c7f83f5b.json"
)
DEFAULT_SHEETS_CREDENTIALS = str(
    Path(__file__).resolve().parent / "sonorous-saga-321204-35715098b819.json"
)
DEFAULT_FORMULA_A142 = str(
    _PROJECT_DIR / "cloud" / "formulas" / "a142_combined.formula"
)
LOG_DIR = _PROJECT_DIR / "logs"
STATUS_FILE = LOG_DIR / "prognosis_sheets_status.json"


@dataclass
class PrognosisSums:
    current_week: float
    next_week: float
    as_of: str

    def as_dict(self) -> dict:
        return {
            "current_week": round(self.current_week, 2),
            "next_week": round(self.next_week, 2),
            "as_of": self.as_of,
        }


def _num(value: object) -> float:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0.0
    return float(pd.to_numeric(value, errors="coerce") or 0.0)


def fetch_prognosis_row() -> pd.Series:
    sql = f'SELECT * FROM "{ZOHO_PROGNOSIS_TABLE}"'
    csv_text = run_async_sql_export(sql)
    df = pd.read_csv(io.StringIO(csv_text.lstrip("\ufeff")))
    if df.empty:
        raise RuntimeError(f'Zoho table "{ZOHO_PROGNOSIS_TABLE}" returned no rows')
    return df.iloc[0]


def compute_sums(row: pd.Series) -> PrognosisSums:
    current = _num(row[COL_WEEK_TOTAL]) + _num(row[COL_PROB_CURRENT])
    nxt = (
        _num(row[COL_FORECAST_5D])
        + _num(row[COL_INSURANCE_NEXT])
        + _num(row[COL_PROB_NEXT])
    )
    return PrognosisSums(
        current_week=current,
        next_week=nxt,
        as_of=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
    )


def sheets_config() -> dict[str, str]:
    return {
        "spreadsheet_id": os.getenv("GOOGLE_SHEETS_ID", "").strip(),
        "worksheet": os.getenv("GOOGLE_SHEETS_WORKSHEET", "Лист1").strip(),
        "worksheet_gid": os.getenv("GOOGLE_SHEETS_GID", "").strip(),
        "cell_current": os.getenv("GOOGLE_SHEETS_CELL_CURRENT", "A140").strip(),
        "cell_next": os.getenv("GOOGLE_SHEETS_CELL_NEXT", "A141").strip(),
        "cell_combined": os.getenv("GOOGLE_SHEETS_CELL_COMBINED", "").strip(),
        "formula_combined_file": os.getenv(
            "GOOGLE_SHEETS_FORMULA_COMBINED_FILE", DEFAULT_FORMULA_A142
        ).strip(),
        "cell_updated_at": os.getenv("GOOGLE_SHEETS_CELL_UPDATED_AT", "").strip(),
        "credentials": os.getenv("GOOGLE_SHEETS_CREDENTIALS", DEFAULT_SHEETS_CREDENTIALS).strip(),
    }


def load_sheet_formula(path: str) -> str | None:
    if not path:
        return None
    formula_path = Path(path)
    if not formula_path.is_absolute():
        formula_path = _PROJECT_DIR / formula_path
    if not formula_path.exists():
        return None
    formula = formula_path.read_text(encoding="utf-8").strip()
    if not formula:
        return None
    if not formula.startswith("="):
        formula = "=" + formula
    return formula


def _open_worksheet(gc, cfg: dict[str, str]):
    sh = gc.open_by_key(cfg["spreadsheet_id"])
    if cfg["worksheet_gid"]:
        return sh.get_worksheet_by_id(int(cfg["worksheet_gid"]))
    return sh.worksheet(cfg["worksheet"])


def write_to_google_sheets(sums: PrognosisSums, *, dry_run: bool = False) -> None:
    cfg = sheets_config()
    if not cfg["spreadsheet_id"]:
        raise ValueError(
            "GOOGLE_SHEETS_ID is not set — edit cloud/prognosis-config.env"
        )

    if dry_run:
        print("Dry-run: would write to Google Sheets:")
        print(f"  {cfg['cell_updated_at']}: {sums.as_of}")
        print(f"  {cfg['cell_current']}: {sums.current_week:.2f}")
        print(f"  {cfg['cell_next']}: {sums.next_week:.2f}")
        if cfg["cell_combined"]:
            formula = load_sheet_formula(cfg["formula_combined_file"])
            print(f"  {cfg['cell_combined']}: formula ({len(formula or '')} chars)")
        return

    import gspread
    from google.oauth2.service_account import Credentials

    creds_path = Path(cfg["credentials"])
    if not creds_path.exists():
        raise FileNotFoundError(f"Google credentials not found: {creds_path}")

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file(str(creds_path), scopes=scopes)
    try:
        gc = gspread.authorize(creds)
        ws = _open_worksheet(gc, cfg)
    except PermissionError as e:
        email = ""
        try:
            import json
            email = json.loads(Path(cfg["credentials"]).read_text()).get("client_email", "")
        except OSError:
            pass
        raise PermissionError(
            f"Google Sheets access denied. Share the spreadsheet with {email or 'the service account'} "
            f"and enable Sheets + Drive API in the service account GCP project."
        ) from e
    except Exception as e:
        if "sheets.googleapis.com" in str(e).lower() or "has not been used" in str(e).lower():
            raise RuntimeError(
                "Enable Google Sheets API: "
                "https://console.cloud.google.com/apis/library/sheets.googleapis.com?project=carbide-datum-383616"
            ) from e
        raise

    if cfg["cell_updated_at"]:
        ws.update_acell(cfg["cell_updated_at"], sums.as_of)
    ws.update_acell(cfg["cell_current"], round(sums.current_week, 2))
    ws.update_acell(cfg["cell_next"], round(sums.next_week, 2))
    if cfg["cell_combined"]:
        formula = load_sheet_formula(cfg["formula_combined_file"])
        if formula:
            ws.update(
                cfg["cell_combined"],
                [[formula]],
                value_input_option="USER_ENTERED",
            )
            print(f"  formula -> {cfg['cell_combined']}")
    print(f"  sheet: {ws.title} (gid={ws.id})")


def save_status(sums: PrognosisSums, *, success: bool, error: str | None = None) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "finished_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "success": success,
        "error": error,
        **sums.as_dict(),
        "cells": {
            "current": sheets_config()["cell_current"],
            "next": sheets_config()["cell_next"],
            "combined": sheets_config()["cell_combined"],
            "updated_at": sheets_config()["cell_updated_at"],
        },
    }
    STATUS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cmd_show() -> int:
    row = fetch_prognosis_row()
    sums = compute_sums(row)
    print(f"Table: {ZOHO_PROGNOSIS_TABLE}")
    print(f"Сегодня: {row.get('Сегодня', '')}")
    print()
    print(f"Текущая неделя ({COL_WEEK_TOTAL} + {COL_PROB_CURRENT}):")
    print(f"  {_num(row[COL_WEEK_TOTAL]):,.2f} + {_num(row[COL_PROB_CURRENT]):,.2f} = {sums.current_week:,.2f}")
    print()
    print(f"Следующая неделя ({COL_FORECAST_5D} + {COL_INSURANCE_NEXT} + {COL_PROB_NEXT}):")
    print(
        f"  {_num(row[COL_FORECAST_5D]):,.2f} + {_num(row[COL_INSURANCE_NEXT]):,.2f} "
        f"+ {_num(row[COL_PROB_NEXT]):,.2f} = {sums.next_week:,.2f}"
    )
    return 0


def cmd_run(*, dry_run: bool = False) -> int:
    sums = compute_sums(fetch_prognosis_row())
    print(f"Current week sum: {sums.current_week:,.2f}")
    print(f"Next week sum:    {sums.next_week:,.2f}")
    try:
        write_to_google_sheets(sums, dry_run=dry_run)
        if not dry_run:
            save_status(sums, success=True)
            print("Google Sheets updated.")
        return 0
    except Exception as e:
        if not dry_run:
            save_status(sums, success=False, error=str(e))
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Push Прогноз_CF sums to Google Sheets")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("show", help="Print computed sums from Zoho")
    p_run = sub.add_parser("run", help="Compute and write to Google Sheets")
    p_run.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "show":
            return cmd_show()
        if args.cmd == "run":
            return cmd_run(dry_run=getattr(args, "dry_run", False))
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
