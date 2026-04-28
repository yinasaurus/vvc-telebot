from __future__ import annotations

import asyncio
import logging
import secrets
import time
import uuid
from typing import Any

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
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


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    exc = context.error
    if exc:
        logger.error("Unhandled exception in handler", exc_info=exc)
    if not isinstance(update, Update):
        return
    msg = GENERIC_ERROR_TEXT
    if isinstance(exc, SheetsBackendError):
        msg = str(exc) or SHEET_ERROR_TEXT
    await _reply_text(update, msg)


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

FORMAT_HELP = (
    "Use exactly three parts separated by commas:\n"
    "item, qty, reason\n\n"
    "Example: HDMI cable, 2, Year-end concert booth"
)

CCA_CANCEL_LABEL = "« Cancel"
CCA_BACK_LABEL = "« Back"


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

# Wrong passcode lockout (in-memory; resets on bot restart)
_MAX_PASS_FAILS = 5
_LOCKOUT_SEC = 15 * 60
_pass_failures: dict[int, int] = {}
_pass_lock_until: dict[int, float] = {}
_SESSION_TTL_SEC = max(60, config.SESSION_TTL_MINUTES * 60)
_MAX_BATCH_LINES = 40
_RATE_LIMIT_WINDOW_SEC = 15
_RATE_LIMIT_MAX_MSG = 12


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


def _main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = [
        [KeyboardButton("New request"), KeyboardButton("My loans")],
        [KeyboardButton("Return an item"), KeyboardButton("Edit a request")],
        [KeyboardButton("Exit / Cancel")],
    ]
    if _is_admin(user_id):
        rows.append(
            [
                KeyboardButton("Admin: pending loans"),
                KeyboardButton("Admin: pending returns"),
            ]
        )
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def _cca_help_sentence() -> str:
    return (
        "When you tap New request, you choose Group then Club from buttons "
        "(no typing, so no typo).\n"
    )


