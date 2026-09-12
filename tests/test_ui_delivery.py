from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import Chat, Message, User

from codex_notify.access import OwnerAccessMiddleware
from codex_notify.binding import set_owner
from codex_notify.delivery import OutboxDelivery
from codex_notify.domain import make_event
from codex_notify.main import SecretFilter
from codex_notify.models import AppState, EventPayload, OutboxItem
from codex_notify.storage import Repository
from codex_notify.telegram_ui import render_status
from codex_notify.timeutil import utc_now_iso
from codex_notify.update_status import read_update_status


class FailingBot:
    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
        raise RuntimeError("offline")


class SuccessfulBot:
    def __init__(self) -> None:
        self.sent = asyncio.Event()
        self.text: str | None = None

    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
        self.text = text
        self.sent.set()


def queued() -> OutboxItem:
    return OutboxItem(
        id="queued",
        event_ids=["event"],
        legacy_text="delayed",
        created_at=utc_now_iso(),
        detected_at=utc_now_iso(),
    )


def telegram_message(user_id: int, *, text: str = "/start", chat_type: str = "private") -> Message:
    return Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=user_id, type=chat_type),
        from_user=User(id=user_id, is_bot=False, first_name="User"),
        text=text,
    )


@pytest.mark.asyncio
async def test_failed_telegram_delivery_stays_in_persistent_outbox(tmp_path: Path) -> None:
    repository = Repository(tmp_path)
    await repository.initialize()
    await set_owner(repository.settings, 1234)
    await repository.state.mutate(lambda state: state.outbox.append(queued()))
    delivery = OutboxDelivery(repository, FailingBot())
    task = asyncio.create_task(delivery.run())
    for _ in range(50):
        if (await repository.state.get()).outbox[0].attempts:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    state = await repository.state.get()
    assert state.outbox[0].attempts == 1
    assert state.outbox[0].not_before is not None


@pytest.mark.asyncio
async def test_successful_telegram_ack_removes_outbox_item(tmp_path: Path) -> None:
    repository = Repository(tmp_path)
    await repository.initialize()
    await set_owner(repository.settings, 1234)
    await repository.state.mutate(lambda state: state.outbox.append(queued()))
    bot = SuccessfulBot()
    delivery = OutboxDelivery(repository, bot)
    task = asyncio.create_task(delivery.run())
    await asyncio.wait_for(bot.sent.wait(), timeout=1)
    for _ in range(50):
        if not (await repository.state.get()).outbox:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert (await repository.state.get()).outbox == []


@pytest.mark.asyncio
async def test_structured_outbox_uses_language_selected_at_delivery(tmp_path: Path) -> None:
    repository = Repository(tmp_path)
    await repository.initialize()
    await set_owner(repository.settings, 1234)
    event = make_event(
        "reset_credit_granted",
        "reset_credit_granted",
        {"account": "account", "count": 1},
        EventPayload(available_count=1),
    )
    item = OutboxItem(
        id="structured",
        event_ids=[event.id],
        events=[event],
        created_at=event.detected_at,
        detected_at=event.detected_at,
    )
    await repository.state.mutate(lambda state: state.outbox.append(item))
    await repository.settings.mutate(lambda settings: setattr(settings, "language", "ru"))
    bot = SuccessfulBot()
    delivery = OutboxDelivery(repository, bot)
    task = asyncio.create_task(delivery.run())
    await asyncio.wait_for(bot.sent.wait(), timeout=1)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert bot.text is not None
    assert "Доступен новый reset-кредит" in bot.text


def test_log_filter_redacts_token_even_inside_an_exception_message() -> None:
    secret = "fake-secret-value-for-redaction"
    record = logging.LogRecord("x", logging.ERROR, "", 0, "failure %s", (secret,), None)
    assert SecretFilter(secret).filter(record)
    assert secret not in record.getMessage()
    assert "REDACTED" in record.getMessage()


