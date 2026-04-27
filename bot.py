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
from verified_store import load_verified_users, save_verified_users

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

USER_FLOW_CCA = "await_cca"
USER_FLOW_DESC = "await_need_desc"

FORMAT_HELP = (
    "Use exactly three parts separated by commas:\n"
    "item, qty, reason\n\n"
    "Example: HDMI cable, 2, Year-end concert booth"
)

# When CCA_OPTIONS is set in .env, users pick CCA from buttons instead of typing.
CCA_CANCEL_LABEL = "« Cancel"


def _cca_pick_keyboard() -> ReplyKeyboardMarkup:
    opts = list(config.CCA_OPTIONS)
    rows: list[list[KeyboardButton]] = []
    for i in range(0, len(opts), 2):
        row = [KeyboardButton(opts[i])]
        if i + 1 < len(opts):
            row.append(KeyboardButton(opts[i + 1]))
        rows.append(row)
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


def parse_three_csv_fields(text: str) -> tuple[str, str, str] | None:
    """Split on first two commas so `reason` may contain commas."""
    parts = [p.strip() for p in text.strip().split(",", 2)]
    if len(parts) != 3:
        return None
    item, qty, reason = parts
    if not item or not qty or not reason:
        return None
    return item, qty, reason


def _is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_TELEGRAM_IDS


def _verified(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    return user_id in context.application.bot_data["verified_users"]


def _main_keyboard(user_id: int) -> ReplyKeyboardMarkup:
    rows: list[list[KeyboardButton]] = [
        [KeyboardButton("New request"), KeyboardButton("My loans")],
        [KeyboardButton("Return an item")],
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
    if config.CCA_OPTIONS:
        return (
            "When you tap New request, you pick your CCA from buttons (no need to type the name).\n"
        )
    return (
        "When you tap New request, the bot will ask which CCA — reply with one short line "
        "(e.g. your group name).\n"
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
        "/admin — Logistics shortcuts only (admins)",
        "",
        "Borrower workflow",
        "1) Unlock — First time here: send the shared passcode (only in this private chat). "
        "Your Telegram account stays unlocked on this server.",
        "",
        "2) New request — Say what you need in exactly one line:",
        "   item, qty, reason",
        "   Example: HDMI cable, 2, Year-end concert booth",
        "",
        _cca_help_sentence(),
        "",
        "3) Logistics enters what they actually loaned; then you open My loans.",
        "",
        '4) Tap Sign / acknowledge — that confirms you received the items (your "signature" on the log).',
        "",
        "5) Return an item — When you're bringing gear back; logistics approves to close the loan.",
        "",
        "Tips",
        "• If the format is wrong, the bot sends the template again.",
        "• Use the buttons at the bottom of the chat — they're faster than typing commands.",
    ]
    if include_admin:
        lines.extend(
            [
                "",
                "— Logistics (you are an admin) —",
                "• Admin: pending loans — Pick a request, then send item, qty, reason for what you handed out.",
                "• Admin: pending returns — Approve when the item is physically back.",
                "Use /admin for a shorter reminder.",
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
        "After this Telegram account unlocks once on this server, you won't need the passcode again.\n\n"
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

    users: set[int] = context.application.bot_data["verified_users"]
    users.add(uid)
    if not save_verified_users(config.VERIFIED_USERS_PATH, users):
        users.discard(uid)
        logger.error("Failed to persist verified_users.json for uid=%s", uid)
        try:
            await update.message.reply_text(
                "Unlock could not be saved on the server (disk or permissions). "
                "Ask the bot maintainer to check verified_users.json."
            )
        except TelegramError:
            logger.exception("try_passcode: save-error reply failed")
        return
    try:
        await update.message.reply_text(
            "Unlocked.\n\n"
            "Use the bottom buttons: New request, My loans, Return an item. "
            "Admins also see Admin: pending loans / pending returns.\n\n"
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
    text = update.message.text.strip()
    u = update.effective_user

    menu_labels = frozenset(
        {
            "New request",
            "My loans",
            "Return an item",
            "Admin: pending loans",
            "Admin: pending returns",
        }
    )
    if text in menu_labels:
        context.user_data.pop("expect_loan_for", None)
        context.user_data.pop("flow", None)
        context.user_data.pop("pending_cca", None)

    if (
        context.user_data.get("flow") == USER_FLOW_CCA
        and config.CCA_OPTIONS
        and text == CCA_CANCEL_LABEL
    ):
        context.user_data.pop("flow", None)
        context.user_data.pop("pending_cca", None)
        try:
            await update.message.reply_text(
                "Cancelled.",
                reply_markup=_main_keyboard(uid),
            )
        except TelegramError:
            logger.exception("handle_user_text: CCA cancel reply failed")
        return

    if text == "New request":
        context.user_data["flow"] = USER_FLOW_CCA
        if config.CCA_OPTIONS:
            try:
                await update.message.reply_text(
                    "Pick your CCA below (or Cancel).",
                    reply_markup=_cca_pick_keyboard(),
                )
            except TelegramError:
                logger.exception("handle_user_text: New request CCA keyboard failed")
        else:
            try:
                await update.message.reply_text(
                    "What CCA is this for? Reply with one line (name of the CCA or group)."
                )
            except TelegramError:
                logger.exception("handle_user_text: New request free CCA failed")
        return
    if text == "My loans":
        await show_my_loans(update, context, uid)
        return
    if text == "Return an item":
        await show_return_choices(update, context, uid)
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
                    "Example for what you physically handed out: Mic set A, 1, signed out from store"
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
                await update.message.reply_text(
                    f"Loan approved on file for `{tx_id}`.\n\n"
                    "Tell the borrower to open My loans and tap Acknowledge "
                    '(their "signature" that they received the items).',
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text("That transaction id was not found.")
        except TelegramError:
            logger.exception("handle_user_text: loan confirmation reply failed")
        return

    flow = context.user_data.get("flow")
    if flow == USER_FLOW_CCA:
        if config.CCA_OPTIONS and text not in config.CCA_OPTIONS:
            try:
                await update.message.reply_text(
                    "Pick one of the CCA buttons above, or tap « Cancel.",
                    reply_markup=_cca_pick_keyboard(),
                )
            except TelegramError:
                logger.exception("handle_user_text: invalid CCA pick reply failed")
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
        parsed = parse_three_csv_fields(text)
        if not parsed:
            try:
                await update.message.reply_text(
                    "That line does not match the required format.\n\n"
                    f"{FORMAT_HELP}\n\n"
                    "Tap New request if you want to restart from CCA."
                )
            except TelegramError:
                logger.exception("handle_user_text: format error reply failed")
            return
        item, qty, reason = parsed
        cca = context.user_data.get("pending_cca", "")
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
                await update.message.reply_text(
                    f"{SHEET_ERROR_TEXT} Your answers are still here; send the same description again "
                    "or tap New request to restart."
                )
            except TelegramError:
                logger.exception("handle_user_text: append error reply failed")
            return
        context.user_data.pop("pending_cca", None)
        context.user_data.pop("flow", None)
        try:
            await update.message.reply_text(
                f"Logged request `{tx_id}`. Logistics will record what was loaned next.",
                parse_mode="Markdown",
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

    verified = load_verified_users(config.VERIFIED_USERS_PATH)

    app = (
        Application.builder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .build()
    )
    app.bot_data["verified_users"] = verified

    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
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
