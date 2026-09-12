from __future__ import annotations

import json
from pathlib import Path

import pytest

from codex_notify.instance_lock import AlreadyRunningError, InstanceLock
from codex_notify.models import AppState, CreditBaseline, Event, OutboxItem, Settings
from codex_notify.storage import Repository, StorageRecoveryError
from codex_notify.timeutil import utc_now_iso
from codex_notify.validate_data import validate_data


@pytest.mark.asyncio
async def test_persists_baseline_and_outbox_across_restart(tmp_path: Path) -> None:
    repository = Repository(tmp_path)
    await repository.initialize()

    def update(state: AppState) -> None:
        state.credit_baseline = CreditBaseline(
            observed_at=utc_now_iso(), available_count=2, known_grants={"credit-1": 1}
        )
        event = Event(
            id="event-1",
            type="monitor_unavailable",
            detected_at=utc_now_iso(),
            code="monitor_unavailable",
        )
        state.events.append(event)
        state.outbox.append(
            OutboxItem(
                id="outbox-1",
                event_ids=[event.id],
                events=[event],
                created_at=utc_now_iso(),
                detected_at=event.detected_at,
            )
        )

    await repository.state.mutate(update)
    restarted = Repository(tmp_path)
    _, state = await restarted.initialize()
    assert state.events[0].id == "event-1"
    assert state.outbox[0].id == "outbox-1"
    assert state.credit_baseline is not None
    assert state.credit_baseline.available_count == 2


@pytest.mark.asyncio
async def test_corrupt_primary_is_preserved_and_verified_backup_restored(tmp_path: Path) -> None:
    repository = Repository(tmp_path)
    await repository.initialize()
    await repository.settings.mutate(lambda settings: setattr(settings, "interval_minutes", 15))
    await repository.settings.mutate(lambda settings: setattr(settings, "interval_minutes", 60))
    (tmp_path / "settings.json").write_text("{broken", encoding="utf-8")

    restarted = Repository(tmp_path)
    settings, _ = await restarted.initialize()
    assert settings.interval_minutes == 15
    assert list(tmp_path.glob("settings.json.corrupt-*"))


@pytest.mark.asyncio
async def test_corruption_without_backup_fails_closed(tmp_path: Path) -> None:
    repository = Repository(tmp_path)
    await repository.initialize()
    (tmp_path / "settings.json").write_text("[]", encoding="utf-8")
    with pytest.raises(StorageRecoveryError, match=r"registration remains closed|no backup"):
        await Repository(tmp_path).initialize()


@pytest.mark.asyncio
async def test_missing_one_canonical_file_never_reopens_registration(tmp_path: Path) -> None:
    repository = Repository(tmp_path)
    await repository.initialize()
    (tmp_path / "state.json").unlink()
    with pytest.raises(StorageRecoveryError, match="refusing unsafe reset"):
        await Repository(tmp_path).initialize()


@pytest.mark.asyncio
async def test_v1_migration_preserves_owner_language_history_and_outbox(tmp_path: Path) -> None:
    detected_at = "2026-09-12T10:00:00Z"
    settings_v1 = {
        "schema_version": 1,
        "owner_id": 123456,
        "interval_minutes": 30,
        "timezone": "Europe/Moscow",
        "paused": False,
        "notifications": {
            "window_updates": True,
            "usage_restored": True,
            "reset_credits": True,
            "significant_changes": True,
            "credit_expiry_reminder": False,
            "service_health": True,
        },
        "binding": None,
    }
    state_v1 = {
        "schema_version": 1,
        "account": None,
        "auth_status": "disconnected",
        "snapshot": None,
        "credit_baseline": None,
        "events": [
            {
                "id": "legacy-event",
                "type": "monitor_unavailable",
                "detected_at": detected_at,
                "title": "Старое событие",
                "details": "Сохранённые детали",
                "rationale": "Сохранённое основание",
            }
        ],
        "dedupe_event_ids": ["legacy-event"],
        "outbox": [
            {
                "id": "legacy-outbox",
                "event_ids": ["legacy-event"],
                "text": "Старое уведомление",
                "created_at": detected_at,
                "detected_at": detected_at,
                "attempts": 0,
                "not_before": None,
            }
        ],
        "last_success_at": None,
        "next_check_at": None,
        "last_error": None,
        "consecutive_failures": 0,
        "outage_started_at": None,
        "outage_notified": False,
        "auth_required_notified": False,
        "pending_login": None,
        "post_reset_checks": [],
    }
    (tmp_path / "settings.json").write_text(json.dumps(settings_v1), encoding="utf-8")
    (tmp_path / "state.json").write_text(json.dumps(state_v1), encoding="utf-8")

    settings, state = await Repository(tmp_path).initialize()

    assert settings.schema_version == 2
    assert settings.owner_id == 123456
    assert settings.language == "ru"
    assert settings.timezone == "Europe/Moscow"
    assert state.schema_version == 2
    assert state.events[0].code == "legacy_v1"
    assert state.events[0].legacy_text is not None
    assert state.events[0].legacy_text.title == "Старое событие"
    assert state.outbox[0].legacy_text == "Старое уведомление"
    assert json.loads((tmp_path / "settings.json").read_text())["schema_version"] == 2
    assert json.loads((tmp_path / "state.json").read_text())["schema_version"] == 2


