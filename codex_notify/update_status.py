from __future__ import annotations

import json
import re
from pathlib import Path

from .i18n import tr
from .models import Language

STATUS_VALUES = {"installed", "success", "up_to_date", "docs_only", "failed", "rolled_back"}
SHA_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")


def read_update_status(path: Path, language: Language = "en") -> str:
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
        if status not in STATUS_VALUES:
            raise ValueError("unknown status")
        if not isinstance(commit, str) or not SHA_PATTERN.fullmatch(commit):
            commit = tr(language, "update.unknown_commit")
        else:
            commit = commit[:12]
        if not isinstance(updated_at, str) or len(updated_at) > 40:
            updated_at = tr(language, "update.unknown_time")
        return f"{tr(language, f'update.{status}')} · {commit} · {updated_at}"
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return tr(language, "update.unavailable")
