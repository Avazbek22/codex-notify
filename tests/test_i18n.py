from __future__ import annotations

import ast
from pathlib import Path
from typing import get_args

from codex_notify.domain import make_event, normalize_snapshot
from codex_notify.i18n import CATALOGS, SUPPORTED_LANGUAGES, tr
from codex_notify.models import AppState, Event, EventCode, EventPayload, Settings
from codex_notify.presentation import render_details, render_event, render_status
from codex_notify.telegram_ui import MAIN_BUTTONS

from .conftest import rate_payload

ROOT = Path(__file__).resolve().parents[1]


def test_catalogs_have_identical_keys_and_default_is_global() -> None:
    assert SUPPORTED_LANGUAGES == ("en", "ru")
    assert CATALOGS["en"].keys() == CATALOGS["ru"].keys()
    settings = Settings()
    assert settings.language == "en"
    assert settings.timezone == "UTC"
    assert MAIN_BUTTONS["🔄 Проверить сейчас"] == "check"


def test_every_literal_translation_reference_exists() -> None:
    referenced: set[str] = set()
    for path in (ROOT / "codex_notify").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "tr" or len(node.args) < 2:
                continue
            key = node.args[1]
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                referenced.add(key.value)
    assert referenced <= CATALOGS["en"].keys()


def test_user_facing_cyrillic_is_confined_to_translation_catalog() -> None:
    for path in (ROOT / "codex_notify").glob("*.py"):
        if path.name == "i18n.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert not any("А" <= character <= "я" or character in "Ёё" for character in text), path


def test_compact_status_hides_account_and_raw_technical_noise() -> None:
    snapshot, account = normalize_snapshot(
        rate_payload(
            used=11,
            reset=1_800_000_000,
            credits={"availableCount": 1, "credits": []},
        ),
        {"type": "chatgpt", "email": "owner@example.com", "planType": "pro"},
    )
    state = AppState(
        account=account,
        auth_status="connected",
        snapshot=snapshot,
        last_success_at=snapshot.observed_at,
    )
    settings = Settings()

    compact = render_status(settings, state)
    detailed = render_details(settings, state)

    assert "Codex available · Pro" in compact
    assert "89% left" in compact
    assert "5h" in compact
    assert "owner@example.com" not in compact
    assert "used 11%" not in compact
    assert "300 min" not in compact
    assert "o****@example.com" in detailed
    assert "used 11%" in detailed


def test_same_structured_event_renders_in_both_languages() -> None:
    event = make_event(
        "window_reset",
        "window_reset_confirmed",
        {"account": "account", "window": "codex:primary", "reset": 123},
        EventPayload(
            limit_label="Codex",
            window="primary",
            previous_used_percent=90,
            used_percent=5,
            duration_minutes=300,
        ),
    )

    assert "Limit window reset" in render_event(event, "en", "UTC", rationale=True)
    assert "Окно лимита обновилось" in render_event(event, "ru", "UTC", rationale=True)
    assert tr("en", "event.window_reset_confirmed.reason") != tr(
        "ru", "event.window_reset_confirmed.reason"
    )


def test_every_structured_event_code_has_complete_translations() -> None:
    for code in get_args(EventCode):
        if code == "legacy_v1":
            continue
        event = Event(
            id=f"event-{code}",
            type="significant_change",
            detected_at="2026-09-12T10:00:00Z",
            code=code,
            payload=EventPayload(),
        )
        assert render_event(event, "en", "UTC", rationale=True)
        assert render_event(event, "ru", "UTC", rationale=True)
