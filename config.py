import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()


def _normalize_admin_ids_env(raw: str) -> str:
    """Strip BOM, trim, drop trailing # comments (single-line .env style)."""
    s = raw.replace("\ufeff", "").strip()
    if "#" in s:
        s = s.split("#", 1)[0].strip()
    return s


def _parse_admin_ids(raw: str | None) -> set[int]:
    if not raw:
        return set()
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip().strip('"').strip("'")  # allow quoted tokens in env
        if not part:
            continue
        if part.isdigit():
            out.add(int(part))
            continue
        if part.startswith("+") and part[1:].isdigit():
            out.add(int(part[1:]))
    return out


def _parse_positive_int(raw: str | None, default: int) -> int:
    if not raw:
        return default
    try:
        n = int(raw.strip())
    except ValueError:
        return default
    return n if n > 0 else default


def _parse_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    v = raw.strip().lower()
    if v in {"1", "true", "yes", "y", "on"}:
        return True
    if v in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _parse_hour(raw: str | None, default: int) -> int:
    if raw is None or not raw.strip():
        return default
    try:
        n = int(raw.strip())
    except ValueError:
        return default
    return n


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
BOT_PASSCODE = os.environ.get("BOT_PASSCODE", "").strip()
ADMIN_TELEGRAM_IDS_RAW = os.environ.get("ADMIN_TELEGRAM_IDS", "").strip()
ADMIN_TELEGRAM_IDS = _parse_admin_ids(_normalize_admin_ids_env(ADMIN_TELEGRAM_IDS_RAW))
ALERT_TELEGRAM_IDS = _parse_admin_ids(
    os.environ.get("ALERT_TELEGRAM_IDS") or os.environ.get("ADMIN_TELEGRAM_IDS")
)
ALERT_COOLDOWN_SEC = _parse_positive_int(os.environ.get("ALERT_COOLDOWN_SEC"), 300)
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
OPERATING_HOURS_ENABLED = _parse_bool(os.environ.get("OPERATING_HOURS_ENABLED"), True)
OPENING_HOUR_24 = _parse_hour(os.environ.get("OPENING_HOUR_24"), 9)
CLOSING_HOUR_24 = _parse_hour(os.environ.get("CLOSING_HOUR_24"), 21)


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
        if _normalize_admin_ids_env(ADMIN_TELEGRAM_IDS_RAW):
            errors.append(
                "ADMIN_TELEGRAM_IDS is set but parsed to zero valid IDs. "
                "Use numeric IDs only — open the bot and send /whoami, "
                'then paste that number here (comma-separated). Do not use @username.'
            )
        else:
            errors.append(
                "ADMIN_TELEGRAM_IDS is missing (comma-separated numeric Telegram user IDs)"
            )
    if not ALERT_TELEGRAM_IDS:
        errors.append("ALERT_TELEGRAM_IDS is missing (comma-separated Telegram user IDs)")
    if not GOOGLE_SHEET_ID:
        errors.append("GOOGLE_SHEET_ID is missing")
    if SESSION_TTL_MINUTES < 1:
        errors.append("SESSION_TTL_MINUTES must be >= 1")
    if not (0 <= OPENING_HOUR_24 <= 23):
        errors.append("OPENING_HOUR_24 must be 0..23")
    if not (0 <= CLOSING_HOUR_24 <= 23):
        errors.append("CLOSING_HOUR_24 must be 0..23")
    if OPENING_HOUR_24 == CLOSING_HOUR_24:
        errors.append("OPENING_HOUR_24 and CLOSING_HOUR_24 cannot be the same")
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