@pytest.mark.asyncio
async def test_owner_and_private_chat_guard_for_messages(tmp_path: Path) -> None:
    from codex_notify.telegram_ui import TelegramUI

    repository = Repository(tmp_path)
    await repository.initialize()
    await set_owner(repository.settings, 1234)
    ui = object.__new__(TelegramUI)
    ui.repository = repository

    foreign = SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=9999),
        answer=AsyncMock(),
    )
    assert not await ui._authorized_message(foreign)
    foreign.answer.assert_not_awaited()

    group = SimpleNamespace(
        chat=SimpleNamespace(type="group"),
        from_user=SimpleNamespace(id=1234),
        answer=AsyncMock(),
    )
    assert not await ui._authorized_message(group)

    owner = SimpleNamespace(
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=1234),
        answer=AsyncMock(),
    )
    assert await ui._authorized_message(owner)


@pytest.mark.asyncio
async def test_outer_middleware_drops_strangers_and_groups_before_handler(tmp_path: Path) -> None:
    repository = Repository(tmp_path)
    await repository.initialize()
    await set_owner(repository.settings, 1234)
    middleware = OwnerAccessMiddleware(repository)
    handler = AsyncMock()

    await middleware(handler, telegram_message(9999), {})
    await middleware(handler, telegram_message(1234, chat_type="group"), {})

    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_outer_middleware_allows_only_well_formed_claim_before_binding(
    tmp_path: Path,
) -> None:
    repository = Repository(tmp_path)
    await repository.initialize()
    middleware = OwnerAccessMiddleware(repository)
    handler = AsyncMock()

    await middleware(handler, telegram_message(9999, text="/start claim_short"), {})
    handler.assert_not_awaited()
    await middleware(
        handler,
        telegram_message(9999, text=f"/start claim_{'A' * 32}"),
        {},
    )
    handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_foreign_callback_is_rejected_without_data(tmp_path: Path) -> None:
    from codex_notify.telegram_ui import TelegramUI

    repository = Repository(tmp_path)
    await repository.initialize()
    await set_owner(repository.settings, 1234)
    ui = object.__new__(TelegramUI)
    ui.repository = repository
    query = SimpleNamespace(
        message=SimpleNamespace(chat=SimpleNamespace(type="private")),
        from_user=SimpleNamespace(id=2222),
        answer=AsyncMock(),
    )
    assert not await ui._authorized_callback(query)
    query.answer.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_commands_are_owner_scoped_and_follow_selected_language(tmp_path: Path) -> None:
    from codex_notify.telegram_ui import TelegramUI

    repository = Repository(tmp_path)
    await repository.initialize()
    await set_owner(repository.settings, 1234)
    bot = SimpleNamespace(delete_my_commands=AsyncMock(), set_my_commands=AsyncMock())
    ui = object.__new__(TelegramUI)
    ui.repository = repository
    ui.bot = bot

    await ui.register_commands()
    (commands,) = bot.set_my_commands.await_args.args
    scope = bot.set_my_commands.await_args.kwargs["scope"]
    assert scope.chat_id == 1234
    assert commands[0].description == "Show current limits"
    bot.delete_my_commands.assert_awaited_once_with()

    await repository.settings.mutate(lambda settings: setattr(settings, "language", "ru"))
    await ui.register_commands()
    (commands,) = bot.set_my_commands.await_args.args
    assert commands[0].description == "Показать текущие лимиты"


@pytest.mark.asyncio
async def test_status_marks_cached_data_stale_and_unknown_is_not_zero(tmp_path: Path) -> None:
    repository = Repository(tmp_path)
    settings, _ = await repository.initialize()

    def update(state: AppState) -> None:
        state.last_error = "codex_unavailable"

    state = await repository.state.mutate(update)
    text = render_status(settings, state)
    assert "stale" in text
    assert "0%" not in text


def test_update_status_reader_accepts_only_bounded_known_values(tmp_path: Path) -> None:
    path = tmp_path / "update-status.json"
    path.write_text(
        '{"status":"success","commit":"abcdef0123456789","updated_at":"2026-09-12T10:00:00Z"}',
        encoding="utf-8",
    )
    assert "updated successfully" in read_update_status(path)
    assert "обновлено успешно" in read_update_status(path, "ru")
    path.write_text('{"status":"<script>","commit":"bad"}', encoding="utf-8")
    assert read_update_status(path) == "no result recorded yet"
