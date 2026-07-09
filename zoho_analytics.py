# -*- coding: utf-8 -*-
"""
Zoho Analytics API client (OAuth + metadata / workspaces).

Docs: https://www.zoho.com/analytics/api/v2/metadata-api/get-table-metadata.html

CLI:
    python zoho_analytics.py workspaces
    python zoho_analytics.py metadata
    python zoho_analytics.py test
"""

from __future__ import annotations

import argparse
import io
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from datetime import date
from typing import Any

import certifi

from dotenv import load_dotenv

load_dotenv()

DC = os.getenv("ZOHO_DC", "com")
ACCOUNTS_URL = f"https://accounts.zoho.{DC}/oauth/v2/token"
API_URL = f"https://analyticsapi.zoho.{DC}/restapi/v2"

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())


def _env(*keys: str, default: str = "") -> str:
    for key in keys:
        val = os.getenv(key, "").strip().strip('"').strip("'")
        if val:
            return val
    return default


def zoho_config() -> dict[str, str]:
    return {
        "client_id": _env("ZOHO_CLIENT_ID", "CLIENTID"),
        "client_secret": _env("ZOHO_CLIENT_SECRET", "CLIENTSECRET"),
        "refresh_token": _env("ZOHO_REFRESH_TOKEN", "REFRESHTOKEN"),
        "org_id": _env("ZOHO_ORG_ID", "ORGID"),
        "workspace_id": _env("ZOHO_WORKSPACE_ID", "WORKSPACEID"),
        "view_id": _env("ZOHO_VIEW_ID", "VIEWID"),
    }


def _post_form(url: str, data: dict[str, str]) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    with urllib.request.urlopen(req, timeout=60, context=_SSL_CTX) as resp:
        return json.loads(resp.read().decode())


def _get_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=120, context=_SSL_CTX) as resp:
        return json.loads(resp.read().decode())


def _request_json(
    url: str,
    headers: dict[str, str],
    *,
    method: str = "GET",
    data: bytes | None = None,
) -> dict[str, Any]:
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=300, context=_SSL_CTX) as resp:
        return json.loads(resp.read().decode())


def _get_text(url: str, headers: dict[str, str]) -> str:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=300, context=_SSL_CTX) as resp:
        return resp.read().decode("utf-8")


