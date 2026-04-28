import json
import os
from pathlib import Path
from typing import Any

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


def _parse_positive_int(raw: str | None, default: int) -> int:
    if not raw:
        return default
    try:
        n = int(raw.strip())
    except ValueError:
        return default
    return n if n > 0 else default


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
BOT_PASSCODE = os.environ.get("BOT_PASSCODE", "").strip()
ADMIN_TELEGRAM_IDS = _parse_admin_ids(os.environ.get("ADMIN_TELEGRAM_IDS"))
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
GOOGLE_SERVICE_ACCOUNT_FILE = os.environ.get(
    "GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json"
).strip()

# Optional: full service account JSON (for Render etc.) — avoids committing a file.
_raw_sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
SERVICE_ACCOUNT_INFO: dict[str, Any] | None = None
SERVICE_ACCOUNT_JSON_ERROR: str | None = None
if _raw_sa_json:
    try:
        parsed = json.loads(_raw_sa_json)
        if isinstance(parsed, dict):
            SERVICE_ACCOUNT_INFO = parsed
        else:
            SERVICE_ACCOUNT_JSON_ERROR = "GOOGLE_SERVICE_ACCOUNT_JSON must be a JSON object"
    except json.JSONDecodeError as e:
        SERVICE_ACCOUNT_JSON_ERROR = f"GOOGLE_SERVICE_ACCOUNT_JSON is invalid JSON: {e}"

SESSION_TTL_MINUTES = _parse_positive_int(os.environ.get("SESSION_TTL_MINUTES"), 30)


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
    if SESSION_TTL_MINUTES < 1:
        errors.append("SESSION_TTL_MINUTES must be >= 1")
    if SERVICE_ACCOUNT_JSON_ERROR:
        errors.append(SERVICE_ACCOUNT_JSON_ERROR)
    elif SERVICE_ACCOUNT_INFO is not None:
        pass
    elif Path(GOOGLE_SERVICE_ACCOUNT_FILE).is_file():
        pass
    else:
        errors.append(
            "Set GOOGLE_SERVICE_ACCOUNT_JSON (paste full JSON in env) or place "
            f"GOOGLE_SERVICE_ACCOUNT_FILE at {Path(GOOGLE_SERVICE_ACCOUNT_FILE).resolve()}"
        )
    return errors
