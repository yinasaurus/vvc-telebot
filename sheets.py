"""Synchronous Google Sheets helpers (call from bot via asyncio.to_thread)."""

from __future__ import annotations

import re
from datetime import datetime, timezone

import gspread
import config
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# 22 columns A–V, keep in sync with len(HEADERS)
_END_COL = "V"

HEADERS = [
    "id",
    "created_at",
    "updated_at",
    "requester_tg_id",
    "requester_username",
    "requester_display_name",
    "cca",
    "need_item",
    "need_qty",
    "need_reason",
    "status",
    "loan_item",
    "loan_qty",
    "loan_reason",
    "admin_tg_id",
    "admin_username",
    "loan_recorded_at",
    "user_ack_at",
    "return_requested_at",
    "return_approved_at",
    "return_approver_tg_id",
    "return_approver_username",
]

# Previous schema (single need_description + loan_description); auto-migrated on startup.
LEGACY_HEADERS_V1 = [
    "id",
    "created_at",
    "updated_at",
    "requester_tg_id",
    "requester_username",
    "requester_display_name",
    "cca",
    "need_description",
    "status",
    "loan_description",
    "admin_tg_id",
    "admin_username",
    "loan_recorded_at",
    "user_ack_at",
    "return_requested_at",
    "return_approved_at",
    "return_approver_tg_id",
    "return_approver_username",
]

STATUS_PENDING_ADMIN = "pending_admin"
STATUS_AWAITING_ACK = "awaiting_user_ack"
STATUS_ON_LOAN = "on_loan"
STATUS_PENDING_RETURN = "pending_return"
STATUS_RETURNED = "returned"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def split_pipe_triple(s: str) -> tuple[str, str, str]:
    """Parse stored lines like: item | qty 2 | reason text"""
    s = (s or "").strip()
    if not s:
        return "", "", ""
    m = re.match(r"^(.*?) \| qty (.+?) \| (.+)$", s, re.DOTALL)
    if m:
        return m.group(1).strip(), m.group(2).strip(), m.group(3).strip()
    return s, "", ""


def _migrate_legacy_data_row(row: list[str]) -> list[str]:
    r = list(row)
    while len(r) < 18:
        r.append("")
    r = r[:18]
    ni, nq, nr = split_pipe_triple(r[7])
    li, lq, lr = split_pipe_triple(r[9])
    return [
        r[0],
        r[1],
        r[2],
        r[3],
        r[4],
        r[5],
        r[6],
        ni,
        nq,
        nr,
        r[8],
        li,
        lq,
        lr,
        r[10],
        r[11],
        r[12],
        r[13],
        r[14],
        r[15],
        r[16],
        r[17],
    ]


def _migrate_legacy_sheet(ws: gspread.Worksheet, all_vals: list[list[str]]) -> None:
    new_rows: list[list[str]] = [HEADERS]
    for row in all_vals[1:]:
        if not row or not row[0]:
            continue
        new_rows.append(_migrate_legacy_data_row(row))
    ws.clear()
    if new_rows:
        ws.update(
            f"A1:{_END_COL}{len(new_rows)}",
            new_rows,
            value_input_option="USER_ENTERED",
        )


def _client(credentials_path: str) -> gspread.Client:
    if config.SERVICE_ACCOUNT_INFO is not None:
        creds = Credentials.from_service_account_info(
            config.SERVICE_ACCOUNT_INFO, scopes=SCOPES
        )
    else:
        creds = Credentials.from_service_account_file(credentials_path, scopes=SCOPES)
    return gspread.authorize(creds)


def _ws(credentials_path: str, sheet_id: str) -> gspread.Worksheet:
    gc = _client(credentials_path)
    sh = gc.open_by_key(sheet_id)
    return sh.sheet1


def ensure_headers(credentials_path: str, sheet_id: str) -> None:
    ws = _ws(credentials_path, sheet_id)
    all_vals = ws.get_all_values()
    if not all_vals:
        ws.update(f"A1:{_END_COL}1", [HEADERS], value_input_option="USER_ENTERED")
        return
    row1 = all_vals[0]
    if row1 == HEADERS:
        return
    if row1 == LEGACY_HEADERS_V1:
        _migrate_legacy_sheet(ws, all_vals)
        return
    if len(all_vals) == 1 and not any(row1):
        ws.update(f"A1:{_END_COL}1", [HEADERS], value_input_option="USER_ENTERED")
        return
    raise RuntimeError(
        "Row 1 of the Google Sheet must be the bot header row. "
        "Expected current columns or the legacy 18-column layout (auto-migrated). "
        "Use a fresh sheet or fix row 1."
    )


def append_request(
    credentials_path: str,
    sheet_id: str,
    *,
    tx_id: str,
    requester_tg_id: int,
    requester_username: str,
    requester_display_name: str,
    cca: str,
    need_item: str,
    need_qty: str,
    need_reason: str,
) -> None:
    ensure_headers(credentials_path, sheet_id)
    ws = _ws(credentials_path, sheet_id)
    now = now_iso()
    row = [
        tx_id,
        now,
        now,
        str(requester_tg_id),
        requester_username,
        requester_display_name,
        cca,
        need_item,
        need_qty,
        need_reason,
        STATUS_PENDING_ADMIN,
        "",  # loan_item
        "",  # loan_qty
        "",  # loan_reason
        "",  # admin_tg_id
        "",  # admin_username
        "",  # loan_recorded_at
        "",  # user_ack_at
        "",  # return_requested_at
        "",  # return_approved_at
        "",  # return_approver_tg_id
        "",  # return_approver_username
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")


def _row_to_dict(row: list[str]) -> dict[str, str]:
    if len(row) < len(HEADERS):
        row = row + [""] * (len(HEADERS) - len(row))
    return dict(zip(HEADERS, row))


def list_transactions(credentials_path: str, sheet_id: str) -> list[dict[str, str]]:
    ensure_headers(credentials_path, sheet_id)
    ws = _ws(credentials_path, sheet_id)
    rows = ws.get_all_values()
    if len(rows) < 2:
        return []
    out: list[dict[str, str]] = []
    for r in rows[1:]:
        if not r or not r[0]:
            continue
        out.append(_row_to_dict(r))
    return out


def find_transaction(
    credentials_path: str, sheet_id: str, tx_id: str
) -> dict[str, str] | None:
    for t in list_transactions(credentials_path, sheet_id):
        if t.get("id") == tx_id:
            return t
    return None


def _find_row_index(ws: gspread.Worksheet, tx_id: str) -> int | None:
    cells = ws.col_values(1)
    for i, val in enumerate(cells[1:], start=2):
        if val == tx_id:
            return i
    return None


def update_transaction(
    credentials_path: str,
    sheet_id: str,
    tx_id: str,
    updates: dict[str, str],
) -> bool:
    ensure_headers(credentials_path, sheet_id)
    ws = _ws(credentials_path, sheet_id)
    idx = _find_row_index(ws, tx_id)
    if idx is None:
        return False
    current = _row_to_dict(ws.row_values(idx))
    now = now_iso()
    current["updated_at"] = now
    for k, v in updates.items():
        if k in current:
            current[k] = v
    ordered = [current.get(h, "") for h in HEADERS]
    ws.update(
        f"A{idx}:{_END_COL}{idx}",
        [ordered],
        value_input_option="USER_ENTERED",
    )
    return True
