from __future__ import annotations

import asyncio
from typing import Any

import pytest
from aiogram.methods import GetMe, GetUpdates

from codex_notify.poll_health import PollHealth


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def call(health: PollHealth, method: Any, *, fail: bool = False) -> Any:
    async def make_request(bot: Any, method: Any) -> Any:
        del bot, method
        if fail:
            raise ConnectionError("Conflict: terminated by other getUpdates request")
        return "response"

    return asyncio.run(health(make_request, None, method))  # type: ignore[arg-type]


def test_successful_get_updates_keeps_polling_healthy() -> None:
    clock = Clock()
    health = PollHealth(max_age_seconds=90, clock=clock)
    clock.now += 100
    assert not health.healthy()
    assert call(health, GetUpdates()) == "response"
    assert health.healthy()


def test_failed_get_updates_does_not_count() -> None:
    clock = Clock()
    health = PollHealth(max_age_seconds=90, clock=clock)
    clock.now += 100
    with pytest.raises(ConnectionError):
        call(health, GetUpdates(), fail=True)
    assert not health.healthy()


def test_other_methods_do_not_count_as_polling() -> None:
    clock = Clock()
    health = PollHealth(max_age_seconds=90, clock=clock)
    clock.now += 100
    call(health, GetMe())
    assert not health.healthy()