def test_preflight_validation_accepts_v1_without_mutating_rollback_copy(tmp_path: Path) -> None:
    settings = Settings().model_dump()
    settings["schema_version"] = 1
    settings.pop("language")
    state = AppState().model_dump()
    state["schema_version"] = 1
    settings_raw = json.dumps(settings)
    state_raw = json.dumps(state)
    (tmp_path / "settings.json").write_text(settings_raw, encoding="utf-8")
    (tmp_path / "state.json").write_text(state_raw, encoding="utf-8")

    validate_data(tmp_path)

    assert (tmp_path / "settings.json").read_text(encoding="utf-8") == settings_raw
    assert (tmp_path / "state.json").read_text(encoding="utf-8") == state_raw


@pytest.mark.asyncio
async def test_failure_before_atomic_replace_keeps_old_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = Repository(tmp_path)
    await repository.initialize()
    from codex_notify import storage

    real_replace = storage.os.replace

    def fail_primary(source: Path, target: Path) -> None:
        if Path(target) == tmp_path / "state.json":
            raise OSError("simulated crash")
        real_replace(source, target)

    monkeypatch.setattr(storage.os, "replace", fail_primary)
    with pytest.raises(OSError, match="simulated"):
        await repository.state.mutate(lambda state: setattr(state, "consecutive_failures", 3))
    assert (await repository.state.get()).consecutive_failures == 0
    assert json.loads((tmp_path / "state.json").read_text())["consecutive_failures"] == 0


@pytest.mark.asyncio
async def test_failure_after_replace_recovers_committed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = Repository(tmp_path)
    await repository.initialize()
    from codex_notify import storage

    calls = 0
    real_fsync = storage._fsync_directory

    def fail_after_state(directory: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("power loss after replace")
        real_fsync(directory)

    monkeypatch.setattr(storage, "_fsync_directory", fail_after_state)
    with pytest.raises(OSError, match="power loss"):
        await repository.state.mutate(lambda state: setattr(state, "consecutive_failures", 1))
    reloaded = Repository(tmp_path)
    _, state = await reloaded.initialize()
    assert state.consecutive_failures in {0, 1}


def test_history_is_bounded_but_unsent_outbox_is_never_discarded() -> None:
    now = utc_now_iso()
    state = AppState(
        events=[
            Event(
                id=f"event-{index}",
                type="significant_change",
                detected_at=now,
                code="backend_limit_status_changed",
            )
            for index in range(250)
        ],
        dedupe_event_ids=[f"event-{index}" for index in range(1100)],
        outbox=[
            OutboxItem(
                id=f"outbox-{index}",
                event_ids=[],
                legacy_text="message",
                created_at=now,
                detected_at=now,
            )
            for index in range(250)
        ],
    )
    state.bounded()
    assert len(state.events) == 200
    assert len(state.dedupe_event_ids) == 1000
    assert len(state.outbox) == 250


def test_instance_lock_refuses_two_process_owners_of_one_data_directory(tmp_path: Path) -> None:
    first = InstanceLock(tmp_path / "instance.lock")
    second = InstanceLock(tmp_path / "instance.lock")
    first.acquire()
    try:
        with pytest.raises(AlreadyRunningError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()
