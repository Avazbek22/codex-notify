from __future__ import annotations

import json
import re
from pathlib import Path

STATUS_LABELS = {
    "installed": "установлено",
    "success": "обновлено успешно",
    "up_to_date": "обновлений нет",
    "docs_only": "обновлены только файлы без перезапуска",
    "failed": "неудача, выполнен откат",
    "rolled_back": "выполнен ручной откат",
}
SHA_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")


def read_update_status(path: Path) -> str:
    try:
        raw = path.read_bytes()
        if len(raw) > 4096:
            raise ValueError("oversized status")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("invalid status")
        status = value.get("status")
        commit = value.get("commit")
        updated_at = value.get("updated_at")
        if status not in STATUS_LABELS:
            raise ValueError("unknown status")
        if not isinstance(commit, str) or not SHA_PATTERN.fullmatch(commit):
            commit = "неизвестен"
        else:
            commit = commit[:12]
        if not isinstance(updated_at, str) or len(updated_at) > 40:
            updated_at = "время неизвестно"
        return f"{STATUS_LABELS[status]} · {commit} · {updated_at}"
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return "результат ещё не записан"
