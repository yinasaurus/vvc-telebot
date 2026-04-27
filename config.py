import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _parse_admin_ids(raw: str | None) -> set[int]:
    if not raw:
        return set()
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
BOT_PASSCODE = os.environ.get("BOT_PASSCODE", "").strip()
ADMIN_TELEGRAM_IDS = _parse_admin_ids(os.environ.get("ADMIN_TELEGRAM_IDS"))
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get(
    "GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json"
).strip()

VERIFIED_USERS_PATH = Path(__file__).resolve().parent / "verified_users.json"


def validate_config() -> list[str]:
    errors: list[str] = []
    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN is missing")
    if not BOT_PASSCODE:
        errors.append("BOT_PASSCODE is missing")
    elif len(BOT_PASSCODE) < 12:
        errors.append(
            "BOT_PASSCODE should be at least 12 characters (use a random phrase or password manager)"
        )
    if not ADMIN_TELEGRAM_IDS:
        errors.append("ADMIN_TELEGRAM_IDS is missing (comma-separated Telegram user IDs)")
    if not GOOGLE_SHEET_ID:
        errors.append("GOOGLE_SHEET_ID is missing")
    p = Path(GOOGLE_SERVICE_ACCOUNT_FILE)
    if not p.is_file():
        errors.append(f"GOOGLE_SERVICE_ACCOUNT_FILE not found: {p.resolve()}")
    return errors