def _help_text(*, include_admin: bool) -> str:
    lines = [
        "HOW TO USE THIS BOT",
        "",
        "What it does",
        "Loan requests and returns are logged to a Google Sheet: who asked, what went out, "
        "when both sides confirmed, and when gear came back.",
        "",
        "Commands",
        "/start — Intro and keyboard (after you unlock)",
        "/help — This full guide",
        "/whoami — Show your Telegram ID and role",
        "/status <tx_id> — Show one transaction status",
        "/cancelreq <tx_id> — Cancel your own request if entered by mistake",
        "/editreq <tx_id> — Cancel your pending request and re-enter it",
        "",
        "Borrower workflow",
        "1) Unlock — First time here: send the shared passcode (only in this private chat). "
        f"Session unlock expires after about {config.SESSION_TTL_MINUTES} minute(s) of inactivity.",
        "",
        "2) New request — Send one line per item:",
        "   item, qty, reason",
        "   Example: HDMI cable, 2, Year-end concert booth",
        "   You can also paste multiple lines at once (or rows copied from a spreadsheet).",
        "",
        _cca_help_sentence(),
        "",
        "3) Logistics enters what they actually loaned; then you open My loans.",
        "",
        '4) Tap Sign / acknowledge — that confirms you received the items (your "signature" on the log).',
        "",
        "5) Return an item — When you're bringing gear back; logistics approves to close the loan.",
        "6) Edit a request — pick one pending request to cancel and immediately resubmit.",
        "",
        "Tips",
        "• If the format is wrong, the bot sends the template again.",
        "• Use the buttons at the bottom of the chat — they're faster than typing commands.",
        "• Tap Exit / Cancel any time to leave the current input flow.",
    ]
    if include_admin:
        lines.extend(
            [
                "",
                "— Logistics (you are an admin) —",
                "• Admin: pending loans — Pick a request, then send item, qty, reason for what you handed out.",
                "• Admin: pending returns — Approve when the item is physically back.",
                "• /adminlog — Show latest admin audit entries.",
                "• /pending — Quick counts of pending queues.",
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
    try:
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=_main_keyboard(uid) if unlocked else None,
        )
    except TelegramError:
        logger.exception("cmd_whoami: reply failed")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
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


async def cmd_adminlog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def cmd_cancelreq(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
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
            "Cannot cancel after items are on loan/returning. Use normal return flow."
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
    if current != sheets.STATUS_PENDING_ADMIN:
        await update.message.reply_text(
            "You can only edit while request is still pending admin review "
            f"(current status: `{current}`)."
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
    if _verified(context, uid):
        intro = (
            "You're unlocked — welcome back.\n\n"
            "Quick reminder: New request → logistics records the loan → you Sign / acknowledge "
            "under My loans → Return an item when done.\n\n"
            "Send /help for the full step-by-step guide."
        )
        try:
            await update.message.reply_text(
                intro,
                reply_markup=_main_keyboard(uid),
            )
        except TelegramError:
            logger.exception("cmd_start: reply failed")
        return
    welcome = (
        "Welcome to the loan bot.\n\n"
        "It tracks equipment loans in a shared Google Sheet (requests, approvals, signatures, returns). "
        "Only people with the passcode can use it.\n\n"
        "Next step: send the shared passcode as your next message right here "
        "(private chat only — not in groups).\n\n"
        f"Session unlock expires after about {config.SESSION_TTL_MINUTES} minute(s) of inactivity,\n"
        "then passcode is required again.\n\n"
        "Send /help anytime for a full how-to (even before unlocking)."
    )
    try:
        await update.message.reply_text(welcome)
    except TelegramError:
        logger.exception("cmd_start: welcome reply failed")


async def try_passcode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.message.text:
        return
    if update.effective_chat and update.effective_chat.type != "private":
        return
    uid = update.effective_user.id
    if _verified(context, uid):
        await handle_user_text(update, context)
        return

    now_m = time.monotonic()
    lock_until = _pass_lock_until.get(uid)
    if lock_until is not None and now_m < lock_until:
        mins = max(1, int((lock_until - now_m) // 60) + 1)
        try:
            await update.message.reply_text(
                f"Too many wrong attempts. Try again in about {mins} minute(s)."
            )
        except TelegramError:
            logger.exception("try_passcode: lockout reply failed")
        return
    if lock_until is not None and now_m >= lock_until:
        _pass_lock_until.pop(uid, None)
        _pass_failures.pop(uid, None)

    guess = update.message.text.strip()
    try:
        match = secrets.compare_digest(guess, config.BOT_PASSCODE)
    except ValueError:
        match = False
    if not match:
        n = _pass_failures.get(uid, 0) + 1
        _pass_failures[uid] = n
        try:
            if n >= _MAX_PASS_FAILS:
                _pass_lock_until[uid] = now_m + _LOCKOUT_SEC
                _pass_failures.pop(uid, None)
                await update.message.reply_text(
                    "Too many wrong passcodes. This account is temporarily blocked "
                    f"from trying again for {_LOCKOUT_SEC // 60} minutes."
                )
            else:
                await update.message.reply_text(
                    f"Wrong passcode ({n}/{_MAX_PASS_FAILS} before temporary lockout)."
                )
        except TelegramError:
            logger.exception("try_passcode: unauthorized reply failed")
        return

    _pass_failures.pop(uid, None)
    _pass_lock_until.pop(uid, None)

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
            "Unlocked.\n\n"
            "Use the bottom buttons: New request, My loans, Return an item. "
            "Admins also see Admin: pending loans / pending returns.\n\n"
            f"Session expires after about {config.SESSION_TTL_MINUTES} minute(s) of inactivity.\n"
            "Send /help for how the whole flow works (sheet logging, Sign / acknowledge, returns).",
            reply_markup=_main_keyboard(uid),
        )
    except TelegramError:
        logger.exception("try_passcode: unlock reply failed")


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message:
        return
    if not _is_admin(update.effective_user.id):
        try:
            await update.message.reply_text("Admin only.")
        except TelegramError:
            logger.exception("cmd_admin: reply failed")
        return
    try:
        await update.message.reply_text(
            "Admin — how approvals work:\n\n"
            "1) Pending loans = someone requested gear. You pick a row, then send "
            "item, qty, reason — that records what you actually handed out (approves the loan on our side).\n\n"
            "2) The borrower then opens My loans and taps Sign / acknowledge — that is their "
            '"signature" that they received it.\n\n'
            "3) Pending returns = borrower started a return. Tap Approve return when the "
            "physical item is back.\n\n"
            "Buttons: Admin: pending loans / Admin: pending returns"
        )
    except TelegramError:
        logger.exception("cmd_admin: help reply failed")


async def handle_user_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_user or not update.message or not update.message.text:
        return
    uid = update.effective_user.id
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

    menu_labels = frozenset(
        {
            "New request",
            "My loans",
            "Return an item",
            "Edit a request",
            "Admin: pending loans",
            "Admin: pending returns",
            "Exit / Cancel",
        }
    )
    if text in menu_labels:
        context.user_data.pop("expect_loan_for", None)
        context.user_data.pop("flow", None)
        context.user_data.pop("pending_group", None)
        context.user_data.pop("pending_cca", None)

    if text in {"Exit / Cancel", CCA_CANCEL_LABEL}:
        context.user_data.pop("flow", None)
        context.user_data.pop("pending_group", None)
        context.user_data.pop("pending_cca", None)
        context.user_data.pop("expect_loan_for", None)
        try:
            await update.message.reply_text(
                "Cancelled.",
                reply_markup=_main_keyboard(uid),
            )
        except TelegramError:
            logger.exception("handle_user_text: CCA cancel reply failed")
        return

    if text == "New request":
        context.user_data["flow"] = USER_FLOW_GROUP
        try:
            await update.message.reply_text(
                "Pick your Group (then you will pick Club).",
                reply_markup=_options_keyboard(list(CLUB_GROUPS.keys())),
            )
        except TelegramError:
            logger.exception("handle_user_text: New request group keyboard failed")
        return
    if text == "My loans":
        await show_my_loans(update, context, uid)
        return
    if text == "Return an item":
        await show_return_choices(update, context, uid)
        return
    if text == "Edit a request":
        await show_edit_choices(update, context, uid)
        return
    if text == "Admin: pending loans":
        await admin_pending_loans(update, context)
        return
    if text == "Admin: pending returns":
        await admin_pending_returns(update, context)
        return

    if _is_admin(uid) and context.user_data.get("expect_loan_for"):
        parsed_loan = parse_three_csv_fields(text)
        if not parsed_loan:
            try:
                await update.message.reply_text(
                    "Loan details must use the same three-part format:\n\n"
                    f"{FORMAT_HELP}\n\n"
                    "Example for what you physically handed out: Mic set A, 1, signed out from store\n"
                    "(for one selected request, send one line only)."
                )
            except TelegramError:
                logger.exception("handle_user_text: loan format reply failed")
            return
        item, qty, reason = parsed_loan
        tx_id = context.user_data["expect_loan_for"]
        now = sheets.now_iso()
        try:
            ok = await _sheet(
                sheets.update_transaction,
                CRED,
                SHEET,
                tx_id,
                {
                    "loan_item": item,
                    "loan_qty": qty,
                    "loan_reason": reason,
                    "admin_tg_id": str(uid),
                    "admin_username": u.username or "",
                    "loan_recorded_at": now,
                    "status": sheets.STATUS_AWAITING_ACK,
                },
            )
        except SheetsBackendError:
            try:
                await update.message.reply_text(SHEET_ERROR_TEXT)
            except TelegramError:
                logger.exception("handle_user_text: sheet error reply failed")
            return
        context.user_data.pop("expect_loan_for", None)
        try:
            if ok:
                await _admin_audit(
                    context,
                    action="loan_recorded",
                    admin_user=u,
                    tx_id=tx_id,
                    notes=f"Recorded loan details: {item} x {qty}",
                )
                await update.message.reply_text(
                    f"Loan approved on file for `{tx_id}`.\n\n"
                    "Tell the borrower to open My loans and tap Acknowledge "
                    '(their "signature" that they received the items).',
                    parse_mode="Markdown",
                )
            else:
                await _admin_audit(
                    context,
                    action="loan_record_failed_missing_tx",
                    admin_user=u,
                    tx_id=tx_id,
                    notes="Attempted to record loan but transaction id not found",
                )
                await update.message.reply_text("That transaction id was not found.")
        except TelegramError:
            logger.exception("handle_user_text: loan confirmation reply failed")
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
                f"Group: {text}\nNow pick Club.",
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
                f"What do you need to borrow? Send one message:\n{FORMAT_HELP}",
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
                    f"Line {bad_line} does not match the required format.\n\n"
                    f"{FORMAT_HELP}\n\n"
                    "You can send one item or many lines at once.\n"
                    "Tap New request if you want to restart from CCA."
                )
            except TelegramError:
                logger.exception("handle_user_text: format error reply failed")
            return
        group = context.user_data.get("pending_group", "")
        club = context.user_data.get("pending_cca", "")
        cca = f"{group} / {club}".strip(" /")
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
                    f"Logged request `{tx_ids[0]}`. Logistics will record what was loaned next.",
                    parse_mode="Markdown",
                )
            else:
                shown = ", ".join(tx_ids[:10])
                extra = "" if len(tx_ids) <= 10 else f" (+{len(tx_ids) - 10} more)"
                await update.message.reply_text(
                    f"Logged {len(tx_ids)} requests for this CCA.\n"
                    f"IDs: {shown}{extra}\n"
                    "Logistics can now process them in Admin: pending loans."
                )
        except TelegramError:
            logger.exception("handle_user_text: success reply failed")
        return

    try:
        await update.message.reply_text("Use the menu buttons, or send /help for instructions.")
    except TelegramError:
        logger.exception("handle_user_text: default hint reply failed")


async def show_my_loans(
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
            logger.exception("show_my_loans: sheet error reply failed")
        return
    mine = [
        t
        for t in txs
        if t.get("requester_tg_id") == str(uid)
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
            f"• `{sid}` [{st}]\n  CCA: {t.get('cca','')}\n"
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
        "Your loans — if status is awaiting_user_ack, tap Sign / acknowledge to confirm "
        "you received what logistics recorded.\n\n"
    )
    await _reply_markdown_safe(
        update.message,
        header + ("\n\n".join(lines) or "Nothing to show."),
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None,
    )


async def show_return_choices(
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
            logger.exception("show_return_choices: sheet error reply failed")
        return
    mine = [
        t
        for t in txs
        if t.get("requester_tg_id") == str(uid)
        and t.get("status") == sheets.STATUS_ON_LOAN
    ]
    if not mine:
        try:
            await update.message.reply_text(
                "You have nothing on loan that can be returned yet."
            )
        except TelegramError:
            logger.exception("show_return_choices: empty reply failed")
        return
    buttons = [
        [
            InlineKeyboardButton(
                f"Return {t['id'][:6]}…",
                callback_data=f"rtn:{t['id']}",
            )
        ]
        for t in mine[-15:]
    ]
    try:
        await update.message.reply_text(
            "Pick the item you are returning. Logistics must approve before it is marked returned.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except TelegramError:
        logger.exception("show_return_choices: keyboard reply failed")


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
            await update.message.reply_text("No requests waiting for loan details.")
        except TelegramError:
            logger.exception("admin_pending_loans: empty reply failed")
        return
    buttons = [
        [
            InlineKeyboardButton(
                f"Record loan {t['id'][:6]}…",
                callback_data=f"pv:{t['id']}",
            )
        ]
        for t in pending[-20:]
    ]
    try:
        await update.message.reply_text(
            "Approve by recording what you actually handed out.\n"
            "Pick a request, then send one line:\n"
            "item, qty, reason\n"
            "(same format as borrowers). That moves it to awaiting their acknowledgement.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except TelegramError:
        logger.exception("admin_pending_loans: keyboard reply failed")


async def admin_pending_returns(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not update.message or not update.effective_user:
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
            "When the physical item is back with you, approve the return below "
            "(logs time and your Telegram id on the sheet).",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except TelegramError:
        logger.exception("admin_pending_returns: keyboard reply failed")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data or not update.effective_user:
        return
    await _callback_ack(q)
    uid = update.effective_user.id
    if not _verified(context, uid):
        await _edit_callback_message(q, "Session not unlocked.")
        return

    data = q.data
    if data.startswith("pv:"):
        if not _is_admin(uid):
            await _edit_callback_message(q, "Admin only.")
            return
        tx_id = data.split(":", 1)[1]
        context.user_data["expect_loan_for"] = tx_id
        await _edit_callback_message(
            q,
            f"Selected `{tx_id}`. Send one message: item, qty, reason",
            parse_mode="Markdown",
        )
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
        now = sheets.now_iso()
        try:
            await _sheet(
                sheets.update_transaction,
                CRED,
                SHEET,
                tx_id,
                {"user_ack_at": now, "status": sheets.STATUS_ON_LOAN},
            )
        except SheetsBackendError:
            await _edit_callback_message(q, SHEET_ERROR_TEXT)
            return
        await _edit_callback_message(
            q,
            "Signed — acknowledgement saved. Status is now on loan.",
        )
        return

    if data.startswith("rtn:"):
        tx_id = data.split(":", 1)[1]
        try:
            t = await _sheet(sheets.find_transaction, CRED, SHEET, tx_id)
        except SheetsBackendError:
            await _edit_callback_message(q, SHEET_ERROR_TEXT)
            return
        if not t or t.get("requester_tg_id") != str(uid):
            await _edit_callback_message(q, "Not found or not yours.")
            return
        if t.get("status") != sheets.STATUS_ON_LOAN:
            await _edit_callback_message(q, "Only active loans can start a return.")
            return
        now = sheets.now_iso()
        try:
            await _sheet(
                sheets.update_transaction,
                CRED,
                SHEET,
                tx_id,
                {"return_requested_at": now, "status": sheets.STATUS_PENDING_RETURN},
            )
        except SheetsBackendError:
            await _edit_callback_message(q, SHEET_ERROR_TEXT)
            return
        await _edit_callback_message(
            q,
            "Return requested. Logistics will approve it in the bot when the item is back.",
        )
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
        if t.get("status") != sheets.STATUS_PENDING_ADMIN:
            await _edit_callback_message(
                q, "Only pending_admin requests can be edited."
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
    except RuntimeError as e:
        raise SystemExit(str(e)) from e

    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .build()
    )
    app.bot_data["unlocked_until"] = {}
    app.bot_data["user_msg_times"] = {}

    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancelreq", cmd_cancelreq))
    app.add_handler(CommandHandler("editreq", cmd_editreq))
    app.add_handler(CommandHandler("adminlog", cmd_adminlog))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            try_passcode,
        ),
    )

    logger.info("Starting bot (long polling). For 24/7, keep this process running on a host.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
