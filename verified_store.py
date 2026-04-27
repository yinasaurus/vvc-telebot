import json
from pathlib import Path


def load_verified_users(path: Path) -> set[int]:
    if not path.is_file():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {int(x) for x in data if str(x).isdigit()}
    except (json.JSONDecodeError, OSError, ValueError):
        pass
    return set()


def save_verified_users(path: Path, user_ids: set[int]) -> bool:
    try:
        path.write_text(
            json.dumps(sorted(user_ids), indent=2),
            encoding="utf-8",
        )
        return True
    except OSError:
        return False
