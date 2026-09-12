from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from codex_notify.binding import consume_binding, create_binding, set_owner
from codex_notify.storage import Repository


@pytest.mark.asyncio
async def test_binding_is_hashed_one_time_and_replay_safe(tmp_path: Path) -> None:
    repository = Repository(tmp_path)
    await repository.initialize()
    token = await create_binding(repository.settings)
    raw = (tmp_path / "settings.json").read_text(encoding="utf-8")
    assert token not in raw
    assert hashlib.sha256(token.encode()).hexdigest() not in raw
    assert await consume_binding(repository.settings, token, 123456789)
    assert not await consume_binding(repository.settings, token, 999999999)
    assert (await repository.settings.get()).owner_id == 123456789


@pytest.mark.asyncio
async def test_owner_cannot_be_replaced(tmp_path: Path) -> None:
    repository = Repository(tmp_path)
    await repository.initialize()
    await set_owner(repository.settings, 1111)
    with pytest.raises(RuntimeError, match="replace"):
        await set_owner(repository.settings, 2222)
