from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from .storage import Repository

_CLAIM_COMMAND = re.compile(r"^/start(?:@[A-Za-z0-9_]+)?\s+claim_[A-Za-z0-9_-]{32}$")


class OwnerAccessMiddleware(BaseMiddleware):
    """Reject non-owner updates before handlers, FSM work, or external calls."""

    def __init__(self, repository: Repository) -> None:
        self.repository = repository

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        settings = await self.repository.settings.get()
        if isinstance(event, Message):
            private = event.chat.type == "private"
            user_id = event.from_user.id if event.from_user else None
            is_claim = (
                settings.owner_id is None
                and private
                and user_id is not None
                and bool(_CLAIM_COMMAND.fullmatch((event.text or "").strip()))
            )
            if is_claim or (private and user_id == settings.owner_id):
                return await handler(event, data)
            return None
        if isinstance(event, CallbackQuery):
            message = event.message
            private = message is not None and message.chat.type == "private"
            if private and event.from_user.id == settings.owner_id:
                return await handler(event, data)
            await event.answer()
            return None
        return None
