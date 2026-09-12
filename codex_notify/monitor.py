from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from .codex_rpc import CodexRpcError
from .domain import (
    analyze_changes,
    credit_expiry_events,
    make_event,
    normalize_snapshot,
    notification_enabled,
    render_event_bundle,
)
from .models import AppState, Event, OutboxItem, Settings
from .storage import Repository
from .timeutil import parse_utc, utc_now, utc_now_iso


class RpcClient(Protocol):
    async def request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class CheckResult:
    success: bool
    message: str
    new_events: int = 0
    retry_after_seconds: float | None = None


class Monitor:
    def __init__(
        self,
        repository: Repository,
        rpc: RpcClient,
        *,
        confirmation_delay: float = 2.0,
        manual_cooldown: float = 5.0,
    ) -> None:
        self.repository = repository
        self.rpc = rpc
        self.confirmation_delay = confirmation_delay
        self.manual_cooldown = manual_cooldown
        self._current: asyncio.Task[CheckResult] | None = None
        self._current_lock = asyncio.Lock()
        self._last_manual_started = float("-inf")
        self._schedule_wakeup = asyncio.Event()
        self.next_check_at: str | None = None

    async def check_now(self, *, manual: bool = False) -> CheckResult:
        async with self._current_lock:
            if self._current and not self._current.done():
                task = self._current
            elif manual and time.monotonic() - self._last_manual_started < self.manual_cooldown:
                return CheckResult(True, "Проверка уже выполнялась несколько секунд назад.")
            else:
                if manual:
                    self._last_manual_started = time.monotonic()
                task = asyncio.create_task(self._perform_check(), name="codex-check")
                self._current = task
        try:
            return await task
        finally:
            async with self._current_lock:
                if self._current is task and task.done():
                    self._current = None

    async def _perform_check(self) -> CheckResult:
        try:
            account_response = await self.rpc.request("account/read", {"refreshToken": False})
            account = account_response.get("account")
            if not isinstance(account, dict) or account.get("type") != "chatgpt":
                return await self._record_auth_required()

            raw = await self.rpc.request(
                "account/rateLimits/read", {"excludeResetCreditDetails": False}
            )
            snapshot, identity = normalize_snapshot(raw, account)
            state = await self.repository.state.get()
            settings = await self.repository.settings.get()

            account_changed = state.account is not None and state.account.key != identity.key
            previous = None if account_changed else state.snapshot
            baseline = None if account_changed else state.credit_baseline
            analysis = analyze_changes(previous, snapshot, baseline, now=utc_now())
            if analysis.requires_confirmation:
                await asyncio.sleep(self.confirmation_delay)
                confirm_raw = await self.rpc.request(
                    "account/rateLimits/read", {"excludeResetCreditDetails": False}
                )
                confirmed_snapshot, confirmed_identity = normalize_snapshot(confirm_raw, account)
                if confirmed_identity.key == identity.key:
                    snapshot = confirmed_snapshot
                    identity = confirmed_identity
                    analysis = analyze_changes(
                        previous, snapshot, baseline, now=utc_now(), confirmed=True
                    )

            detected_events = analysis.events
            if settings.notifications.credit_expiry_reminder:
                detected_events.extend(credit_expiry_events(snapshot, now=utc_now()))
            if account_changed:
                detected_events.insert(
                    0,
                    make_event(
                        "account_changed",
                        "Подключён другой Codex-аккаунт",
                        "Новый аккаунт принят как baseline; старые показания не сравниваются.",
                        "Идентификатор аккаунта изменился.",
                        {"account": identity.key},
                    ),
                )

            def commit(current: AppState) -> None:
                if account_changed:
                    current.outbox.clear()
                    current.credit_baseline = None
                known_ids = {event.id for event in current.events}
                new_events = [event for event in detected_events if event.id not in known_ids]
                current.account = identity
                current.auth_status = "connected"
                current.snapshot = snapshot
                current.credit_baseline = analysis.credit_baseline
                current.last_success_at = snapshot.observed_at
                current.last_error = None
                current.auth_required_notified = False

                if current.outage_notified:
                    recovered = make_event(
                        "monitor_recovered",
                        "Мониторинг восстановлен",
                        "Достоверные данные Codex снова получены.",
                        "Успешное чтение после ранее объявленного длительного сбоя.",
                        {"outage": current.outage_started_at or "unknown"},
                    )
                    if recovered.id not in known_ids:
                        new_events.append(recovered)
                current.consecutive_failures = 0
                current.outage_started_at = None
                current.outage_notified = False
                self._append_events_and_outbox(current, new_events, settings)
                current.bounded()

            saved = await self.repository.state.mutate(commit)
            count = len(
                [event for event in detected_events if event.id in {e.id for e in saved.events}]
            )
            return CheckResult(True, "Лимиты обновлены.", count)
        except asyncio.CancelledError:
            raise
        except CodexRpcError as exc:
            if exc.auth_related:
                return await self._record_auth_required()
            return await self._record_failure(retry_after=exc.retry_after)
        except Exception:
            return await self._record_failure()

    async def _record_auth_required(self) -> CheckResult:
        settings = await self.repository.settings.get()

        def commit(state: AppState) -> None:
            state.auth_status = "reauth_required" if state.account else "disconnected"
            state.last_error = "Требуется вход в Codex"
            if state.account and not state.auth_required_notified:
                event = make_event(
                    "auth_required",
                    "Требуется повторный вход в Codex",
                    "Автоматические проверки приостановлены. Откройте «Аккаунт» и войдите снова.",
                    "account/read вернул отсутствие ChatGPT-аккаунта; сетевые ошибки сюда не относятся.",
                    {"account": state.account.key, "state": "signed_out"},
                )
                self._append_events_and_outbox(state, [event], settings)
                state.auth_required_notified = True
            state.bounded()

        await self.repository.state.mutate(commit)
        return CheckResult(False, "Codex не подключён. Откройте «Аккаунт».")

    async def _record_failure(self, *, retry_after: float | None = None) -> CheckResult:
        settings = await self.repository.settings.get()

        def commit(state: AppState) -> None:
            state.consecutive_failures += 1
            state.last_error = "Не удалось получить данные Codex; сохранённый статус устарел"
            if state.outage_started_at is None:
                state.outage_started_at = utc_now_iso()
            if state.consecutive_failures >= 3 and not state.outage_notified:
                event = make_event(
                    "monitor_unavailable",
                    "Мониторинг временно недоступен",
                    "Три проверки подряд завершились ошибкой. Повторные одинаковые уведомления отключены.",
                    "Серия транспортных или RPC-ошибок; она не считается отзывом авторизации.",
                    {"outage": state.outage_started_at},
                )
                self._append_events_and_outbox(state, [event], settings)
                state.outage_notified = True
            state.bounded()

        await self.repository.state.mutate(commit)
        return CheckResult(
            False,
            "OpenAI сейчас недоступен. Сохранённые данные не изменены.",
            retry_after_seconds=retry_after,
        )

    @staticmethod
    def _append_events_and_outbox(state: AppState, events: list[Event], settings: Settings) -> None:
        existing_event_ids = set(state.dedupe_event_ids)
        unique = [event for event in events if event.id not in existing_event_ids]
        if not unique:
            return
        state.events.extend(unique)
        state.dedupe_event_ids.extend(event.id for event in unique)
        notify = [event for event in unique if notification_enabled(event, settings.notifications)]
        if not notify:
            return
        event_ids = [event.id for event in notify]
        digest = hashlib.sha256(json.dumps(event_ids).encode()).hexdigest()[:24]
        if any(item.id == digest for item in state.outbox):
            return
        state.outbox.append(
            OutboxItem(
                id=digest,
                event_ids=event_ids,
                text=render_event_bundle(notify),
                created_at=utc_now_iso(),
                detected_at=notify[0].detected_at,
            )
        )

    async def set_next_check(self, when: datetime | None) -> None:
        value = when.astimezone(UTC).isoformat().replace("+00:00", "Z") if when else None
        self.next_check_at = value

        def commit(state: AppState) -> None:
            state.next_check_at = value

        await self.repository.state.mutate(commit)

    def settings_changed(self) -> None:
        self._schedule_wakeup.set()

    async def scheduler_loop(self) -> None:
        first = True
        while True:
            settings = await self.repository.settings.get()
            state = await self.repository.state.get()
            result: CheckResult | None = None
            if (
                settings.owner_id is not None
                and not settings.paused
                and state.auth_status not in {"disconnected", "reauth_required"}
            ):
                if first or state.account is not None:
                    result = await self.check_now()
            first = False
            settings = await self.repository.settings.get()
            delay = float(settings.interval_minutes * 60)
            state = await self.repository.state.get()
            if result is not None and not result.success:
                delay = min(
                    delay,
                    retry_delay(state.consecutive_failures, base=30.0, maximum=900.0),
                )
                if result.retry_after_seconds is not None:
                    delay = max(delay, result.retry_after_seconds)
            if not settings.paused and state.snapshot:
                now_ts = int(utc_now().timestamp())
                candidates = [
                    window.resets_at
                    for bucket in state.snapshot.buckets
                    for window in bucket.windows
                    if window.resets_at is not None
                    and window.resets_at >= now_ts
                    and f"{window.key}:{window.resets_at}" not in state.post_reset_checks
                ]
                if candidates:
                    delay = min(delay, max(60.0, min(candidates) - now_ts + 60.0))
            await self.set_next_check(
                None if settings.paused else utc_now() + timedelta(seconds=delay)
            )
            self._schedule_wakeup.clear()
            try:
                async with asyncio.timeout(delay):
                    await self._schedule_wakeup.wait()
            except TimeoutError:
                pass


def retry_delay(attempt: int, *, base: float = 2.0, maximum: float = 300.0) -> float:
    ceiling = min(maximum, base * (2 ** min(attempt, 8)))
    return secrets.SystemRandom().uniform(ceiling / 2, ceiling)


def outbox_ready(not_before: str | None) -> bool:
    parsed = parse_utc(not_before)
    return parsed is None or parsed <= utc_now()
