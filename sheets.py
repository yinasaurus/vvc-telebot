"""Synchronous Google Sheets helpers (call from bot via asyncio.to_thread)."""

from __future__ import annotations

import re
from typing import Any
from datetime import datetime, timedelta, timezone

import gspread
import config
from google.oauth2.service_account import Credentials

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# 24 columns A–X, keep in sync with len(HEADERS)
_END_COL = "X"

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
    "ack_full_name",
    "ack_method",
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

# Previous 22-column schema before ack_full_name/ack_method.
LEGACY_HEADERS_V2 = [
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

STATUS_PENDING_ADMIN = "pending_admin"
STATUS_AWAITING_ACK = "awaiting_user_ack"
STATUS_ON_LOAN = "on_loan"
STATUS_PENDING_RETURN = "pending_return"
STATUS_RETURNED = "returned"
STATUS_CANCELLED = "cancelled"
COLLATED_SHEET_NAME = "collated_logs"
COLLATED_HEADERS = ["item", "total_qty", "request_count", "updated_at"]
LIMITS_SHEET_NAME = "limits"
LIMITS_HEADERS = ["item", "max_outstanding_qty"]
ALIASES_SHEET_NAME = "item_aliases"
ALIASES_HEADERS = ["alias", "canonical_item"]
ADMIN_AUDIT_SHEET_NAME = "admin_audit"
ADMIN_AUDIT_HEADERS = [
    "timestamp",
    "action",
    "tx_id",
    "admin_tg_id",
    "admin_username",
    "admin_display_name",
    "notes",
]

# Explains current workflow / commands; created once if missing (not overwritten later).
QUICKREF_SHEET_NAME = "bot_quickref"


def now_iso() -> str:
    sgt = timezone(timedelta(hours=8))
    return datetime.now(sgt).strftime("%Y-%m-%d %H:%M:%S UTC+8")


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
        "",
        "",
        r[14],
        r[15],
        r[16],
        r[17],
    ]