def _multipart_csv_upload(
    url: str,
    headers: dict[str, str],
    csv_path: Path,
) -> dict[str, Any]:
    boundary = f"----ZohoForm{uuid.uuid4().hex}"
    file_data = csv_path.read_bytes()
    body = io.BytesIO()
    body.write(f"--{boundary}\r\n".encode())
    body.write(f'Content-Disposition: form-data; name="FILE"; filename="{csv_path.name}"\r\n'.encode())
    body.write(b"Content-Type: text/csv\r\n\r\n")
    body.write(file_data)
    body.write(f"\r\n--{boundary}--\r\n".encode())
    upload_headers = {
        **headers,
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    return _request_json(url, upload_headers, method="POST", data=body.getvalue())


def get_access_token(cfg: dict[str, str] | None = None) -> str:
    cfg = cfg or zoho_config()
    for key in ("client_id", "client_secret", "refresh_token"):
        if not cfg.get(key) or "xxxxx" in cfg[key]:
            raise ValueError(f"Missing or invalid Zoho config: {key}")
    data = _post_form(
        ACCOUNTS_URL,
        {
            "refresh_token": cfg["refresh_token"],
            "client_id": cfg["client_id"],
            "client_secret": cfg["client_secret"],
            "grant_type": "refresh_token",
        },
    )
    if "access_token" not in data:
        raise RuntimeError(f"Token refresh failed: {data}")
    return data["access_token"]


def _headers(cfg: dict[str, str], access_token: str) -> dict[str, str]:
    return {
        "Authorization": f"Zoho-oauthtoken {access_token}",
        "ZANALYTICS-ORGID": cfg["org_id"],
    }


def list_workspaces(cfg: dict[str, str] | None = None) -> dict[str, Any]:
    cfg = cfg or zoho_config()
    token = get_access_token(cfg)
    return _get_json(f"{API_URL}/workspaces", _headers(cfg, token))


def delete_rows(
    cfg: dict[str, str] | None = None,
    *,
    criteria: str | None = None,
    delete_all: bool = False,
) -> dict[str, Any]:
    """Delete rows matching criteria. See Zoho Delete Row API."""
    cfg = cfg or zoho_config()
    token = get_access_token(cfg)
    config: dict[str, Any] = {}
    if delete_all:
        config["deleteAllRows"] = True
    elif criteria:
        config["criteria"] = criteria
    else:
        raise ValueError("Provide criteria or delete_all=True")
    params = urllib.parse.urlencode({"CONFIG": json.dumps(config, ensure_ascii=False)})
    url = f"{API_URL}/workspaces/{cfg['workspace_id']}/views/{cfg['view_id']}/rows?{params}"
    return _request_json(url, _headers(cfg, token), method="DELETE")


def import_csv(
    csv_path: Path,
    cfg: dict[str, str] | None = None,
    *,
    import_type: str = "append",
) -> dict[str, Any]:
    """Bulk import CSV into existing Zoho view."""
    cfg = cfg or zoho_config()
    token = get_access_token(cfg)
    config = {
        "importType": import_type,
        "fileType": "csv",
        "autoIdentify": True,
        "onError": "skiprow",
        "dateFormat": "yyyy-MM-dd",
    }
    params = urllib.parse.urlencode({"CONFIG": json.dumps(config)})
    url = f"{API_URL}/workspaces/{cfg['workspace_id']}/views/{cfg['view_id']}/data?{params}"
    return _multipart_csv_upload(url, _headers(cfg, token), csv_path)


def date_range_criteria(date_col: str, start: str, end: str) -> str:
    return f'"{date_col}">=\'{start}\' AND "{date_col}"<=\'{end}\''


def _view_table_name(cfg: dict[str, str]) -> str:
    return _env("ZOHO_TABLE_NAME", default="combined_report_temp")


def run_async_sql_export(
    sql_query: str,
    cfg: dict[str, str] | None = None,
    *,
    response_format: str = "csv",
    poll_seconds: float = 2.0,
    max_polls: int = 45,
) -> str:
    """Run async bulk SQL export and return downloaded text."""
    import time

    cfg = cfg or zoho_config()
    token = get_access_token(cfg)
    headers = _headers(cfg, token)
    ws = cfg["workspace_id"]
    config = {"responseFormat": response_format, "sqlQuery": sql_query}
    params = urllib.parse.urlencode({"CONFIG": json.dumps(config, ensure_ascii=False)})
    create_url = f"{API_URL}/bulk/workspaces/{ws}/data?{params}"
    created = _get_json(create_url, headers)
    job_id = created["data"]["jobId"]

    for _ in range(max_polls):
        time.sleep(poll_seconds)
        status = _get_json(f"{API_URL}/bulk/workspaces/{ws}/exportjobs/{job_id}", headers)
        code = str(status["data"].get("jobCode", ""))
        if code == "1004":
            return _get_text(status["data"]["downloadUrl"], headers)
        if code in {"1003", "1005"}:
            raise RuntimeError(f"Zoho export job failed: {status}")
    raise TimeoutError(f"Zoho export job {job_id} timed out")


def get_max_visit_date(
    date_col: str = "Дата визита",
    *,
    cfg: dict[str, str] | None = None,
) -> date | None:
    """MAX(Дата визита) in Zoho via async SQL export."""
    import pandas as pd

    cfg = cfg or zoho_config()
    table = _view_table_name(cfg)
    sql = f'SELECT MAX("{date_col}") FROM "{table}"'
    csv_text = run_async_sql_export(sql, cfg=cfg)
    csv_text = csv_text.lstrip("\ufeff")
    lines = [ln.strip() for ln in csv_text.splitlines() if ln.strip()]
    if len(lines) < 2:
        return None
    raw = lines[-1].split()[0][:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        mx = pd.to_datetime(lines[-1], format="%Y-%m-%d", errors="coerce")
        if pd.isna(mx):
            return None
        return mx.date()


def get_table_metadata(cfg: dict[str, str] | None = None) -> dict[str, Any]:
    cfg = cfg or zoho_config()
    token = get_access_token(cfg)
    url = f"{API_URL}/workspaces/{cfg['workspace_id']}/views/{cfg['view_id']}/metadata"
    return _get_json(url, _headers(cfg, token))


def exchange_auth_code(code: str, client_id: str, client_secret: str) -> dict[str, Any]:
    """One-time exchange of authorization code from self_client.json."""
    return _post_form(
        ACCOUNTS_URL,
        {
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
        },
    )


def cmd_test(cfg: dict[str, str]) -> int:
    print("Config:")
    for k, v in cfg.items():
        if "secret" in k or "token" in k:
            print(f"  {k}: {'*' * 8}")
        else:
            print(f"  {k}: {v}")
    token = get_access_token(cfg)
    print(f"Access token: OK ({token[:12]}...)")
    meta = get_table_metadata(cfg)
    cols = meta["data"]["columns"]
    print(f"Metadata: {len(cols)} columns in view {cfg['view_id']}")
    print(f"  first: {cols[0]['columnName']}")
    print(f"  last:  {cols[-1]['columnName']}")
    return 0


def cmd_workspaces(cfg: dict[str, str]) -> int:
    data = list_workspaces(cfg)
    for ws in data.get("data", {}).get("ownedWorkspaces", []):
        print(f"{ws['workspaceId']}\t{ws['workspaceName']}\torg={ws['orgId']}")
    return 0


def cmd_metadata(cfg: dict[str, str]) -> int:
    data = get_table_metadata(cfg)
    for col in data["data"]["columns"]:
        print(f"{col['columnIndex']:>3}  {col['columnName']:<40} {col['dataTypeName']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zoho Analytics API")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("test", help="Test OAuth + metadata")
    sub.add_parser("workspaces", help="List workspaces")
    sub.add_parser("metadata", help="Print table column metadata")
    p_exchange = sub.add_parser("exchange-code", help="Exchange self_client code for refresh token")
    p_exchange.add_argument("--file", type=Path, default=Path("self_client.json"))

    args = parser.parse_args(argv)
    cfg = zoho_config()

    try:
        if args.cmd == "exchange-code":
            raw = json.loads(args.file.read_text(encoding="utf-8"))
            result = exchange_auth_code(raw["code"], raw["client_id"], raw["client_secret"])
            print(json.dumps({k: v for k, v in result.items() if k != "access_token"}, indent=2))
            if "refresh_token" in result:
                print("\nAdd to .env:\nZOHO_REFRESH_TOKEN=" + result["refresh_token"])
            return 0
        if args.cmd == "test":
            return cmd_test(cfg)
        if args.cmd == "workspaces":
            return cmd_workspaces(cfg)
        if args.cmd == "metadata":
            return cmd_metadata(cfg)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        print(f"HTTP {e.code}: {body}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
