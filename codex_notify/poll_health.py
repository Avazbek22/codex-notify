from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from aiogram.client.session.middlewares.base import (
    BaseRequestMiddleware,
    NextRequestMiddlewareType,
)
from aiogram.methods import GetUpdates, TelegramMethod
from aiogram.methods.base import Response, TelegramType

if TYPE_CHECKING:
    from aiogram import Bot

# Long polling returns at least every polling_timeout (10 seconds by default).
DEFAULT_MAX_AGE_SECONDS = 90.0


class PollHealth(BaseRequestMiddleware):
    """Records successful getUpdates calls so health reflects real polling.

    A revoked token, a network outage, or a second instance polling the same
    token (HTTP 409) makes getUpdates fail, the polling health check turns
    false, and Docker reports the container unhealthy.
    """

    def __init__(
        self,
        *,
        max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_age_seconds = max_age_seconds
        self.clock = clock
        self.last_success = clock()

    async def __call__(
        self,
        make_request: NextRequestMiddlewareType[TelegramType],
        bot: Bot,
        method: TelegramMethod[TelegramType],
    ) -> Response[TelegramType]:
        response = await make_request(bot, method)
        if isinstance(method, GetUpdates):
            self.last_success = self.clock()
        return response

    def healthy(self) -> bool:
        return self.clock() - self.last_success < self.max_age_seconds
