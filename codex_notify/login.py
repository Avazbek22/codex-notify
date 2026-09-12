from __future__ import annotations

import asyncio
import hashlib
import hmac
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .models import AppState, PendingLogin
from .monitor import Monitor, RpcClient
from .storage import Repository
from .timeutil import utc_now_iso


class LoginAlreadyRunning(RuntimeError):
    pass


class LoginProtocolError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DeviceLogin:
    login_id: str
    verification_url: str
    user_code: str


@dataclass(frozen=True, slots=True)
class LoginCompletion:
    success: bool
    message_key: str


def validate_verification_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "auth.openai.com"
        or parsed.path.rstrip("/") != "/codex/device"
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise LoginProtocolError("Codex returned an unexpected device-login URL")
    return value


def login_callback_tag(login_id: str) -> str:
    return hashlib.sha256(f"codex-notify-login\0{login_id}".encode()).hexdigest()[:16]


class LoginManager:
    def __init__(
        self,
        repository: Repository,
        rpc: RpcClient,
        monitor: Monitor,
        *,
        timeout: float = 900,
    ) -> None:
        self.repository = repository
        self.rpc = rpc
        self.monitor = monitor
        self.timeout = timeout
        self._lock = asyncio.Lock()

    async def start(self) -> DeviceLogin:
        async with self._lock:
            state = await self.repository.state.get()
            if state.pending_login is not None:
                raise LoginAlreadyRunning
            response = await self.rpc.request("account/login/start", {"type": "chatgptDeviceCode"})
            if response.get("type") != "chatgptDeviceCode":
                raise LoginProtocolError("Codex returned an unexpected login flow")
            login_id = response.get("loginId")
            url = response.get("verificationUrl")
            code = response.get("userCode")
            if not isinstance(login_id, str) or not login_id:
                raise LoginProtocolError("Codex returned an incomplete device-login response")
            if not isinstance(url, str) or not url:
                raise LoginProtocolError("Codex returned an incomplete device-login response")
            if not isinstance(code, str) or not code:
                raise LoginProtocolError("Codex returned an incomplete device-login response")
            validated_url = validate_verification_url(url)

            def save_pending(current: AppState) -> None:
                current.pending_login = PendingLogin(login_id=login_id, started_at=utc_now_iso())

            await self.repository.state.mutate(save_pending)
            return DeviceLogin(login_id, validated_url, code)

    async def attach_message(self, login_id: str, chat_id: int, message_id: int) -> None:
        def attach(current: AppState) -> None:
            if current.pending_login and current.pending_login.login_id == login_id:
                current.pending_login.message_chat_id = chat_id
                current.pending_login.message_id = message_id

        await self.repository.state.mutate(attach)

    async def wait(self, login_id: str) -> LoginCompletion:
        try:
            notification_method = "account/login/completed"
            next_notification = getattr(self.rpc, "next_notification", None)
            if next_notification is None:
                raise LoginProtocolError("RPC adapter cannot receive login notifications")
            notification: dict[str, Any] = await next_notification(
                lambda message: (
                    message.get("method") == notification_method
                    and isinstance(message.get("params"), dict)
                    and message["params"].get("loginId") in {login_id, None}
                ),
                timeout=self.timeout,
            )
            params = notification.get("params", {})
            if not params.get("success"):
                await self._clear(login_id)
                return LoginCompletion(False, "login.rejected")
            await self._clear(login_id)
            result = await self.monitor.check_now(manual=True)
            if result.success:
                return LoginCompletion(True, "login.connected")
            return LoginCompletion(True, "login.connected_no_limits")
        except TimeoutError:
            await self.cancel(login_id)
            return LoginCompletion(False, "login.expired")
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._clear(login_id)
            return LoginCompletion(False, "login.interrupted")

    async def cancel(self, login_id: str | None = None) -> bool:
        async with self._lock:
            state = await self.repository.state.get()
            pending = state.pending_login
            if pending is None or (login_id is not None and pending.login_id != login_id):
                return False
            try:
                await self.rpc.request("account/login/cancel", {"loginId": pending.login_id})
            finally:
                await self._clear(pending.login_id)
            return True

    async def cancel_by_tag(self, tag: str) -> bool:
        state = await self.repository.state.get()
        pending = state.pending_login
        if pending is None or not hmac.compare_digest(login_callback_tag(pending.login_id), tag):
            return False
        return await self.cancel(pending.login_id)

    async def _clear(self, login_id: str) -> None:
        def clear(current: AppState) -> None:
            if current.pending_login and current.pending_login.login_id == login_id:
                current.pending_login = None

        await self.repository.state.mutate(clear)

    async def recover_interrupted(self) -> PendingLogin | None:
        state = await self.repository.state.get()
        pending = state.pending_login
        if pending is not None:
            await self._clear(pending.login_id)
        return pending
