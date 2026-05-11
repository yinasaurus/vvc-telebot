from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import sheets
from club_catalog import CLUB_GROUPS

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

CRED = config.GOOGLE_SERVICE_ACCOUNT_FILE
SHEET = config.GOOGLE_SHEET_ID

GENERIC_ERROR_TEXT = (
    "Something went wrong on the bot. Please try again. "
    "If it keeps happening, tell logistics so they can check the server logs."
)
SHEET_ERROR_TEXT = (
    "Could not read or write the Google Sheet (network or permissions). "
    "Try again in a moment."
)


class SheetsBackendError(Exception):
    """Raised when a Google Sheets operation fails after logging the real cause."""


async def _sheet(fn: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except RuntimeError as e:
        logger.exception("Spreadsheet configuration error in %s", getattr(fn, "__name__", fn))
        raise SheetsBackendError(str(e) or SHEET_ERROR_TEXT) from e
    except Exception as e:
        logger.exception("Google Sheets API error in %s", getattr(fn, "__name__", fn))
        raise SheetsBackendError(SHEET_ERROR_TEXT) from e


async def _reply_text(update: Update, text: str) -> None:
    """Send a text reply; swallow Telegram delivery errors after logging."""
    try:
        if update.message:
            await update.message.reply_text(text)
        elif update.callback_query and update.callback_query.message:
            await update.callback_query.message.reply_text(text)
        elif update.edited_message:
            await update.edited_message.reply_text(text)
    except TelegramError:
        logger.exception("Could not send error reply to user")


async def _guard_operating_hours(
    update: Update, *, allow_admin: bool = False
) -> bool:
    """Return True when request can proceed; otherwise reply with inactive message."""
    if _is_within_operating_hours():
        return True
    uid = update.effective_user.id if update.effective_user else None
    if allow_admin and uid is not None and _is_admin(uid):
        return True
    await _reply_text(update, _closing_hours_message())
    return False


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    exc = context.error
    if exc:
        logger.error("Unhandled exception in handler", exc_info=exc)
        await _send_ops_alert(context, f"Unhandled exception: {type(exc).__name__}: {exc}")
    if not isinstance(update, Update):
        return
    msg = GENERIC_ERROR_TEXT
    if isinstance(exc, SheetsBackendError):
        msg = str(exc) or SHEET_ERROR_TEXT
    await _reply_text(update, msg)


async def _send_ops_alert(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Send throttled operational alerts to maintainers."""
    bot = context.application.bot
    now_m = time.monotonic()
    last_by_key: dict[str, float] = context.application.bot_data["alert_last_sent"]
    key = text[:120]
    last = last_by_key.get(key, 0.0)
    if now_m - last < config.ALERT_COOLDOWN_SEC:
        return
    last_by_key[key] = now_m
    for uid in config.ALERT_TELEGRAM_IDS:
        try:
            await bot.send_message(chat_id=uid, text=f"[vvc-telebot alert]\n{text}")
        except TelegramError:
            logger.exception("Failed to send alert message to uid=%s", uid)


async def _reply_markdown_safe(
    message: Any, text: str, reply_markup: Any = None
) -> None:
    try:
        await message.reply_text(
            text, parse_mode="Markdown", reply_markup=reply_markup
        )
    except BadRequest:
        logger.info("Markdown rejected; sending plain text")
        try:
            await message.reply_text(text, reply_markup=reply_markup)
        except TelegramError:
            logger.exception("_reply_markdown_safe: plain reply failed")


async def _callback_ack(query: Any) -> None:
    try:
        await query.answer()
    except BadRequest:
        logger.info("callback answer skipped (expired or already answered)")


async def _edit_callback_message(query: Any, text: str, *, parse_mode: str | None = None) -> None:
    try:
        await query.edit_message_text(text, parse_mode=parse_mode)
    except BadRequest:
        try:
            await query.edit_message_text(text)
        except (BadRequest, TelegramError):
            if query.message:
                try:
                    await query.message.reply_text(text)
                except TelegramError:
                    logger.exception("_edit_callback_message: fallback reply failed")

USER_FLOW_GROUP = "await_group"
USER_FLOW_CLUB = "await_club"
USER_FLOW_DESC = "await_need_desc"
USER_FLOW_ACK_NAME = "await_ack_full_name"
USER_FLOW_ACK_CONFIRM = "await_ack_confirm"
USER_FLOW_MYLOANS_CCA = "await_myloans_cca"

FORMAT_HELP = (
    "Please send in this format:\n"
    "item, qty, reason\n\n"
    "Meaning:\n"
    "- item = what you need\n"
    "- qty = how many\n"
    "- reason = why you need it\n\n"
    "Example:\n"
    "HDMI cable, 2, Year-end concert booth"
)

CCA_CANCEL_LABEL = "« Cancel"
CCA_BACK_LABEL = "« Back"
MY_LOANS_ALL_LABEL = "All my CCAs"

# Bottom reply keyboard — short labels read better on phones.
LABEL_HELP = "Help"
LABEL_SEARCH = "Search"
LABEL_PENDING_LOANS = "Pending loans"
LABEL_PENDING_RETURNS = "Pending returns"
_LEGACY_PENDING_LOANS = "Admin: pending loans"
_LEGACY_PENDING_RETURNS = "Admin: pending returns"


def _main_menu_reply_labels() -> frozenset[str]:
    return frozenset(
        {
            "New request",
            "My loans",
            "Edit a request",
            LABEL_HELP,
            "Exit / Cancel",
            LABEL_PENDING_LOANS,
            LABEL_PENDING_RETURNS,
            _LEGACY_PENDING_LOANS,
            _LEGACY_PENDING_RETURNS,
        }
    )


# Reply keyboard labels that abort the current flow (must be checked before parsers / step handlers).
_CANCEL_TEXTS = frozenset({"Exit / Cancel", CCA_CANCEL_LABEL})
# Telegram often delivers two consecutive message updates for a single reply-keyboard tap.
_REPLY_KEYBOARD_DUPLICATE_BURST_SEC = 2.5


def _suppress_duplicate_keyboard_menu_tap(
    context: ContextTypes.DEFAULT_TYPE, uid: int, text: str
) -> bool:
    """Return True to skip handling: same menu label from this user arrived twice in one burst."""
    now_m = time.monotonic()
    last: dict[tuple[int, str], float] = context.application.bot_data.setdefault(
        "reply_keyboard_burst_ts", {}
    )
    k = (uid, text)
    t_prev = last.get(k)
    if t_prev is not None and now_m - t_prev < _REPLY_KEYBOARD_DUPLICATE_BURST_SEC:
        return True
    last[k] = now_m
    return False


def _options_keyboard(options: list[str], *, include_back: bool = False) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = []
    for i in range(0, len(options), 2):
        row = [KeyboardButton(options[i])]
        if i + 1 < len(options):
            row.append(KeyboardButton(options[i + 1]))
        rows.append(row)
    if include_back:
        rows.append([KeyboardButton(CCA_BACK_LABEL)])
    rows.append([KeyboardButton(CCA_CANCEL_LABEL)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _fmt_need_row(t: dict[str, str]) -> str:
    i, q, r = (
        t.get("need_item", "").strip(),
        t.get("need_qty", "").strip(),
        t.get("need_reason", "").strip(),
    )
    if i or q or r:
        return f"Need: {i} × {q} — {r}"
    return "Need: (empty)"


def _fmt_loan_row(t: dict[str, str]) -> str:
    i, q, r = (
        t.get("loan_item", "").strip(),
        t.get("loan_qty", "").strip(),
        t.get("loan_reason", "").strip(),
    )
    if i or q or r:
        return f"Loaned: {i} × {q} — {r}"
    return "Loaned: (pending)"


def _status_chip(status: str) -> str:
    mapping = {
        sheets.STATUS_PENDING_ADMIN: "🟡 pending admin",
        sheets.STATUS_AWAITING_ACK: "🟠 awaiting signature",
        sheets.STATUS_ON_LOAN: "🟢 on loan",
        sheets.STATUS_PENDING_RETURN: "🔵 pending return",
        sheets.STATUS_RETURNED: "✅ returned",
        sheets.STATUS_CANCELLED: "⚪ cancelled",
    }
    return mapping.get(status, status or "unknown")


_FIND_MAX_RESULTS = 22
_FIND_REPLY_SOFT_CHARS = 3600


def _find_usage_text() -> str:
    return (
        "Admin search — look up rows here instead of scrolling the Sheet.\n\n"
        "/find tg <telegram_user_id>\n"
        "/find id <transaction_id_prefix>\n"
        "/find club <text> — CCA / club line contains this text\n"
        "/find keyword <text> — item, qty, reasons, borrower name/username, ids\n"
        "/find status <status> — exact status (e.g. pending_admin, on_loan)\n\n"
        "Examples: /find club badminton   /find tg 5912345678\n\n"
        "Shortcut: tap Search on your admin keyboard anytime.\n"
        "Tip: /status <full_transaction_id> for full detail on one row."
    )


def _truncate_find_field(s: str, max_len: int) -> str:
    line = " ".join((s or "").split())
    return line if len(line) <= max_len else line[: max_len - 1] + "…"


def _format_find_hit(t: dict[str, str]) -> str:
    full_id = (t.get("id") or "").strip()
    sid = full_id[:12] + "…" if len(full_id) > 12 else full_id or "?"
    cca = _truncate_find_field(t.get("cca") or "", 42)
    need_bit = ", ".join(
        p
        for p in (
            (t.get("need_item") or "").strip(),
            (t.get("need_qty") or "").strip(),
        )
        if p
    )
    need_bit = _truncate_find_field(need_bit, 48)
    un = (t.get("requester_username") or "").strip()
    who = f"@{un}" if un else f"id {t.get('requester_tg_id', '?')}"
    return f"{sid} | {_status_chip(t.get('status',''))} | {who} | {cca} | {need_bit}"


def _sort_tx_by_recent(txs: list[dict[str, str]]) -> list[dict[str, str]]:
    def rk(rec: dict[str, str]) -> str:
        return rec.get("updated_at") or rec.get("created_at") or ""

    return sorted(txs, key=rk, reverse=True)


async def _reply_find_chunks(
    msg: Any, header: str, lines: list[str]
) -> None:
    """Send header + numbered lines split under Telegram limits."""
    if not lines:
        await msg.reply_text(header + "\n\nNo matching rows.")
        return
    chunks: list[str] = []
    cur = header + "\n\n"
    for ln in lines:
        addon = ln + "\n"
        if len(cur) + len(addon) > _FIND_REPLY_SOFT_CHARS:
            chunks.append(cur.rstrip())
            cur = addon
        else:
            cur += addon
    if cur.strip():
        chunks.append(cur.rstrip())
    for part in chunks:
        await msg.reply_text(part)

_SESSION_TTL_SEC = max(60, config.SESSION_TTL_MINUTES * 60)
_MAX_BATCH_LINES = 40
_RATE_LIMIT_WINDOW_SEC = 15
_RATE_LIMIT_MAX_MSG = 12
_ACTIVE_OUTSTANDING_STATUSES = {
    sheets.STATUS_PENDING_ADMIN,
    sheets.STATUS_AWAITING_ACK,
    sheets.STATUS_ON_LOAN,
    sheets.STATUS_PENDING_RETURN,
}


def _operating_hours_text() -> str:
    return (
        f"{config.OPENING_HOUR_24:02d}:00 to {config.CLOSING_HOUR_24:02d}:00 "
        "UTC+8 daily"
    )


def _is_within_operating_hours() -> bool:
    if not config.OPERATING_HOURS_ENABLED:
        return True
    now_sgt = datetime.now(timezone(timedelta(hours=8)))
    h = now_sgt.hour
    open_h = config.OPENING_HOUR_24
    close_h = config.CLOSING_HOUR_24
    if open_h < close_h:
        return open_h <= h < close_h
    # Overnight window (for example 21 -> 6)
    return h >= open_h or h < close_h


def _closing_hours_message() -> str:
    return (
        "The bot is currently inactive (outside operating hours).\n\n"
        f"Operating hours: {_operating_hours_text()}\n"
        "Please come back during those hours."
    )


def parse_three_csv_fields(text: str) -> tuple[str, str, str] | None:
    """Split on first two commas so `reason` may contain commas."""
    parts = [p.strip() for p in text.strip().split(",", 2)]
    if len(parts) != 3:
        return None
    item, qty, reason = parts
    if not item or not qty or not reason:
        return None
    return item, qty, reason


def parse_three_fields_line(text: str) -> tuple[str, str, str] | None:
    """Parse one line as CSV (`a,b,c`) or TSV (`a<TAB>b<TAB>c`)."""
    line = text.strip()
    if not line:
        return None
    if "\t" in line:
        parts = [p.strip() for p in line.split("\t", 2)]
        if len(parts) != 3:
            return None
        item, qty, reason = parts
        if not item or not qty or not reason:
            return None
        return item, qty, reason
    return parse_three_csv_fields(line)


def parse_batch_lines(text: str) -> tuple[list[tuple[str, str, str]], int | None]:
    """
    Parse one or many lines.
    Returns (parsed_rows, bad_line_number). bad_line_number is 1-based.
    """
    rows: list[tuple[str, str, str]] = []
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) > _MAX_BATCH_LINES:
        return [], -1
    for i, ln in enumerate(lines, start=1):
        parsed = parse_three_fields_line(ln)
        if not parsed:
            return [], i
        rows.append(parsed)
    if not rows:
        return [], 1
    return rows, None


def _is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_TELEGRAM_IDS


def _resolve_tx_id_hex_prefix(
    token: str, rows: list[dict[str, str]], *, none_msg: str, multi_prefix: str
) -> tuple[str | None, str]:
    """Match full id or a unique lowercase hex prefix among rows that have ``id``."""
    tkn = token.strip().lower().replace("-", "")
    if not tkn:
        return None, ""
    if len(tkn) < 6:
        return None, "Paste at least 6 characters of the id (from the Sheet or borrower message)."
    if any(c not in "0123456789abcdef" for c in tkn):
        return None, "Transaction id should be hex digits (and optional dashes), from the `id` column."
    ids = [t["id"] for t in rows if t.get("id")]
    for tid in ids:
        if tid.lower().replace("-", "") == tkn:
            return tid, ""
    matches = [t for t in rows if t.get("id", "").lower().replace("-", "").startswith(tkn)]
    if not matches:
        return None, none_msg
    if len(matches) > 1:
        bits = [f"{m['id'][:12]}…" for m in matches[:5]]
        extra = "" if len(matches) <= 5 else f" (+{len(matches) - 5} more)"
        return None, multi_prefix + "\n" + "\n".join(bits) + extra
    return matches[0]["id"], ""


def _resolve_pending_admin_tx_id(token: str, pending: list[dict[str, str]]) -> tuple[str | None, str]:
    """Match full id or a unique lowercase hex prefix among pending_admin rows."""
    return _resolve_tx_id_hex_prefix(
        token,
        pending,
        none_msg=(
            "No pending_admin row matches that id. Check the Sheet id column, "
            "or open Pending loans."
        ),
        multi_prefix="Several pending requests match that prefix. Paste more of the hex id:",
    )


def _verified(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    sessions: dict[int, float] = context.application.bot_data["unlocked_until"]
    now_m = time.monotonic()
    exp = sessions.get(user_id)
    if exp is None or now_m >= exp:
        sessions.pop(user_id, None)
        return False
    # Sliding session: active users stay unlocked while chatting.
    sessions[user_id] = now_m + _SESSION_TTL_SEC
    return True


def _unlock_session(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> None:
    sessions: dict[int, float] = context.application.bot_data["unlocked_until"]
    sessions[user_id] = time.monotonic() + _SESSION_TTL_SEC


def _rate_limited(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    now_m = time.monotonic()
    by_user: dict[int, list[float]] = context.application.bot_data["user_msg_times"]
    recent = [t for t in by_user.get(user_id, []) if now_m - t <= _RATE_LIMIT_WINDOW_SEC]
    recent.append(now_m)
    by_user[user_id] = recent
    return len(recent) > _RATE_LIMIT_MAX_MSG


async def _admin_audit(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    action: str,
    admin_user: Any,
    tx_id: str = "",
    notes: str = "",
) -> None:
    """Best-effort admin audit logging; should never block core flow."""
    try:
        await _sheet(
            sheets.append_admin_audit,
            CRED,
            SHEET,
            action=action,
            admin_tg_id=admin_user.id,
            admin_username=admin_user.username or "",
            admin_display_name=admin_user.full_name or "",
            tx_id=tx_id,
            notes=notes,
        )
    except SheetsBackendError:
        logger.warning("Admin audit write failed for action=%s tx_id=%s", action, tx_id)


async def _maybe_dm_requester(bot: Any, requester_tg_id: str | None, text: str) -> bool:
    """Notify borrower in private chat. False if Telegram blocked delivery (often user never pressed /start)."""
    rid = (requester_tg_id or "").strip()
    if not rid.isdigit():
        return False
    try:
        await bot.send_message(chat_id=int(rid), text=text)
        return True
    except TelegramError:
        logger.warning("Borrower DM failed for tg_id=%s", rid[:6])
        return False


async def _approve_pending_loan_as_requested(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    tx_id: str,
    admin_user: Any,
) -> tuple[bool, str, bool]:
    """Apply sheet update, audit, borrower DM. Returns (sheet_ok, admin_error_hint, borrower_dm_sent)."""
    try:
        t = await _sheet(sheets.find_transaction, CRED, SHEET, tx_id)
    except SheetsBackendError:
        return False, SHEET_ERROR_TEXT, False
    if not t:
        return False, "Transaction not found.", False
    if t.get("status") != sheets.STATUS_PENDING_ADMIN:
        return False, f"Not waiting for logistics (status is {t.get('status','')}).", False
    now = sheets.now_iso()
    ni, nq, nr = (
        (t.get("need_item") or "").strip(),
        (t.get("need_qty") or "").strip(),
        (t.get("need_reason") or "").strip(),
    )
    try:
        ok = await _sheet(
            sheets.update_transaction,
            CRED,
            SHEET,
            tx_id,
            {
                "loan_item": ni,
                "loan_qty": nq,
                "loan_reason": nr,
                "admin_tg_id": str(admin_user.id),
                "admin_username": admin_user.username or "",
                "loan_recorded_at": now,
                "status": sheets.STATUS_AWAITING_ACK,
            },
        )
    except SheetsBackendError:
        return False, SHEET_ERROR_TEXT, False
    if not ok:
        return False, "Could not update the sheet.", False
    await _admin_audit(
        context,
        action="loan_approved_as_requested",
        admin_user=admin_user,
        tx_id=tx_id,
        notes="Loan mirrors request; borrower notified",
    )
    cca = (t.get("cca") or "").strip() or "(CCA not set)"
    need_one = _fmt_need_row(t)
    short = tx_id[:10]
    body = (
        "✅ Your loan request was approved.\n\n"
        f"{need_one}\n"
        f"CCA: {cca}\n\n"
        "Please open this bot, tap My loans, then Sign / acknowledge. "
        "Enter your full name and type CONFIRM — that confirms you received the gear and locks the loan on our records.\n\n"
        f"Reference id: {short}…"
    )
    dm_ok = await _maybe_dm_requester(context.bot, t.get("requester_tg_id"), body)
    return True, "", dm_ok


async def _reject_pending_loan_admin(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    tx_id: str,
    admin_user: Any,
) -> tuple[bool, str, bool]:
    """Mark cancelled, audit, notify borrower."""
    try:
        t = await _sheet(sheets.find_transaction, CRED, SHEET, tx_id)
    except SheetsBackendError:
        return False, SHEET_ERROR_TEXT, False
    if not t:
        return False, "Transaction not found.", False
    if t.get("status") != sheets.STATUS_PENDING_ADMIN:
        return False, f"Not waiting for logistics (status is {t.get('status','')}).", False
    try:
        ok = await _sheet(
            sheets.update_transaction,
            CRED,
            SHEET,
            tx_id,
            {
                "status": sheets.STATUS_CANCELLED,
                "admin_tg_id": str(admin_user.id),
                "admin_username": admin_user.username or "",
            },
        )
    except SheetsBackendError:
        return False, SHEET_ERROR_TEXT, False
    if not ok:
        return False, "Could not update the sheet.", False
    await _admin_audit(
        context,
        action="loan_request_rejected",
        admin_user=admin_user,
        tx_id=tx_id,
        notes="Marked cancelled; borrower notified",
    )
    cca = (t.get("cca") or "").strip() or "(CCA not set)"
    body = (
        "❌ Your loan request was not approved by logistics.\n\n"
        f"{_fmt_need_row(t)}\n"
        f"CCA: {cca}\n\n"
        "If you still need equipment, check with logistics or submit a new request when appropriate.\n\n"
        f"Reference id: {tx_id[:10]}…"
    )
    dm_ok = await _maybe_dm_requester(context.bot, t.get("requester_tg_id"), body)
    return True, "", dm_ok


def _main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = [
        [KeyboardButton("New request"), KeyboardButton("My loans")],
        [KeyboardButton("Edit a request")],
    ]
    if _is_admin(user_id):
        rows.append([KeyboardButton(LABEL_HELP), KeyboardButton(LABEL_SEARCH)])
    else:
        rows.append([KeyboardButton(LABEL_HELP)])
    rows.append([KeyboardButton("Exit / Cancel")])
    if _is_admin(user_id):
        rows.append(
            [KeyboardButton(LABEL_PENDING_LOANS), KeyboardButton(LABEL_PENDING_RETURNS)]
        )
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _signing_name_keyboard() -> ReplyKeyboardMarkup:
    """Shows only Escape during name entry (main menu hides after inline taps on some clients)."""
    return ReplyKeyboardMarkup([[KeyboardButton("Exit / Cancel")]], resize_keyboard=True)


def _signing_confirm_keyboard() -> ReplyKeyboardMarkup:
    """Tappable CONFIRM + Exit instead of relying on hidden main keyboard."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton("CONFIRM")], [KeyboardButton("Exit / Cancel")]],
        resize_keyboard=True,
    )


def _clear_ui_flow_user_data(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop wizard state for borrower/admin input flows."""
    ud = context.user_data
    ud.pop("expect_loan_for", None)
    ud.pop("flow", None)
    ud.pop("pending_group", None)
    ud.pop("pending_cca", None)
    ud.pop("pending_ack_tx", None)
    ud.pop("pending_ack_name", None)
    ud.pop("pending_ack_action", None)
    ud.pop("myloans_cca_options", None)


async def _abort_user_flow(
    update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int
) -> None:
    """Clear in-progress flows and hide the reply keyboard.

    Duplicate Exit/Cancel taps in one burst send only one visible "Cancelled." line,
    but both hide the keyboard. Send /start or /help to show the menu again.
    """
    _clear_ui_flow_user_data(context)
    if not update.message:
        return
    remove = ReplyKeyboardRemove()
    now_m = time.monotonic()
    deb: dict[int, float] = context.application.bot_data.setdefault(
        "cancel_reply_last_ts", {}
    )
    last_verbal = deb.get(uid)
    say_cancelled = (
        last_verbal is None or now_m - last_verbal >= _REPLY_KEYBOARD_DUPLICATE_BURST_SEC
    )
    msg = (
        "Cancelled. Send /start or /help to show the menu again."
        if say_cancelled
        else "\u200c"
    )
    try:
        await update.message.reply_text(msg, reply_markup=remove)
    except TelegramError:
        logger.exception("abort_user_flow: reply failed")
        return
    if say_cancelled:
        deb[uid] = now_m


def _cca_help_sentence() -> str:
    return (
        "When you tap New request, you choose Group then Club from buttons "
        "(no typing, so no typo).\n"
    )


def _help_text(*, include_admin: bool) -> str:
    lines = [
        "EASY GUIDE",
        "",
        "What this bot does",
        "This bot helps you request, sign, and return borrowed equipment.",
        "",
        "Operating hours",
        (
            f"- Active: {_operating_hours_text()}"
            if config.OPERATING_HOURS_ENABLED
            else "- Active: always on (operating-hours check is disabled)"
        ),
        "",
        "Main buttons (bottom of chat)",
        "Help = show this guide",
        "Exit / Cancel = stop current step and hide keyboard",
        "To show keyboard again: send /start or /help",
        "",
        "Useful commands",
        "/start — Refresh intro and keyboard",
        "/reset — If you feel stuck",
        "/help — Same as Help button",
        "/status <tx_id> — Check one request/loan",
        "/return <tx_id> — Start return (after sign-off)",
        "/cancelreq / /editreq <tx_id> — Cancel or redo pending request",
        "",
        "Step-by-step (borrower)",
        "1) Unlock: send shared passcode in this private chat. "
        f"Session unlock expires after about {config.SESSION_TTL_MINUTES} minute(s) of inactivity.",
        "",
        "2) Tap New request.",
        "3) Pick Group, then Club.",
        "4) Send one line per item:",
        "   item, qty, reason",
        "   item = what you need",
        "   qty = how many",
        "   reason = why you need it",
        "   Example: HDMI cable, 2, Year-end concert booth",
        "   You can also paste multiple lines at once (or rows copied from a spreadsheet).",
        "",
        "5) Wait for approval/rejection message.",
        "6) If approved, open My loans and tap Sign / acknowledge.",
        "7) Type your full name, then tap CONFIRM.",
        "8) When returning gear, send /return with the transaction id "
        "from the Sheet log or My loans. Logistics approves under Pending returns.",
        "9) Need to change a pending request? Tap Edit a request.",
        "",
        "Tips",
        "• If format is wrong, bot shows example again.",
        "• Use buttons whenever possible.",
        "• Stuck menus? Tap Exit / Cancel, or send /reset, then Help.",
    ]
    if include_admin:
        lines.extend(
            [
                "",
                "— Logistics (you are an admin) —",
                "• Pending loans — Approve / Reject; borrower is messaged either way.",
                "• /recordloan <id> or /rejectloan <id> — same as buttons, using Sheet id.",
                "• Search /find — admins only (by Telegram id, club, keyword…).",
                "• Pending returns — tap when gear is physically back.",
                "• /adminlog — Show latest admin audit entries.",
                "• /pending — Quick counts of pending queues.",
                "• /backupnow — Create a timestamped backup sheet snapshot.",
            ]
        )
    return "\n".join(lines)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if update.effective_chat and update.effective_chat.type != "private":
        try:
            await update.message.reply_text("Use /help in a private chat with this bot.")
        except TelegramError:
            logger.exception("cmd_help: non-private reply failed")
        return
    uid = update.effective_user.id
    verified = _verified(context, uid)
    text = _help_text(include_admin=_is_admin(uid))
    try:
        await update.message.reply_text(
            text,
            reply_markup=_main_keyboard(uid) if verified else None,
        )
    except TelegramError:
        logger.exception("cmd_help: reply failed")


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if update.effective_chat and update.effective_chat.type != "private":
        try:
            await update.message.reply_text("Use /whoami in a private chat with this bot.")
        except TelegramError:
            logger.exception("cmd_whoami: non-private reply failed")
        return
    u = update.effective_user
    uid = u.id
    role = "admin" if _is_admin(uid) else "member"
    unlocked = _verified(context, uid)
    status = "unlocked" if unlocked else "locked (send passcode to unlock)"
    username = f"@{u.username}" if u.username else "(no username)"
    text = (
        "Your bot identity:\n"
        f"- Telegram ID: `{uid}`\n"
        f"- Username: {username}\n"
        f"- Role: {role}\n"
        f"- Session: {status}\n\n"
        "Send this Telegram ID to maintainer if you should be admin."
    )
    if role != "admin":
        text += (
            '\n\nStill seeing "member" after you were added? '
            "Put this numeric id into ADMIN_TELEGRAM_IDS as digits only (not @username), "
            "then restart or redeploy the bot so env changes load."
        )
    try:
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=_main_keyboard(uid) if unlocked else None,
        )
    except TelegramError:
        logger.exception("cmd_whoami: reply failed")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force-clear local flow state and show the right keyboard."""
    if not update.effective_user or not update.message:
        return
    uid = update.effective_user.id
    _clear_ui_flow_user_data(context)
    if _verified(context, uid):
        await update.message.reply_text(
            "State reset. Use the menu below.",
            reply_markup=_main_keyboard(uid),
        )
    else:
        await update.message.reply_text(
            "State reset. Session is locked, send passcode to unlock.",
            reply_markup=ReplyKeyboardRemove(),
        )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not await _guard_operating_hours(update):
        return
    if update.effective_chat and update.effective_chat.type != "private":
        await update.message.reply_text("Use /status in private chat.")
        return
    uid = update.effective_user.id
    if not _verified(context, uid):
        await update.message.reply_text("Session not unlocked. Send passcode first.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /status <transaction_id>")
        return
    tx_id = context.args[0].strip()
    try:
        t = await _sheet(sheets.find_transaction, CRED, SHEET, tx_id)
    except SheetsBackendError:
        await update.message.reply_text(SHEET_ERROR_TEXT)
        return
    if not t:
        await update.message.reply_text("Transaction not found.")
        return
    if not _is_admin(uid) and t.get("requester_tg_id") != str(uid):
        await update.message.reply_text("You can only view your own transactions.")
        return
    msg = (
        f"Status for `{tx_id}`\n"
        f"- Status: {t.get('status','')}\n"
        f"- CCA: {t.get('cca','')}\n"
        f"- Need: {_fmt_need_row(t)}\n"
        f"- Loaned: {_fmt_loan_row(t)}\n"
        f"- Created: {t.get('created_at','')}\n"
        f"- Updated: {t.get('updated_at','')}"
    )
    await _reply_markdown_safe(update.message, msg)


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Search sheet-backed transactions — admins only (full sheet scope)."""
    if not update.effective_user or not update.message:
        return
    if not await _guard_operating_hours(update):
        return
    if update.effective_chat and update.effective_chat.type != "private":
        await update.message.reply_text("Use /find in private chat.")
        return
    uid = update.effective_user.id
    if not _verified(context, uid):
        await update.message.reply_text("Session not unlocked. Send passcode first.")
        return
    if not _is_admin(uid):
        await update.message.reply_text(
            "/find is for logistics admins only.\nYour loans are under My loans; see Help.",
            reply_markup=_main_keyboard(uid),
        )
        return

    parts = list(context.args or [])
    if not parts or parts[0].lower() in ("help", "?"):
        await update.message.reply_text(_find_usage_text())
        return

    mode = parts[0].casefold()
    needle = " ".join(parts[1:]).strip()
    if not needle:
        await update.message.reply_text(
            f"Missing text after `{mode}`.\n\n{_find_usage_text()}"
        )
        return

    try:
        txs = await _sheet(sheets.list_transactions, CRED, SHEET)
    except SheetsBackendError:
        await update.message.reply_text(SHEET_ERROR_TEXT)
        return

    pool = txs

    matched: list[dict[str, str]] = []

    if mode in {"tg", "tele", "user"}:
        digits = "".join(ch for ch in needle if ch.isdigit())
        if not digits:
            await update.message.reply_text(
                "Use digits only for Telegram user id — same number as /whoami for that person."
            )
            return
        matched = [t for t in pool if (t.get("requester_tg_id") or "") == digits]
    elif mode in {"id", "tx"}:
        hx = "".join(ch for ch in needle.lower() if ch in "0123456789abcdef")
        if len(hx) < 4:
            await update.message.reply_text("Use at least 4 hex characters of the transaction id.")
            return
        matched = [
            t
            for t in pool
            if (t.get("id") or "").lower().replace("-", "").startswith(hx)
        ]
    elif mode in {"club", "cca"}:
        nf = needle.casefold()
        matched = [t for t in pool if nf in (t.get("cca") or "").casefold()]
    elif mode in {"keyword", "kw", "q", "text"}:
        nf = needle.casefold()

        def haystack(rec: dict[str, str]) -> str:
            return " ".join(
                [
                    rec.get("cca", ""),
                    rec.get("need_item", ""),
                    rec.get("need_qty", ""),
                    rec.get("need_reason", ""),
                    rec.get("loan_item", ""),
                    rec.get("loan_qty", ""),
                    rec.get("loan_reason", ""),
                    rec.get("requester_username", ""),
                    rec.get("requester_display_name", ""),
                    rec.get("requester_tg_id", ""),
                    rec.get("id", ""),
                    rec.get("status", ""),
                ]
            ).casefold()

        matched = [t for t in pool if nf in haystack(t)]
    elif mode == "status":
        st = needle.strip().lower().replace(" ", "_").replace("-", "_")
        matched = [t for t in pool if (t.get("status") or "").strip().lower() == st]
    else:
        await update.message.reply_text(
            f'Unknown mode "{parts[0]}". Use tg, id, club, keyword, or status.\n\n'
            f"{_find_usage_text()}"
        )
        return

    ordered = _sort_tx_by_recent(matched)
    total = len(ordered)
    shown = ordered[:_FIND_MAX_RESULTS]
    clipped = total > len(shown)

    hdr = f'Find {mode}: "{needle}" — {total} row(s).' + (
        " Showing first " + str(len(shown)) + "." if clipped else ""
    )
    linelist = [_format_find_hit(t) for t in shown]
    await _reply_find_chunks(update.message, hdr, linelist)


async def cmd_adminlog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not await _guard_operating_hours(update):
        return
    uid = update.effective_user.id
    if not _is_admin(uid):
        await update.message.reply_text("Admin only.")
        return
    if not _verified(context, uid):
        await update.message.reply_text("Session not unlocked. Send passcode first.")
        return
    try:
        rows = await _sheet(sheets.list_admin_audit, CRED, SHEET)
    except SheetsBackendError:
        await update.message.reply_text(SHEET_ERROR_TEXT)
        return
    if not rows:
        await update.message.reply_text("No admin audit entries yet.")
        return
    lines = []
    for r in rows[-10:]:
        lines.append(
            f"- {r.get('timestamp','')} | {r.get('action','')} | tx={r.get('tx_id','-')} | @{r.get('admin_username','') or 'no-username'}"
        )
    await update.message.reply_text("Latest admin audit entries:\n" + "\n".join(lines))


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not await _guard_operating_hours(update):
        return
    uid = update.effective_user.id
    if not _is_admin(uid):
        await update.message.reply_text("Admin only.")
        return
    if not _verified(context, uid):
        await update.message.reply_text("Session not unlocked. Send passcode first.")
        return
    try:
        txs = await _sheet(sheets.list_transactions, CRED, SHEET)
    except SheetsBackendError:
        await update.message.reply_text(SHEET_ERROR_TEXT)
        return
    pending_loans = sum(1 for t in txs if t.get("status") == sheets.STATUS_PENDING_ADMIN)
    awaiting_ack = sum(1 for t in txs if t.get("status") == sheets.STATUS_AWAITING_ACK)
    on_loan = sum(1 for t in txs if t.get("status") == sheets.STATUS_ON_LOAN)
    pending_returns = sum(1 for t in txs if t.get("status") == sheets.STATUS_PENDING_RETURN)
    await update.message.reply_text(
        "Queue summary:\n"
        f"- Pending loans: {pending_loans}\n"
        f"- Awaiting signatures: {awaiting_ack}\n"
        f"- Currently on loan: {on_loan}\n"
        f"- Pending returns: {pending_returns}"
    )


async def cmd_recordloan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Approve one pending_admin row by Sheet id (same as Approve in Pending loans)."""
    if not update.effective_user or not update.message:
        return
    if not await _guard_operating_hours(update):
        return
    if update.effective_chat and update.effective_chat.type != "private":
        await update.message.reply_text("Use /recordloan in private chat.")
        return
    uid = update.effective_user.id
    if not _verified(context, uid):
        await update.message.reply_text("Session not unlocked. Send passcode first.")
        return
    if not _is_admin(uid):
        await update.message.reply_text("Admin only.")
        return
    joined = "".join(context.args).strip()
    token = "".join(ch for ch in joined.lower() if ch in "0123456789abcdef")
    if len(token) < 6:
        await update.message.reply_text(
            "Usage: /recordloan <transaction_id>\n\n"
            "Approves the pending request (loan line on the sheet matches what they asked for).\n"
            "Use the id column from the Google Sheet, or enough leading hex digits to match one row."
        )
        return
    try:
        txs = await _sheet(sheets.list_transactions, CRED, SHEET)
    except SheetsBackendError:
        await update.message.reply_text(SHEET_ERROR_TEXT)
        return
    pending = [t for t in txs if t.get("status") == sheets.STATUS_PENDING_ADMIN]
    tx_id, err = _resolve_pending_admin_tx_id(token, pending)
    if not tx_id:
        await update.message.reply_text(err)
        return
    ok, hint, dm_ok = await _approve_pending_loan_as_requested(
        context, tx_id=tx_id, admin_user=update.effective_user
    )
    if not ok:
        await update.message.reply_text(hint)
        return
    msg = (
        f"Approved {tx_id} on file. Status is now awaiting the borrower's acknowledgement.\n\n"
    )
    if dm_ok:
        msg += "They were sent a Telegram message to open My loans and sign."
    else:
        msg += "Could not DM them—ask them to open this bot once and try again, or tell them manually."
    await update.message.reply_text(msg)


async def cmd_rejectloan(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Decline one pending_admin request and notify the borrower."""
    if not update.effective_user or not update.message:
        return
    if not await _guard_operating_hours(update):
        return
    if update.effective_chat and update.effective_chat.type != "private":
        await update.message.reply_text("Use /rejectloan in private chat.")
        return
    uid = update.effective_user.id
    if not _verified(context, uid):
        await update.message.reply_text("Session not unlocked. Send passcode first.")
        return
    if not _is_admin(uid):
        await update.message.reply_text("Admin only.")
        return
    joined = "".join(context.args).strip()
    token = "".join(ch for ch in joined.lower() if ch in "0123456789abcdef")
    if len(token) < 6:
        await update.message.reply_text(
            "Usage: /rejectloan <transaction_id>\n\n"
            "Rejects a pending request and messages the borrower. "
            "Use the Sheet id column or a unique hex prefix (same as /recordloan)."
        )
        return
    try:
        txs = await _sheet(sheets.list_transactions, CRED, SHEET)
    except SheetsBackendError:
        await update.message.reply_text(SHEET_ERROR_TEXT)
        return
    pending = [t for t in txs if t.get("status") == sheets.STATUS_PENDING_ADMIN]
    tx_id, err = _resolve_pending_admin_tx_id(token, pending)
    if not tx_id:
        await update.message.reply_text(err)
        return
    ok, hint, dm_ok = await _reject_pending_loan_admin(
        context, tx_id=tx_id, admin_user=update.effective_user
    )
    if not ok:
        await update.message.reply_text(hint)
        return
    msg = f"Rejected {tx_id} (marked cancelled on the sheet).\n\n"
    if dm_ok:
        msg += "The borrower was messaged."
    else:
        msg += "Could not DM the borrower — they may need to open the bot once first."
    await update.message.reply_text(msg)


async def cmd_return(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Borrower: start return by pasting Sheet id (same update as the old Return button)."""
    if not update.effective_user or not update.message:
        return
    if not await _guard_operating_hours(update):
        return
    if update.effective_chat and update.effective_chat.type != "private":
        await update.message.reply_text("Use /return in private chat.")
        return
    uid = update.effective_user.id
    if not _verified(context, uid):
        await update.message.reply_text("Session not unlocked. Send passcode first.")
        return
    joined = "".join(context.args).strip()
    token = "".join(ch for ch in joined.lower() if ch in "0123456789abcdef")
    if len(token) < 6:
        await update.message.reply_text(
            "Usage: /return <transaction_id>\n\n"
            "Paste the id from the Sheet log or My loans (full id or enough hex digits to match one row). "
            "You still need Sign / acknowledge (name + CONFIRM) first — until then status is not on loan and /return will not apply.\n\n"
            "When you're bringing gear back, logistics closes the loop under Pending returns.",
            reply_markup=_main_keyboard(uid),
        )
        return
    try:
        txs = await _sheet(sheets.list_transactions, CRED, SHEET)
    except SheetsBackendError:
        await update.message.reply_text(SHEET_ERROR_TEXT)
        return
    mine_returnable = [
        t
        for t in txs
        if t.get("requester_tg_id") == str(uid)
        and t.get("status")
        in (sheets.STATUS_ON_LOAN, sheets.STATUS_PENDING_RETURN)
    ]
    tx_id, err = _resolve_tx_id_hex_prefix(
        token,
        mine_returnable,
        none_msg=(
            "No matching loan for you with that id. Use the `id` from the log or My loans. "
            "If it's still awaiting your sign-off, open My loans and Sign first."
        ),
        multi_prefix="Several of your loans match that prefix. Paste more of the hex id:",
    )
    if not tx_id:
        await update.message.reply_text(err, reply_markup=_main_keyboard(uid))
        return
    t = next((r for r in txs if r.get("id") == tx_id), None)
    if not t:
        await update.message.reply_text("Transaction not found.", reply_markup=_main_keyboard(uid))
        return
    if t.get("status") == sheets.STATUS_PENDING_RETURN:
        await update.message.reply_text(
            "That loan is already waiting for logistics to approve the return.",
            reply_markup=_main_keyboard(uid),
        )
        return
    now = sheets.now_iso()
    try:
        ok = await _sheet(
            sheets.update_transaction,
            CRED,
            SHEET,
            tx_id,
            {"return_requested_at": now, "status": sheets.STATUS_PENDING_RETURN},
        )
    except SheetsBackendError:
        await update.message.reply_text(SHEET_ERROR_TEXT)
        return
    if not ok:
        await update.message.reply_text("Could not update the sheet.", reply_markup=_main_keyboard(uid))
        return
    await update.message.reply_text(
        "Return requested. Logistics will approve it in the bot when the item is back.",
        reply_markup=_main_keyboard(uid),
    )


async def cmd_backupnow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    uid = update.effective_user.id
    if not _is_admin(uid):
        await update.message.reply_text("Admin only.")
        return
    if not _verified(context, uid):
        await update.message.reply_text("Session not unlocked. Send passcode first.")
        return
    try:
        name = await _sheet(sheets.backup_main_sheet, CRED, SHEET)
    except SheetsBackendError:
        await update.message.reply_text(SHEET_ERROR_TEXT)
        return
    await _admin_audit(
        context,
        action="backup_created",
        admin_user=update.effective_user,
        notes=f"Created worksheet snapshot: {name}",
    )
    await update.message.reply_text(
        f"Backup created: `{name}`\n"
        "You can find it as a new worksheet tab in the same Google Sheet.",
        parse_mode="Markdown",
    )


async def cmd_cancelreq(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not await _guard_operating_hours(update):
        return
    uid = update.effective_user.id
    if not _verified(context, uid):
        await update.message.reply_text("Session not unlocked. Send passcode first.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /cancelreq <transaction_id>")
        return
    tx_id = context.args[0].strip()
    try:
        t = await _sheet(sheets.find_transaction, CRED, SHEET, tx_id)
    except SheetsBackendError:
        await update.message.reply_text(SHEET_ERROR_TEXT)
        return
    if not t:
        await update.message.reply_text("Transaction not found.")
        return
    if not _is_admin(uid) and t.get("requester_tg_id") != str(uid):
        await update.message.reply_text("You can only cancel your own request.")
        return
    current = t.get("status", "")
    if current in (sheets.STATUS_RETURNED, sheets.STATUS_CANCELLED):
        await update.message.reply_text(f"Cannot cancel from status `{current}`.")
        return
    if current in (sheets.STATUS_ON_LOAN, sheets.STATUS_PENDING_RETURN):
        await update.message.reply_text(
            "Cannot cancel after items are on loan/returning. Use /return when turning gear in."
        )
        return
    updates = {"status": sheets.STATUS_CANCELLED}
    if _is_admin(uid):
        updates["admin_tg_id"] = str(uid)
        updates["admin_username"] = update.effective_user.username or ""
    try:
        ok = await _sheet(sheets.update_transaction, CRED, SHEET, tx_id, updates)
    except SheetsBackendError:
        await update.message.reply_text(SHEET_ERROR_TEXT)
        return
    if not ok:
        await update.message.reply_text("Transaction not found.")
        return
    if _is_admin(uid):
        await _admin_audit(
            context,
            action="request_cancelled",
            admin_user=update.effective_user,
            tx_id=tx_id,
            notes=f"Cancelled from status {current}",
        )
    await update.message.reply_text(f"Cancelled `{tx_id}` from status `{current}`.")


async def cmd_editreq(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not await _guard_operating_hours(update):
        return
    uid = update.effective_user.id
    if not _verified(context, uid):
        await update.message.reply_text("Session not unlocked. Send passcode first.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /editreq <transaction_id>")
        return
    tx_id = context.args[0].strip()
    try:
        t = await _sheet(sheets.find_transaction, CRED, SHEET, tx_id)
    except SheetsBackendError:
        await update.message.reply_text(SHEET_ERROR_TEXT)
        return
    if not t:
        await update.message.reply_text("Transaction not found.")
        return
    if t.get("requester_tg_id") != str(uid):
        await update.message.reply_text("You can only edit your own request.")
        return
    current = t.get("status", "")
    if current not in (sheets.STATUS_PENDING_ADMIN, sheets.STATUS_AWAITING_ACK):
        await update.message.reply_text(
            f"You can only edit from pending_admin / awaiting_user_ack (current: `{current}`)."
        )
        return
    try:
        ok = await _sheet(
            sheets.update_transaction,
            CRED,
            SHEET,
            tx_id,
            {"status": sheets.STATUS_CANCELLED},
        )
    except SheetsBackendError:
        await update.message.reply_text(SHEET_ERROR_TEXT)
        return
    if not ok:
        await update.message.reply_text("Transaction not found.")
        return

    # Start a fresh request flow immediately.
    context.user_data["flow"] = USER_FLOW_GROUP
    context.user_data.pop("pending_group", None)
    context.user_data.pop("pending_cca", None)
    context.user_data.pop("expect_loan_for", None)
    await update.message.reply_text(
        f"Cancelled `{tx_id}` so you can edit. Now pick your Group to create the corrected request.",
        parse_mode="Markdown",
        reply_markup=_options_keyboard(list(CLUB_GROUPS.keys())),
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if update.effective_chat and update.effective_chat.type != "private":
        try:
            await update.message.reply_text("Use this bot in a private chat.")
        except TelegramError:
            logger.exception("cmd_start: non-private reply failed")
        return
    uid = update.effective_user.id
    # Always clear partial flow on /start so reopening chat feels clean.
    context.user_data.pop("flow", None)
    context.user_data.pop("pending_group", None)
    context.user_data.pop("pending_cca", None)
    context.user_data.pop("expect_loan_for", None)
    if _verified(context, uid):
        intro = (
            "You're unlocked.\n\n"
            "Reminder: My loans holds sign / acknowledge and returns.\n"
            "Tap Help for the full guide.\n"
            + (
                f"Operating hours: {_operating_hours_text()}\n"
                if config.OPERATING_HOURS_ENABLED
                else ""
            )
            + (
                "Search (/find) is on your keyboard for logistics.\n"
                if _is_admin(uid)
                else ""
            )
            + "\nUse /reset if a menu feels stuck."
        )
        try:
            await update.message.reply_text(
                intro,
                reply_markup=_main_keyboard(uid),
            )
        except TelegramError:
            logger.exception("cmd_start: reply failed")
        return
    hours_line = (
        f"Operating hours: {_operating_hours_text()}.\n"
        "Outside these hours, the bot auto-replies as inactive.\n\n"
        if config.OPERATING_HOURS_ENABLED
        else ""
    )
    welcome = (
        "Welcome to the loan bot.\n\n"
        "You can use this chat to request and return equipment.\n"
        "Only users with passcode can continue.\n\n"
        + hours_line
        + "Next step:\n"
        "1) Send passcode in this chat.\n"
        "2) Tap New request.\n"
        "3) Follow button steps.\n\n"
        "Important: use this bot in private chat (not groups).\n\n"
        f"Session unlock expires after about {config.SESSION_TTL_MINUTES} minute(s) of inactivity,\n"
        "then passcode is required again.\n\n"
        "Send /help anytime for a full how-to (even before unlocking)."
    )
    try:
        await update.message.reply_text(
            welcome,
            reply_markup=ReplyKeyboardRemove(),
        )
    except TelegramError:
        logger.exception("cmd_start: welcome reply failed")


async def try_passcode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.message.text:
        return
    if update.effective_chat and update.effective_chat.type != "private":
        return
    uid = update.effective_user.id
    if not await _guard_operating_hours(update):
        return
    if _verified(context, uid):
        await handle_user_text(update, context)
        return

    guess = update.message.text.strip()
    try:
        match = secrets.compare_digest(guess, config.BOT_PASSCODE)
    except ValueError:
        match = False
    if not match:
        try:
            await update.message.reply_text(
                "That passcode is not correct.\n\n"
                "Try this:\n"
                "1) Check the latest passcode in Logs.\n"
                "2) Check for typo/caps/extra spaces when pasting.\n"
                "3) If still failing, the bot may be down or passcode may have changed — contact logistics admin."
            )
        except TelegramError:
            logger.exception("try_passcode: unauthorized reply failed")
        return

    _unlock_session(context, uid)
    if _is_admin(uid):
        await _admin_audit(
            context,
            action="admin_unlock",
            admin_user=update.effective_user,
            notes="Admin unlocked session with passcode",
        )
    try:
        await update.message.reply_text(
            "Unlocked. Here's the bottom menu:\n\n"
            "• New request · My loans\n"
            "• Edit a request\n"
            + (
                "• Help · Search (Sheet lookups · admins)\n"
                "• Exit / Cancel\n\n"
                "Admin bottom row: Pending loans · Pending returns\n"
                if _is_admin(uid)
                else "• Help\n"
                     "• Exit / Cancel\n"
            )
            + f"\nIdle ~{config.SESSION_TTL_MINUTES} min — you'll need the passcode again.\n"
            "Questions? Tap Help.",
            reply_markup=_main_keyboard(uid),
        )
    except TelegramError:
        logger.exception("try_passcode: unlock reply failed")


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not await _guard_operating_hours(update):
        return
    if not _is_admin(update.effective_user.id):
        try:
            await update.message.reply_text("Admin only.")
        except TelegramError:
            logger.exception("cmd_admin: reply failed")
        return
    try:
        await update.message.reply_text(
            "Admin — quick reference:\n\n"
            "1) Pending loans — Approve (ok) or Reject (decline). Borrower gets a DM. "
            "/recordloan and /rejectloan work too with Sheet id.\n\n"
            "2) They sign under My loans (name + CONFIRM).\n\n"
            "3) They return by sending /return + Sheet id; Pending returns — approve when gear is back.\n\n"
            "Buttons: Pending loans · Pending returns · Search to find rows."
        )
    except TelegramError:
        logger.exception("cmd_admin: help reply failed")


async def handle_user_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.message.text:
        return
    uid = update.effective_user.id
    if not await _guard_operating_hours(update):
        return
    if not _verified(context, uid):
        return
    if _rate_limited(context, uid):
        try:
            await update.message.reply_text(
                "Too many messages too quickly. Please wait a few seconds and try again."
            )
        except TelegramError:
            logger.exception("handle_user_text: rate-limit reply failed")
        return
    text = update.message.text.strip()
    u = update.effective_user

    if text in _CANCEL_TEXTS:
        await _abort_user_flow(update, context, uid)
        return

    flow = context.user_data.get("flow")
    if flow == USER_FLOW_MYLOANS_CCA:
        options: list[str] = context.user_data.get("myloans_cca_options", [])
        if text not in options:
            await update.message.reply_text(
                "Pick one CCA option above, or tap « Cancel.",
                reply_markup=_options_keyboard(options),
            )
            return
        context.user_data.pop("flow", None)
        context.user_data.pop("myloans_cca_options", None)
        cca_filter = None if text == MY_LOANS_ALL_LABEL else text
        await show_my_loans(update, context, uid, cca_filter=cca_filter)
        return

    flow = context.user_data.get("flow")
    if flow == USER_FLOW_ACK_NAME:
        if len(text) < 3:
            await update.message.reply_text(
                "Please enter your full name (at least 3 characters).\n"
                "(Use Exit / Cancel on the keyboard below if you want to stop.)",
                reply_markup=_signing_name_keyboard(),
            )
            return
        context.user_data["pending_ack_name"] = text
        context.user_data["flow"] = USER_FLOW_ACK_CONFIRM
        await update.message.reply_text(
            f"Captured name: {text}\n\n"
            "If that's wrong, tap Exit / Cancel and start Sign again from My loans.\n\n"
            "Otherwise tap CONFIRM below to finish signing.",
            reply_markup=_signing_confirm_keyboard(),
        )
        return
    if flow == USER_FLOW_ACK_CONFIRM:
        if text != "CONFIRM":
            await update.message.reply_text(
                "Tap CONFIRM below to finish, or Exit / Cancel to stop.",
                reply_markup=_signing_confirm_keyboard(),
            )
            return
        tx_id = context.user_data.get("pending_ack_tx", "")
        action = context.user_data.get("pending_ack_action", "ack")
        full_name = context.user_data.get("pending_ack_name", "")
        if not tx_id:
            context.user_data.pop("flow", None)
            await update.message.reply_text(
                "Signing session expired. Open My loans and tap Sign / acknowledge again.",
                reply_markup=_main_keyboard(uid),
            )
            return
        try:
            t = await _sheet(sheets.find_transaction, CRED, SHEET, tx_id)
        except SheetsBackendError:
            await update.message.reply_text(
                SHEET_ERROR_TEXT,
                reply_markup=_main_keyboard(uid),
            )
            return
        if not t or t.get("requester_tg_id") != str(uid):
            await update.message.reply_text(
                "Not found or not yours.",
                reply_markup=_main_keyboard(uid),
            )
            return
        now = sheets.now_iso()
        try:
            if action == "ack":
                if t.get("status") != sheets.STATUS_AWAITING_ACK:
                    await update.message.reply_text(
                        "This item is no longer waiting for acknowledgement.",
                        reply_markup=_main_keyboard(uid),
                    )
                    return
                await _sheet(
                    sheets.update_transaction,
                    CRED,
                    SHEET,
                    tx_id,
                    {
                        "user_ack_at": now,
                        "ack_full_name": full_name,
                        "ack_method": "full_name+confirm",
                        "status": sheets.STATUS_ON_LOAN,
                    },
                )
            else:
                await update.message.reply_text(
                    "Unknown confirmation action. Please retry.",
                    reply_markup=_main_keyboard(uid),
                )
                return
        except SheetsBackendError:
            await update.message.reply_text(
                SHEET_ERROR_TEXT,
                reply_markup=_main_keyboard(uid),
            )
            return
        context.user_data.pop("flow", None)
        context.user_data.pop("pending_ack_tx", None)
        context.user_data.pop("pending_ack_name", None)
        context.user_data.pop("pending_ack_action", None)
        await update.message.reply_text(
            f"Signed by {full_name}. Acknowledgement saved and status is now on loan.",
            reply_markup=_main_keyboard(uid),
        )
        return

    menu_labels = _main_menu_reply_labels()
    if text in menu_labels:
        if _suppress_duplicate_keyboard_menu_tap(context, uid, text):
            return
        context.user_data.pop("expect_loan_for", None)
        context.user_data.pop("flow", None)
        context.user_data.pop("pending_group", None)
        context.user_data.pop("pending_cca", None)

    if text == "New request":
        context.user_data["flow"] = USER_FLOW_GROUP
        try:
            await update.message.reply_text(
                "Step 1/3: Pick your Group.",
                reply_markup=_options_keyboard(list(CLUB_GROUPS.keys())),
            )
        except TelegramError:
            logger.exception("handle_user_text: New request group keyboard failed")
        return
    if text == "My loans":
        try:
            txs = await _sheet(sheets.list_transactions, CRED, SHEET)
        except SheetsBackendError:
            await update.message.reply_text(SHEET_ERROR_TEXT)
            return
        mine = [
            t for t in txs
            if t.get("requester_tg_id") == str(uid)
            and t.get("status") in (
                sheets.STATUS_AWAITING_ACK,
                sheets.STATUS_ON_LOAN,
                sheets.STATUS_PENDING_RETURN,
            )
        ]
        ccas = sorted({(t.get("cca") or "").strip() for t in mine if (t.get("cca") or "").strip()})
        if len(ccas) <= 1:
            await show_my_loans(update, context, uid)
            return
        options = [MY_LOANS_ALL_LABEL, *ccas]
        context.user_data["flow"] = USER_FLOW_MYLOANS_CCA
        context.user_data["myloans_cca_options"] = options
        await update.message.reply_text(
            "Choose which CCA loans to view:",
            reply_markup=_options_keyboard(options),
        )
        return
    if text == "Edit a request":
        await show_edit_choices(update, context, uid)
        return
    if text == LABEL_HELP:
        await cmd_help(update, context)
        return
    if text == LABEL_SEARCH:
        if not _is_admin(uid):
            await update.message.reply_text(
                "Search (/find) is only for logistics admins.\n"
                "Use My loans for your borrowing, Help for instructions.",
                reply_markup=_main_keyboard(uid),
            )
            return
        if _suppress_duplicate_keyboard_menu_tap(context, uid, text):
            return
        _clear_ui_flow_user_data(context)
        await update.message.reply_text(
            _find_usage_text(),
            reply_markup=_main_keyboard(uid),
        )
        return
    if text in (LABEL_PENDING_LOANS, _LEGACY_PENDING_LOANS):
        await admin_pending_loans(update, context)
        return
    if text in (LABEL_PENDING_RETURNS, _LEGACY_PENDING_RETURNS):
        await admin_pending_returns(update, context)
        return

    flow = context.user_data.get("flow")
    if flow == USER_FLOW_GROUP:
        if text not in CLUB_GROUPS:
            try:
                await update.message.reply_text(
                    "Pick one Group button above, or tap « Cancel.",
                    reply_markup=_options_keyboard(list(CLUB_GROUPS.keys())),
                )
            except TelegramError:
                logger.exception("handle_user_text: invalid group pick reply failed")
            return
        context.user_data["pending_group"] = text
        context.user_data["flow"] = USER_FLOW_CLUB
        try:
            await update.message.reply_text(
                f"Step 2/3: Group selected: {text}\nNow pick your Club.",
                reply_markup=_options_keyboard(list(CLUB_GROUPS[text]), include_back=True),
            )
        except TelegramError:
            logger.exception("handle_user_text: flow group reply failed")
        return

    if flow == USER_FLOW_CLUB:
        if text == CCA_BACK_LABEL:
            context.user_data["flow"] = USER_FLOW_GROUP
            context.user_data.pop("pending_group", None)
            try:
                await update.message.reply_text(
                    "Pick your Group.",
                    reply_markup=_options_keyboard(list(CLUB_GROUPS.keys())),
                )
            except TelegramError:
                logger.exception("handle_user_text: flow back to group failed")
            return
        group = context.user_data.get("pending_group", "")
        clubs = CLUB_GROUPS.get(group, ())
        if text not in clubs:
            try:
                await update.message.reply_text(
                    "Pick one Club button above, or tap « Back / « Cancel.",
                    reply_markup=_options_keyboard(list(clubs), include_back=True),
                )
            except TelegramError:
                logger.exception("handle_user_text: invalid club pick reply failed")
            return
        context.user_data["pending_cca"] = text
        context.user_data["flow"] = USER_FLOW_DESC
        try:
            await update.message.reply_text(
                f"Step 3/3: Enter what you need.\n{FORMAT_HELP}",
                reply_markup=_main_keyboard(uid),
            )
        except TelegramError:
            logger.exception("handle_user_text: flow CCA reply failed")
        return
    if flow == USER_FLOW_DESC:
        rows, bad_line = parse_batch_lines(text)
        if bad_line is not None:
            if bad_line == -1:
                try:
                    await update.message.reply_text(
                        f"Too many lines in one batch (max {_MAX_BATCH_LINES}). "
                        "Split into smaller chunks and send again."
                    )
                except TelegramError:
                    logger.exception("handle_user_text: batch size reply failed")
                return
            try:
                await update.message.reply_text(
                    f"Line {bad_line} is not in the correct format.\n\n"
                    f"{FORMAT_HELP}\n\n"
                    "You can send one item, or many lines at once.\n"
                    "If needed, tap New request to restart."
                )
            except TelegramError:
                logger.exception("handle_user_text: format error reply failed")
            return
        group = context.user_data.get("pending_group", "")
        club = context.user_data.get("pending_cca", "")
        cca = f"{group} / {club}".strip(" /")

        # Enforce per-CCA outstanding quantity limits from `limits` worksheet.
        try:
            limits = await _sheet(sheets.get_item_limits, CRED, SHEET)
            aliases = await _sheet(sheets.get_item_aliases, CRED, SHEET)
            txs = await _sheet(sheets.list_transactions, CRED, SHEET)
        except SheetsBackendError:
            try:
                await update.message.reply_text(SHEET_ERROR_TEXT)
            except TelegramError:
                logger.exception("handle_user_text: limits read error reply failed")
            return

        current_by_item: dict[str, float] = {}
        for t in txs:
            if t.get("cca") != cca:
                continue
            if t.get("status") not in _ACTIVE_OUTSTANDING_STATUSES:
                continue
            key = sheets.canonical_item_key(t.get("need_item", ""), aliases)
            if not key:
                continue
            current_by_item[key] = current_by_item.get(key, 0.0) + sheets.parse_qty_number(
                t.get("need_qty", "")
            )

        # Validate whole batch before writing anything.
        projected = dict(current_by_item)
        for item, qty, _reason in rows:
            key = sheets.canonical_item_key(item, aliases)
            if not key:
                continue
            if key not in limits:
                continue
            req_qty = sheets.parse_qty_number(qty)
            if req_qty <= 0:
                try:
                    await update.message.reply_text(
                        f"Limit exists for `{item}`, so quantity must be numeric and > 0.\n"
                        "Example: table, 1, event use"
                    )
                except TelegramError:
                    logger.exception("handle_user_text: non-numeric limit qty reply failed")
                return
            allowed = limits[key]
            new_total = projected.get(key, 0.0) + req_qty
            if new_total > allowed + 1e-9:
                cur = projected.get(key, 0.0)
                cur_txt = str(int(cur)) if float(cur).is_integer() else f"{cur:.2f}"
                req_txt = str(int(req_qty)) if float(req_qty).is_integer() else f"{req_qty:.2f}"
                lim_txt = str(int(allowed)) if float(allowed).is_integer() else f"{allowed:.2f}"
                try:
                    await update.message.reply_text(
                        f"Rejected: `{item}` exceeds outstanding limit for this CCA.\n"
                        f"Current outstanding: {cur_txt}\n"
                        f"Requested now: {req_txt}\n"
                        f"Max allowed: {lim_txt}\n\n"
                        "Please reduce quantity or wait for returns."
                    )
                except TelegramError:
                    logger.exception("handle_user_text: limit exceeded reply failed")
                return
            projected[key] = new_total

        tx_ids: list[str] = []
        for item, qty, reason in rows:
            tx_id = uuid.uuid4().hex
            try:
                await _sheet(
                    sheets.append_request,
                    CRED,
                    SHEET,
                    tx_id=tx_id,
                    requester_tg_id=uid,
                    requester_username=u.username or "",
                    requester_display_name=u.full_name or "",
                    cca=cca,
                    need_item=item,
                    need_qty=qty,
                    need_reason=reason,
                )
            except SheetsBackendError:
                try:
                    if tx_ids:
                        await update.message.reply_text(
                            f"{SHEET_ERROR_TEXT} Saved {len(tx_ids)} item(s) before failure.\n"
                            f"Saved IDs: {', '.join(tx_ids[:10])}"
                        )
                    else:
                        await update.message.reply_text(
                            f"{SHEET_ERROR_TEXT} No item was saved. Try again in a moment."
                        )
                except TelegramError:
                    logger.exception("handle_user_text: append error reply failed")
                return
            tx_ids.append(tx_id)
        context.user_data.pop("pending_cca", None)
        context.user_data.pop("pending_group", None)
        context.user_data.pop("flow", None)
        try:
            if len(tx_ids) == 1:
                await update.message.reply_text(
                    f"Saved request {tx_ids[0]}.\n"
                    "Logistics sees it under Pending loans (Approve / Reject)."
                )
            else:
                shown = ", ".join(tx_ids[:10])
                extra = "" if len(tx_ids) <= 10 else f" (+{len(tx_ids) - 10} more)"
                await update.message.reply_text(
                    f"Saved {len(tx_ids)} requests.\n"
                    f"IDs: {shown}{extra}\n"
                    "Logistics opens Pending loans to approve or reject each."
                )
        except TelegramError:
            logger.exception("handle_user_text: success reply failed")
        return

    try:
        await update.message.reply_text(
            "Not sure what to do? Tap Help below.",
            reply_markup=_main_keyboard(uid),
        )
    except TelegramError:
        logger.exception("handle_user_text: default hint reply failed")


async def show_my_loans(
    update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int, *, cca_filter: str | None = None
) -> None:
    if not update.message:
        return
    try:
        txs = await _sheet(sheets.list_transactions, CRED, SHEET)
    except SheetsBackendError:
        try:
            await update.message.reply_text(SHEET_ERROR_TEXT)
        except TelegramError:
            logger.exception("show_my_loans: sheet error reply failed")
        return
    mine = [
        t
        for t in txs
        if t.get("requester_tg_id") == str(uid)
        and (cca_filter is None or (t.get("cca") or "").strip() == cca_filter)
        and t.get("status")
        in (
            sheets.STATUS_AWAITING_ACK,
            sheets.STATUS_ON_LOAN,
            sheets.STATUS_PENDING_RETURN,
        )
    ]
    if not mine:
        try:
            await update.message.reply_text("You have no active loans to show.")
        except TelegramError:
            logger.exception("show_my_loans: empty reply failed")
        return
    lines = []
    buttons: list[list[InlineKeyboardButton]] = []
    for t in mine[-15:]:
        sid = t["id"]
        st = t.get("status", "")
        lines.append(
            f"• `{sid}` [{_status_chip(st)}]\n  CCA: {t.get('cca','')}\n"
            f"  {_fmt_need_row(t)}\n"
            f"  {_fmt_loan_row(t)}"
        )
        if st == sheets.STATUS_AWAITING_ACK:
            buttons.append(
                [
                    InlineKeyboardButton(
                        f"Sign / acknowledge {sid[:6]}…",
                        callback_data=f"ack:{sid}",
                    )
                ]
            )
    header = (
        "Your loans\n"
        "(🟠 = needs your sign-off — tap Sign, enter name, then type CONFIRM.)\n"
        "🟢 on loan (after you signed) → when returning gear, send /return with the id from below or the Sheet log.\n\n"
    )
    await _reply_markdown_safe(
        update.message,
        header + ("\n\n".join(lines) or "Nothing to show."),
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
    )


async def show_edit_choices(
    update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int
) -> None:
    if not update.message:
        return
    try:
        txs = await _sheet(sheets.list_transactions, CRED, SHEET)
    except SheetsBackendError:
        try:
            await update.message.reply_text(SHEET_ERROR_TEXT)
        except TelegramError:
            logger.exception("show_edit_choices: sheet error reply failed")
        return
    mine = [
        t
        for t in txs
        if t.get("requester_tg_id") == str(uid)
        and t.get("status") == sheets.STATUS_PENDING_ADMIN
    ]
    if not mine:
        try:
            await update.message.reply_text(
                "You have no editable requests (only pending_admin can be edited)."
            )
        except TelegramError:
            logger.exception("show_edit_choices: empty reply failed")
        return
    buttons = [
        [
            InlineKeyboardButton(
                f"Edit {t['id'][:6]}…",
                callback_data=f"edt:{t['id']}",
            )
        ]
        for t in mine[-20:]
    ]
    try:
        await update.message.reply_text(
            "Pick a pending request to edit. The old one will be cancelled, then you'll submit a corrected one.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except TelegramError:
        logger.exception("show_edit_choices: keyboard reply failed")


async def admin_pending_loans(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message or not update.effective_user:
        return
    if not await _guard_operating_hours(update):
        return
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    try:
        txs = await _sheet(sheets.list_transactions, CRED, SHEET)
    except SheetsBackendError:
        try:
            await update.message.reply_text(SHEET_ERROR_TEXT)
        except TelegramError:
            logger.exception("admin_pending_loans: sheet error reply failed")
        return
    pending = [t for t in txs if t.get("status") == sheets.STATUS_PENDING_ADMIN]
    if not pending:
        try:
            await update.message.reply_text(
                "No pending approvals — queue is empty."
            )
        except TelegramError:
            logger.exception("admin_pending_loans: empty reply failed")
        return
    buttons: list[list[InlineKeyboardButton]] = []
    for t in pending[-20:]:
        tid = t.get("id")
        if not tid:
            continue
        buttons.append(
            [
                InlineKeyboardButton(f"Approve {tid[:6]}…", callback_data=f"apv:{tid}"),
                InlineKeyboardButton(f"Reject {tid[:6]}…", callback_data=f"rej:{tid}"),
            ]
        )
    try:
        await update.message.reply_text(
            "Approve = notify borrower → they tap My loans → Sign.\n"
            "Reject = cancel request + DM them.\n\n"
            "Also works: /recordloan <id>, /rejectloan <id>. Search finds rows.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except TelegramError:
        logger.exception("admin_pending_loans: keyboard reply failed")


async def admin_pending_returns(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message or not update.effective_user:
        return
    if not await _guard_operating_hours(update):
        return
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    try:
        txs = await _sheet(sheets.list_transactions, CRED, SHEET)
    except SheetsBackendError:
        try:
            await update.message.reply_text(SHEET_ERROR_TEXT)
        except TelegramError:
            logger.exception("admin_pending_returns: sheet error reply failed")
        return
    pending = [t for t in txs if t.get("status") == sheets.STATUS_PENDING_RETURN]
    if not pending:
        try:
            await update.message.reply_text("No returns waiting for approval.")
        except TelegramError:
            logger.exception("admin_pending_returns: empty reply failed")
        return
    buttons = []
    for t in pending[-20:]:
        sid = t["id"]
        buttons.append(
            [
                InlineKeyboardButton(
                    f"Approve return {sid[:6]}…",
                    callback_data=f"apr:{sid}",
                )
            ]
        )
    try:
        await update.message.reply_text(
            "Gear physically back with you?\nTap Approve return below (logs who/when).",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except TelegramError:
        logger.exception("admin_pending_returns: keyboard reply failed")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data or not update.effective_user:
        return
    if not await _guard_operating_hours(update):
        return
    await _callback_ack(q)
    uid = update.effective_user.id
    if not _verified(context, uid):
        await _edit_callback_message(q, "Session not unlocked.")
        return

    data = q.data
    if data.startswith("apv:"):
        if not _is_admin(uid):
            await _edit_callback_message(q, "Admin only.")
            return
        tx_id = data.split(":", 1)[1]
        ok, err, dm_ok = await _approve_pending_loan_as_requested(
            context, tx_id=tx_id, admin_user=update.effective_user
        )
        if not ok:
            await _edit_callback_message(q, err)
            return
        tail = (
            " Borrower was messaged."
            if dm_ok
            else " Could not DM borrower (they may need to /start the bot once)."
        )
        await _edit_callback_message(q, f"Approved on file.{tail}")
        return

    if data.startswith("rej:"):
        if not _is_admin(uid):
            await _edit_callback_message(q, "Admin only.")
            return
        tx_id = data.split(":", 1)[1]
        ok, err, dm_ok = await _reject_pending_loan_admin(
            context, tx_id=tx_id, admin_user=update.effective_user
        )
        if not ok:
            await _edit_callback_message(q, err)
            return
        tail = (
            " Borrower was messaged." if dm_ok else " Could not DM borrower."
        )
        await _edit_callback_message(q, f"Rejected.{tail}")
        return

    if data.startswith("ack:"):
        tx_id = data.split(":", 1)[1]
        try:
            t = await _sheet(sheets.find_transaction, CRED, SHEET, tx_id)
        except SheetsBackendError:
            await _edit_callback_message(q, SHEET_ERROR_TEXT)
            return
        if not t or t.get("requester_tg_id") != str(uid):
            await _edit_callback_message(q, "Not found or not yours.")
            return
        if t.get("status") != sheets.STATUS_AWAITING_ACK:
            await _edit_callback_message(
                q, "This item is not waiting for your acknowledgement."
            )
            return
        context.user_data["flow"] = USER_FLOW_ACK_NAME
        context.user_data["pending_ack_tx"] = tx_id
        context.user_data["pending_ack_action"] = "ack"
        context.user_data.pop("pending_ack_name", None)
        await _edit_callback_message(
            q,
            "Step 1/2: Type your full name in chat as the next message.\n",
        )
        if q.message:
            try:
                await q.message.reply_text(
                    "Under the typing box Telegram shows a keyboard row:\n"
                    "Exit / Cancel stops signing.\n"
                    "Otherwise send your full name as your next message.",
                    reply_markup=_signing_name_keyboard(),
                )
            except TelegramError:
                logger.exception("on_callback ack: signing keyboard reply failed")
        return

    if data.startswith("edt:"):
        tx_id = data.split(":", 1)[1]
        try:
            t = await _sheet(sheets.find_transaction, CRED, SHEET, tx_id)
        except SheetsBackendError:
            await _edit_callback_message(q, SHEET_ERROR_TEXT)
            return
        if not t or t.get("requester_tg_id") != str(uid):
            await _edit_callback_message(q, "Not found or not yours.")
            return
        cur = t.get("status")
        if cur not in (sheets.STATUS_PENDING_ADMIN, sheets.STATUS_AWAITING_ACK):
            await _edit_callback_message(
                q,
                "Only pending_admin / awaiting_user_ack can be edited. "
                "If it is already on loan, send /return with the Sheet transaction id.",
            )
            return
        try:
            ok = await _sheet(
                sheets.update_transaction,
                CRED,
                SHEET,
                tx_id,
                {"status": sheets.STATUS_CANCELLED},
            )
        except SheetsBackendError:
            await _edit_callback_message(q, SHEET_ERROR_TEXT)
            return
        if not ok:
            await _edit_callback_message(q, "Transaction not found.")
            return
        context.user_data["flow"] = USER_FLOW_GROUP
        context.user_data.pop("pending_group", None)
        context.user_data.pop("pending_cca", None)
        context.user_data.pop("expect_loan_for", None)
        await _edit_callback_message(
            q,
            f"Cancelled `{tx_id}`. Now pick your Group to submit the corrected request.",
            parse_mode="Markdown",
        )
        if q.message:
            try:
                await q.message.reply_text(
                    "Pick your Group:",
                    reply_markup=_options_keyboard(list(CLUB_GROUPS.keys())),
                )
            except TelegramError:
                logger.exception("on_callback edt: group keyboard reply failed")
        return

    if data.startswith("apr:"):
        if not _is_admin(uid):
            await _edit_callback_message(q, "Admin only.")
            return
        tx_id = data.split(":", 1)[1]
        try:
            t = await _sheet(sheets.find_transaction, CRED, SHEET, tx_id)
        except SheetsBackendError:
            await _edit_callback_message(q, SHEET_ERROR_TEXT)
            return
        if not t or t.get("status") != sheets.STATUS_PENDING_RETURN:
            await _edit_callback_message(q, "Not pending return.")
            return
        now = sheets.now_iso()
        u = update.effective_user
        try:
            await _sheet(
                sheets.update_transaction,
                CRED,
                SHEET,
                tx_id,
                {
                    "return_approved_at": now,
                    "return_approver_tg_id": str(uid),
                    "return_approver_username": u.username or "",
                    "status": sheets.STATUS_RETURNED,
                },
            )
        except SheetsBackendError:
            await _edit_callback_message(q, SHEET_ERROR_TEXT)
            return
        await _admin_audit(
            context,
            action="return_approved",
            admin_user=u,
            tx_id=tx_id,
            notes="Approved return; marked transaction as returned",
        )
        await _edit_callback_message(q, "Marked returned. Sheet updated.")
        return


def main() -> None:
    errors = config.validate_config()
    if errors:
        raise SystemExit("Config errors:\n- " + "\n- ".join(errors))

    try:
        sheets.ensure_headers(config.GOOGLE_SERVICE_ACCOUNT_FILE, config.GOOGLE_SHEET_ID)
        sheets.ensure_workbook_extras(config.GOOGLE_SERVICE_ACCOUNT_FILE, config.GOOGLE_SHEET_ID)
    except RuntimeError as e:
        raise SystemExit(str(e)) from e

    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .build()
    )
    app.bot_data["unlocked_until"] = {}
    app.bot_data["user_msg_times"] = {}
    app.bot_data["alert_last_sent"] = {}

    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("find", cmd_find))
    app.add_handler(CommandHandler("cancelreq", cmd_cancelreq))
    app.add_handler(CommandHandler("editreq", cmd_editreq))
    app.add_handler(CommandHandler("return", cmd_return))
    app.add_handler(CommandHandler("adminlog", cmd_adminlog))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("recordloan", cmd_recordloan))
    app.add_handler(CommandHandler("rejectloan", cmd_rejectloan))
    app.add_handler(CommandHandler("backupnow", cmd_backupnow))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            try_passcode,
        ),
    )

    logger.info(
        "Configured %s admin Telegram id(s); /whoami id must match for admin menus.",
        len(config.ADMIN_TELEGRAM_IDS),
    )
    logger.info("Starting bot (long polling). For 24/7, keep this process running on a host.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
