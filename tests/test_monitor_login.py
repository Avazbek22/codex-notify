from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from codex_notify.binding import set_owner
from codex_notify.login import LoginManager, login_callback_tag, validate_verification_url
from codex_notify.models import AppState, OutboxItem
from codex_notify.monitor import Monitor
from codex_notify.storage import Repository
from codex_notify.timeutil import utc_now_iso

from .conftest import rate_payload


class FakeRpc:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.gate: asyncio.Event | None = None
        self.fail = False
        self.login_success = True
        self.timeout_login = False
        self.account_id = "acct-A"

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.calls.append(method)
        if self.fail:
            raise OSError("network details that must not be persisted")
        if self.gate and method == "account/read":
            await self.gate.wait()
        if method == "account/read":
            return {
                "account": {"type": "chatgpt", "email": "person@example.com", "planType": "plus"},
                "requiresOpenaiAuth": True,
            }
        if method == "account/rateLimits/read":
            return rate_payload(
                account_id=self.account_id, credits={"availableCount": 0, "credits": []}
            )
        if method == "account/login/start":
            return {
                "type": "chatgptDeviceCode",
                "loginId": "login-1",
                "verificationUrl": "https://auth.openai.com/codex/device",
                "userCode": "CODE-1234",
            }
        return {}

    async def next_notification(self, predicate: Any, *, timeout: float) -> dict[str, Any]:
        if self.timeout_login:
            raise TimeoutError
        value = {
            "method": "account/login/completed",
            "params": {"loginId": "login-1", "success": self.login_success, "error": None},
        }
        assert predicate(value)
        return value


@pytest.mark.asyncio
async def test_simultaneous_manual_checks_join_one_rpc_read(tmp_path: Path) -> None:
    repository = Repository(tmp_path)
    await repository.initialize()
    rpc = FakeRpc()
    rpc.gate = asyncio.Event()
    monitor = Monitor(repository, rpc, confirmation_delay=0)
    first = asyncio.create_task(monitor.check_now(manual=True))
    second = asyncio.create_task(monitor.check_now(manual=True))
    await asyncio.sleep(0)
    rpc.gate.set()
    await asyncio.gather(first, second)
    assert rpc.calls.count("account/read") == 1
    assert rpc.calls.count("account/rateLimits/read") == 1


@pytest.mark.asyncio
async def test_first_login_success_sets_baseline_without_notification(tmp_path: Path) -> None:
    repository = Repository(tmp_path)
    await repository.initialize()
    rpc = FakeRpc()
    monitor = Monitor(repository, rpc, confirmation_delay=0)
    login = LoginManager(repository, rpc, monitor, timeout=1)
    device = await login.start()
    assert "CODE-1234" not in (tmp_path / "state.json").read_text(encoding="utf-8")
    completion = await login.wait(device.login_id)
    state = await repository.state.get()
    assert completion.success
    assert state.auth_status == "connected"
    assert state.snapshot is not None
    assert state.events == []
    assert state.outbox == []


@pytest.mark.asyncio
async def test_login_cancel_and_expiry_clear_persisted_flow(tmp_path: Path) -> None:
    repository = Repository(tmp_path)
    await repository.initialize()
    rpc = FakeRpc()
    monitor = Monitor(repository, rpc)
    login = LoginManager(repository, rpc, monitor, timeout=0.01)
    device = await login.start()
    assert await login.cancel(device.login_id)
    assert (await repository.state.get()).pending_login is None
    assert "account/login/cancel" in rpc.calls

    rpc.timeout_login = True
    device = await login.start()
    completion = await login.wait(device.login_id)
    assert not completion.success
    assert (await repository.state.get()).pending_login is None


@pytest.mark.asyncio
async def test_stale_login_cancel_button_cannot_cancel_current_flow(tmp_path: Path) -> None:
    repository = Repository(tmp_path)
    await repository.initialize()
    rpc = FakeRpc()
    login = LoginManager(repository, rpc, Monitor(repository, rpc))
    device = await login.start()
    assert not await login.cancel_by_tag("0" * 16)
    assert (await repository.state.get()).pending_login is not None
    assert await login.cancel_by_tag(login_callback_tag(device.login_id))
    assert (await repository.state.get()).pending_login is None


def test_device_login_url_is_strictly_official() -> None:
    assert validate_verification_url("https://auth.openai.com/codex/device")
    with pytest.raises(Exception, match="unexpected"):
        validate_verification_url("https://evil.example/codex/device")


@pytest.mark.asyncio
async def test_three_network_failures_queue_one_notice_then_recovery(tmp_path: Path) -> None:
    repository = Repository(tmp_path)
    await repository.initialize()
    await set_owner(repository.settings, 1234)
    rpc = FakeRpc()
    monitor = Monitor(repository, rpc, confirmation_delay=0, manual_cooldown=0)
    rpc.fail = True
    for _ in range(4):
        await monitor.check_now()
    state = await repository.state.get()
    assert len([event for event in state.events if event.type == "monitor_unavailable"]) == 1
    assert len(state.outbox) == 1
    assert "network details" not in (tmp_path / "state.json").read_text(encoding="utf-8")

    rpc.fail = False
    await monitor.check_now()
    state = await repository.state.get()
    assert len([event for event in state.events if event.type == "monitor_recovered"]) == 1


@pytest.mark.asyncio
async def test_paused_scheduler_does_not_poll_but_manual_check_works(tmp_path: Path) -> None:
    repository = Repository(tmp_path)
    await repository.initialize()
    await set_owner(repository.settings, 1234)
    await repository.settings.mutate(lambda settings: setattr(settings, "paused", True))
    rpc = FakeRpc()
    monitor = Monitor(repository, rpc, confirmation_delay=0)
    task = asyncio.create_task(monitor.scheduler_loop())
    await asyncio.sleep(0.02)
    assert rpc.calls == []
    assert (await repository.state.get()).next_check_at is None
    await monitor.check_now(manual=True)
    assert "account/read" in rpc.calls
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_interval_change_persists_and_wakes_scheduler(tmp_path: Path) -> None:
    repository = Repository(tmp_path)
    await repository.initialize()
    rpc = FakeRpc()
    monitor = Monitor(repository, rpc)
    await repository.settings.mutate(lambda settings: setattr(settings, "interval_minutes", 15))
    monitor.settings_changed()
    assert (await repository.settings.get()).interval_minutes == 15
    assert monitor._schedule_wakeup.is_set()


@pytest.mark.asyncio
async def test_account_change_discards_old_outbox_and_rebaselines(tmp_path: Path) -> None:
    repository = Repository(tmp_path)
    await repository.initialize()
    rpc = FakeRpc()
    monitor = Monitor(repository, rpc, confirmation_delay=0, manual_cooldown=0)
    await monitor.check_now()

    def add_old(state: AppState) -> None:
        state.outbox.append(
            OutboxItem(
                id="old-account-message",
                event_ids=[],
                legacy_text="old",
                created_at=utc_now_iso(),
                detected_at=utc_now_iso(),
            )
        )

    await repository.state.mutate(add_old)
    rpc.account_id = "acct-B"
    await monitor.check_now()
    state = await repository.state.get()
    assert all(item.id != "old-account-message" for item in state.outbox)
    assert any(event.type == "account_changed" for event in state.events)