def _migrate_legacy_v2_row(row: list[str]) -> list[str]:
    r = list(row)
    while len(r) < 22:
        r.append("")
    r = r[:22]
    return [
        r[0],
        r[1],
        r[2],
        r[3],
        r[4],
        r[5],
        r[6],
        r[7],
        r[8],
        r[9],
        r[10],
        r[11],
        r[12],
        r[13],
        r[14],
        r[15],
        r[16],
        r[17],
        "",
        "",
        r[18],
        r[19],
        r[20],
        r[21],
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


def _migrate_legacy_v2_sheet(ws: gspread.Worksheet, all_vals: list[list[str]]) -> None:
    new_rows: list[list[str]] = [HEADERS]
    for row in all_vals[1:]:
        if not row or not row[0]:
            continue
        new_rows.append(_migrate_legacy_v2_row(row))
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


def _strip_pad_row(row: list[str], width: int) -> list[str]:
    """Trim/pad header or data rows (Sheets API often drops trailing blanks)."""
    out = [(c or "").strip() for c in row[:width]]
    while len(out) < width:
        out.append("")
    return out[:width]


def _sheet_end_col(col_count: int) -> str:
    """1 → A … 26 → Z … (enough for our small header widths)."""
    if col_count <= 0:
        return "A"
    n = col_count
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _sync_main_header_row(ws: gspread.Worksheet, row1: list[str]) -> None:
    """Write canonical HEADERS when row 1 is short, mistyped spaces, or API-trimmed."""
    w = len(HEADERS)
    if len(row1) < w or any((row1[i] if i < len(row1) else "") != HEADERS[i] for i in range(w)):
        ws.update(f"A1:{_END_COL}1", [HEADERS], value_input_option="USER_ENTERED")


def ensure_headers(credentials_path: str, sheet_id: str) -> None:
    ws = _ws(credentials_path, sheet_id)
    all_vals = ws.get_all_values()
    if not all_vals:
        ws.update(f"A1:{_END_COL}1", [HEADERS], value_input_option="USER_ENTERED")
        return
    row1 = all_vals[0]
    w_main = len(HEADERS)

    if _strip_pad_row(row1, w_main) == HEADERS:
        _sync_main_header_row(ws, row1)
        return

    if _strip_pad_row(row1, len(LEGACY_HEADERS_V1)) == LEGACY_HEADERS_V1:
        _migrate_legacy_sheet(ws, all_vals)
        return

    if _strip_pad_row(row1, len(LEGACY_HEADERS_V2)) == LEGACY_HEADERS_V2:
        _migrate_legacy_v2_sheet(ws, all_vals)
        return

    if len(all_vals) == 1 and not any((c or "").strip() for c in row1):
        ws.update(f"A1:{_END_COL}1", [HEADERS], value_input_option="USER_ENTERED")
        return

    raise RuntimeError(
        "Row 1 of the Google Sheet must be the bot header row. "
        "Expected current columns or legacy 18/22-column layouts (auto-migrated). "
        "Use a fresh sheet or fix row 1."
    )


def ensure_quickref_sheet(credentials_path: str, sheet_id: str) -> None:
    """One-time human-readable workflow tab (skipped if it already exists)."""
    gc = _client(credentials_path)
    sh = gc.open_by_key(sheet_id)
    try:
        sh.worksheet(QUICKREF_SHEET_NAME)
        return
    except gspread.WorksheetNotFound:
        pass
    body = (
        "Loan bot ↔ this spreadsheet",
        "",
        "Main tab (usually Sheet1): row 1 = machine keys — do not rename or reorder.",
        "Column id — use in Telegram for /status, /return (after sign-off), and admin /recordloan /rejectloan.",
        "",
        "Status values (column status): pending_admin · awaiting_user_ack · on_loan · pending_return · returned · cancelled",
        "",
        "Borrower flow: New request → logistics approves → My loans → Sign (name + CONFIRM) → on_loan.",
        "Return: sender types /return <id> in Telegram (same id column); logistics approves under Pending returns.",
        "",
        "Other tabs: collated_logs (totals), limits (optional caps), item_aliases (optional), admin_audit (logistics actions).",
    )
    ws = sh.add_worksheet(title=QUICKREF_SHEET_NAME, rows=40, cols=2)
    col = [[t] for t in body]
    ws.update(
        f"A1:A{len(col)}",
        col,
        value_input_option="USER_ENTERED",
    )


def _ensure_aux_worksheet_headers(sh: Any, name: str, headers: list[str]) -> None:
    """If the tab exists but row 1 is empty or wrong, rewrite the header row only."""
    try:
        ws = sh.worksheet(name)
    except gspread.WorksheetNotFound:
        return
    raw = ws.get_all_values()
    ec = _sheet_end_col(len(headers))
    if not raw:
        ws.update(f"A1:{ec}1", [headers], value_input_option="USER_ENTERED")
        return
    if _strip_pad_row(raw[0], len(headers)) != list(headers):
        ws.update(f"A1:{ec}1", [headers], value_input_option="USER_ENTERED")


def ensure_workbook_extras(credentials_path: str, sheet_id: str) -> None:
    """After the main tab is valid: quickref tab (once) + repair aux tab headers if needed."""
    ensure_quickref_sheet(credentials_path, sheet_id)
    gc = _client(credentials_path)
    sh = gc.open_by_key(sheet_id)
    _ensure_aux_worksheet_headers(sh, LIMITS_SHEET_NAME, LIMITS_HEADERS)
    _ensure_aux_worksheet_headers(sh, ALIASES_SHEET_NAME, ALIASES_HEADERS)
    _ensure_aux_worksheet_headers(sh, ADMIN_AUDIT_SHEET_NAME, ADMIN_AUDIT_HEADERS)


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
        "",  # ack_full_name
        "",  # ack_method
        "",  # return_requested_at
        "",  # return_approved_at
        "",  # return_approver_tg_id
        "",  # return_approver_username
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")
    refresh_collated_logs(credentials_path, sheet_id)


def parse_qty_number(raw: str) -> float:
    m = re.search(r"[-+]?\d*\.?\d+", raw or "")
    if not m:
        return 0.0
    try:
        return float(m.group(0))
    except ValueError:
        return 0.0


def _singularize_word(word: str) -> str:
    """Simple singularization for inventory-style nouns."""
    w = word.strip().casefold()
    if len(w) <= 3:
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith(("ses", "xes", "zes", "ches", "shes")) and len(w) > 4:
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def normalize_item_key(raw: str) -> str:
    """
    Normalize item name for collation:
    - case-insensitive
    - whitespace-normalized
    - basic plural-to-singular conversion per word
    """
    parts = [p for p in re.split(r"\s+", (raw or "").strip()) if p]
    if not parts:
        return ""
    return " ".join(_singularize_word(p) for p in parts)


def get_item_aliases(credentials_path: str, sheet_id: str) -> dict[str, str]:
    """
    Return alias->canonical map using normalized keys.
    Worksheet: item_aliases
    Columns: alias | canonical_item
    """
    gc = _client(credentials_path)
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(ALIASES_SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=ALIASES_SHEET_NAME, rows=200, cols=4)
        ws.update("A1:B1", [ALIASES_HEADERS], value_input_option="USER_ENTERED")
        return {}
    rows = ws.get_all_values()
    if len(rows) < 2:
        return {}
    out: dict[str, str] = {}
    for r in rows[1:]:
        if not r:
            continue
        alias = normalize_item_key(r[0] if len(r) >= 1 else "")
        canonical = normalize_item_key(r[1] if len(r) >= 2 else "")
        if alias and canonical:
            out[alias] = canonical
    return out


def canonical_item_key(raw: str, aliases: dict[str, str] | None = None) -> str:
    base = normalize_item_key(raw)
    if not base:
        return ""
    if aliases and base in aliases:
        return aliases[base]
    return base


def refresh_collated_logs(credentials_path: str, sheet_id: str) -> None:
    txs = list_transactions(credentials_path, sheet_id)
    aliases = get_item_aliases(credentials_path, sheet_id)
    agg: dict[str, dict[str, float | int | str]] = {}
    for t in txs:
        item = (t.get("need_item") or "").strip()
        if not item:
            continue
        key = canonical_item_key(item, aliases)
        rec = agg.setdefault(
            key,
            {"item": item, "total_qty": 0.0, "request_count": 0},
        )
        rec["request_count"] = int(rec["request_count"]) + 1
        rec["total_qty"] = float(rec["total_qty"]) + parse_qty_number(t.get("need_qty", ""))

    rows: list[list[str]] = [COLLATED_HEADERS]
    now = now_iso()
    for rec in sorted(
        agg.values(),
        key=lambda r: (float(r["total_qty"]), int(r["request_count"])),
        reverse=True,
    ):
        qty_val = float(rec["total_qty"])
        qty_text = str(int(qty_val)) if qty_val.is_integer() else f"{qty_val:.2f}"
        rows.append([str(rec["item"]), qty_text, str(rec["request_count"]), now])

    gc = _client(credentials_path)
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(COLLATED_SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=COLLATED_SHEET_NAME, rows=1000, cols=6)
    ws.clear()
    end_row = max(1, len(rows))
    ws.update(f"A1:D{end_row}", rows, value_input_option="USER_ENTERED")


def get_item_limits(credentials_path: str, sheet_id: str) -> dict[str, float]:
    aliases = get_item_aliases(credentials_path, sheet_id)
    """
    Return configurable per-item max outstanding quantity.
    Worksheet: limits
    Columns: item | max_outstanding_qty
    """
    gc = _client(credentials_path)
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(LIMITS_SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=LIMITS_SHEET_NAME, rows=200, cols=4)
        ws.update("A1:B1", [LIMITS_HEADERS], value_input_option="USER_ENTERED")
        return {}
    rows = ws.get_all_values()
    if len(rows) < 2:
        return {}
    out: dict[str, float] = {}
    for r in rows[1:]:
        if not r:
            continue
        item = (r[0] if len(r) >= 1 else "").strip()
        limit_raw = (r[1] if len(r) >= 2 else "").strip()
        key = canonical_item_key(item, aliases)
        limit = parse_qty_number(limit_raw)
        if key and limit > 0:
            out[key] = limit
    return out


def append_admin_audit(
    credentials_path: str,
    sheet_id: str,
    *,
    action: str,
    admin_tg_id: int,
    admin_username: str,
    admin_display_name: str,
    tx_id: str = "",
    notes: str = "",
) -> None:
    gc = _client(credentials_path)
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(ADMIN_AUDIT_SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=ADMIN_AUDIT_SHEET_NAME, rows=1000, cols=8)
        ws.update("A1:G1", [ADMIN_AUDIT_HEADERS], value_input_option="USER_ENTERED")

    row = [
        now_iso(),
        action,
        tx_id,
        str(admin_tg_id),
        admin_username,
        admin_display_name,
        notes,
    ]
    ws.append_row(row, value_input_option="USER_ENTERED")


def backup_main_sheet(credentials_path: str, sheet_id: str) -> str:
    """
    Create a timestamped backup worksheet from the main transaction sheet.
    Returns the new worksheet title.
    """
    ensure_headers(credentials_path, sheet_id)
    gc = _client(credentials_path)
    sh = gc.open_by_key(sheet_id)
    source = sh.sheet1
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
    title = f"backup_{stamp}"
    source.duplicate(new_sheet_name=title)
    return title


def list_admin_audit(credentials_path: str, sheet_id: str) -> list[dict[str, str]]:
    gc = _client(credentials_path)
    sh = gc.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(ADMIN_AUDIT_SHEET_NAME)
    except gspread.WorksheetNotFound:
        return []
    rows = ws.get_all_values()
    if len(rows) < 2:
        return []
    out: list[dict[str, str]] = []
    for r in rows[1:]:
        if not r:
            continue
        padded = r + [""] * max(0, len(ADMIN_AUDIT_HEADERS) - len(r))
        out.append(dict(zip(ADMIN_AUDIT_HEADERS, padded[: len(ADMIN_AUDIT_HEADERS)])))
    return out


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
