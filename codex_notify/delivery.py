from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter

from .i18n import tr
from .models import AppState
from .monitor import outbox_ready, retry_delay
from .presentation import render_event_bundle
from .storage import Repository
from .timeutil import utc_now


class OutboxDelivery:
    def __init__(self, repository: Repository, bot: Any) -> None:
        self.repository = repository
        self.bot = bot
        self._wakeup = asyncio.Event()
        self.running = False
        self.last_error: str | None = None

    def wake(self) -> None:
        self._wakeup.set()

    async def run(self) -> None:
        self.running = True
        try:
            while True:
                settings = await self.repository.settings.get()
                state = await self.repository.state.get()
                item = next(
                    (entry for entry in state.outbox if outbox_ready(entry.not_before)), None
                )
                if settings.owner_id is None or item is None:
                    self._wakeup.clear()
                    try:
                        async with asyncio.timeout(30):
                            await self._wakeup.wait()
                    except TimeoutError:
                        pass
                    continue
                try:
                    if item.legacy_text is not None:
                        text = item.legacy_text
                    elif item.events:
                        text = render_event_bundle(
                            item.events,
                            settings.language,
                            settings.timezone,
                            item.detected_at,
                        )
                    else:
                        text = tr(settings.language, "event.unavailable")
                    await self.bot.send_message(
                        settings.owner_id,
                        text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                except TelegramRetryAfter as exc:
                    await self._postpone(item.id, float(exc.retry_after), "Telegram flood control")
                except TelegramForbiddenError:
                    await self._postpone(item.id, 3600.0, "telegram_bot_blocked")
                except TelegramNetworkError:
                    await self._postpone(
                        item.id, retry_delay(item.attempts), "telegram_unavailable"
                    )
                except Exception:
                    await self._postpone(
                        item.id, retry_delay(item.attempts), "telegram_delivery_failed"
                    )
                else:
                    self.last_error = None
                    delivered_id = item.id

                    def acknowledge(current: AppState, delivered_id: str = delivered_id) -> None:
                        current.outbox = [
                            entry for entry in current.outbox if entry.id != delivered_id
                        ]

                    await self.repository.state.mutate(acknowledge)
        finally:
            self.running = False

    async def _postpone(self, item_id: str, delay: float, safe_error: str) -> None:
        self.last_error = safe_error
        retry_at = (
            (utc_now() + timedelta(seconds=max(1.0, delay))).isoformat().replace("+00:00", "Z")
        )

        def update(current: AppState) -> None:
            for entry in current.outbox:
                if entry.id == item_id:
                    entry.attempts += 1
                    entry.not_before = retry_at
                    break

        await self.repository.state.mutate(update)
