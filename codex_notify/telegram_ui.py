from __future__ import annotations

import asyncio
import contextlib
import html
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from .binding import consume_binding
from .codex_rpc import CodexAppServer
from .config import AppConfig
from .delivery import OutboxDelivery
from .login import DeviceLogin, LoginAlreadyRunning, LoginManager, login_callback_tag
from .models import AppState, NotificationSettings, RateSnapshot, Settings
from .monitor import Monitor
from .storage import Repository
from .timeutil import from_unix, parse_utc
from .update_status import read_update_status

MAIN_BUTTONS = {
    "📊 Статус": "status",
    "🔄 Проверить сейчас": "check",
    "⚙️ Настройки": "settings",
    "🕘 История": "history",
    "👤 Аккаунт": "account",
    "❓ Помощь": "help",
}


class InputState(StatesGroup):
    interval = State()
    timezone = State()


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Статус"), KeyboardButton(text="🔄 Проверить сейчас")],
            [KeyboardButton(text="⚙️ Настройки"), KeyboardButton(text="🕘 История")],
            [KeyboardButton(text="👤 Аккаунт"), KeyboardButton(text="❓ Помощь")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _format_datetime(value: str | None, timezone: str) -> str:
    parsed = parse_utc(value)
    if parsed is None:
        return "нет данных"
    return parsed.astimezone(ZoneInfo(timezone)).strftime("%d.%m.%Y %H:%M %Z")


def _format_unix(value: int | None, timezone: str) -> str:
    parsed = from_unix(value)
    if parsed is None:
        return "не указано"
    return parsed.astimezone(ZoneInfo(timezone)).strftime("%d.%m.%Y %H:%M %Z")


def render_status(settings: Settings, state: AppState) -> str:
    lines = ["📊 <b>Codex Notify</b>"]
    if state.auth_status == "connected":
        lines.append("Подключение: ✅ активно")
    elif state.auth_status == "reauth_required":
        lines.append("Подключение: 🔐 нужен повторный вход")
    else:
        lines.append("Подключение: не настроено")
    if state.account:
        if state.account.masked_email:
            lines.append(f"Аккаунт: <code>{html.escape(state.account.masked_email)}</code>")
        if state.account.plan:
            lines.append(f"План: {html.escape(state.account.plan)}")

    snapshot = state.snapshot
    if snapshot:
        lines.append("")
        lines.extend(_render_snapshot(snapshot, settings.timezone))
    else:
        lines.extend(["", "Достоверных показаний пока нет."])
    if state.last_error:
        lines.extend(["", "⚠️ Сохранённые данные устарели.", html.escape(state.last_error)])
    lines.extend(
        [
            "",
            f"Последняя успешная проверка: {_format_datetime(state.last_success_at, settings.timezone)}",
            f"Следующая попытка: {_format_datetime(state.next_check_at, settings.timezone)}",
            f"Мониторинг: {'⏸ пауза' if settings.paused else '▶️ включён'} · {settings.interval_minutes} мин",
        ]
    )
    return "\n".join(lines)


def _render_snapshot(snapshot: RateSnapshot, timezone: str) -> list[str]:
    lines: list[str] = []
    if not snapshot.buckets:
        lines.append("Окна лимитов: данные не предоставлены.")
    for bucket in snapshot.buckets:
        label = html.escape(bucket.limit_name or bucket.limit_id)
        if not bucket.windows:
            if bucket.unlimited is True:
                lines.append(f"♾ <b>{label}</b>: без ограничения")
            else:
                lines.append(f"• <b>{label}</b>: параметры окна не предоставлены")
        for window in bucket.windows:
            kind = "основное" if window.window == "primary" else "дополнительное"
            remaining = max(0, 100 - window.used_percent)
            duration = (
                f", окно {window.window_duration_mins} мин"
                if window.window_duration_mins is not None
                else ""
            )
            lines.append(
                f"• <b>{label}</b> ({kind}{duration}): использовано {window.used_percent}%, "
                f"осталось {remaining}%"
            )
            lines.append(f"  Сброс: {_format_unix(window.resets_at, timezone)}")
        if bucket.reached_type:
            lines.append(f"  Ограничение backend: {html.escape(bucket.reached_type)}")

    if snapshot.ordinary_usage_status == "known":
        allowed = "доступно" if snapshot.ordinary_usage_allowed else "заблокировано"
        lines.append(f"Обычное использование: {allowed}")
    else:
        lines.append("Обычное использование: неизвестно")

    if snapshot.reset_credits_status == "known":
        lines.append(f"Reset-кредиты: {snapshot.reset_credits_available}")
        for credit in snapshot.reset_credit_details or []:
            if credit.status != "available":
                continue
            expiry = _format_unix(credit.expires_at, timezone)
            lines.append(f"  • {html.escape(credit.title or 'Reset-кредит')} · до {expiry}")
    elif snapshot.reset_credits_status == "unknown":
        lines.append("Reset-кредиты: временно неизвестно")
    else:
        lines.append("Reset-кредиты: не поддерживаются ответом сервера")
    return lines


class TelegramUI:
    def __init__(
        self,
        bot: Bot,
        repository: Repository,
        monitor: Monitor,
        login: LoginManager,
        rpc: CodexAppServer,
        delivery: OutboxDelivery,
        config: AppConfig,
    ) -> None:
        self.bot = bot
        self.repository = repository
        self.monitor = monitor
        self.login = login
        self.rpc = rpc
        self.delivery = delivery
        self.config = config
        self.router = Router(name="codex-notify")
        self._tasks: set[asyncio.Task[None]] = set()
        self._register()

    def _register(self) -> None:
        self.router.message.register(self.handle_start, CommandStart())
        self.router.message.register(self.handle_cancel_input, Command("cancel"))
        self.router.message.register(self.handle_interval_input, InputState.interval)
        self.router.message.register(self.handle_timezone_input, InputState.timezone)
        self.router.message.register(self.handle_message)
        self.router.callback_query.register(self.handle_callback)

    async def register_commands(self) -> None:
        await self.bot.set_my_commands(
            [
                BotCommand(command="status", description="Показать лимиты"),
                BotCommand(command="check", description="Проверить сейчас"),
                BotCommand(command="settings", description="Настройки мониторинга"),
                BotCommand(command="history", description="История событий"),
                BotCommand(command="account", description="Подключение Codex"),
                BotCommand(command="diagnostics", description="Безопасная диагностика"),
                BotCommand(command="help", description="Помощь"),
                BotCommand(command="cancel", description="Отменить ввод"),
            ]
        )

    async def _authorized_message(self, message: Message) -> bool:
        settings = await self.repository.settings.get()
        private = message.chat.type == "private"
        user = message.from_user
        allowed = private and settings.owner_id is not None and user is not None
        allowed = allowed and user is not None and user.id == settings.owner_id
        if not allowed:
            await message.answer("Доступ закрыт.")
        return bool(allowed)

    async def _authorized_callback(self, query: CallbackQuery) -> bool:
        settings = await self.repository.settings.get()
        message = query.message
        private = message is not None and message.chat.type == "private"
        allowed = (
            private and settings.owner_id is not None and query.from_user.id == settings.owner_id
        )
        if not allowed:
            await query.answer("Доступ закрыт.", show_alert=True)
        return bool(allowed)

    async def handle_start(self, message: Message) -> None:
        settings = await self.repository.settings.get()
        text = message.text or ""
        argument = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) == 2 else ""
        if (
            settings.owner_id is None
            and message.chat.type == "private"
            and message.from_user is not None
            and argument.startswith("claim_")
        ):
            accepted = await consume_binding(
                self.repository.settings, argument.removeprefix("claim_"), message.from_user.id
            )
            if accepted:
                await message.answer(
                    "✅ Этот Telegram-аккаунт назначен владельцем. Теперь откройте «Аккаунт» и "
                    "подключите Codex.",
                    reply_markup=main_keyboard(),
                )
                return
        if not await self._authorized_message(message):
            return
        await message.answer(
            "Здравствуйте! Я отслеживаю лимиты одного Codex-аккаунта. Выберите действие:",
            reply_markup=main_keyboard(),
        )

    async def handle_cancel_input(self, message: Message, state: FSMContext) -> None:
        if not await self._authorized_message(message):
            return
        await state.clear()
        await message.answer("Ввод отменён.", reply_markup=main_keyboard())

    async def handle_interval_input(self, message: Message, state: FSMContext) -> None:
        if not await self._authorized_message(message):
            return
        value = (message.text or "").strip()
        if not value.isdecimal() or not 5 <= int(value) <= 1440:
            await message.answer("Введите целое число от 5 до 1440 или /cancel.")
            return
        minutes = int(value)

        def update(settings: Settings) -> None:
            settings.interval_minutes = minutes

        await self.repository.settings.mutate(update)
        await state.clear()
        self.monitor.settings_changed()
        with contextlib.suppress(TelegramBadRequest):
            await message.delete()
        await message.answer(f"Интервал изменён: {minutes} мин.", reply_markup=main_keyboard())

    async def handle_timezone_input(self, message: Message, state: FSMContext) -> None:
        if not await self._authorized_message(message):
            return
        value = (message.text or "").strip()
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            await message.answer("Неизвестная IANA timezone. Пример: Europe/Moscow. Или /cancel.")
            return

        def update(settings: Settings) -> None:
            settings.timezone = value

        await self.repository.settings.mutate(update)
        await state.clear()
        with contextlib.suppress(TelegramBadRequest):
            await message.delete()
        await message.answer(f"Часовой пояс: {html.escape(value)}.", reply_markup=main_keyboard())

    async def handle_message(self, message: Message) -> None:
        if not await self._authorized_message(message):
            return
        text = message.text or ""
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        action = MAIN_BUTTONS.get(text)
        command_map = {
            "/status": "status",
            "/check": "check",
            "/settings": "settings",
            "/history": "history",
            "/account": "account",
            "/help": "help",
            "/diagnostics": "diagnostics",
        }
        action = action or command_map.get(command)
        if action == "status":
            await self.show_status(message)
        elif action == "check":
            waiting = await message.answer("Проверяю…")
            result = await self.monitor.check_now(manual=True)
            self.delivery.wake()
            settings = await self.repository.settings.get()
            state = await self.repository.state.get()
            await waiting.edit_text(f"{result.message}\n\n{render_status(settings, state)}")
        elif action == "settings":
            await message.answer(
                await self.settings_text(), reply_markup=await self.settings_keyboard()
            )
        elif action == "history":
            await self.show_history(message, 0)
        elif action == "account":
            await message.answer(
                await self.account_text(), reply_markup=await self.account_keyboard()
            )
        elif action == "diagnostics":
            await message.answer(await self.diagnostics_text())
        elif action == "help":
            await message.answer(self.help_text(), reply_markup=main_keyboard())
        else:
            await message.answer("Используйте кнопки меню.", reply_markup=main_keyboard())

    async def show_status(self, message: Message) -> None:
        await message.answer(
            render_status(await self.repository.settings.get(), await self.repository.state.get())
        )

    async def settings_text(self) -> str:
        settings = await self.repository.settings.get()
        return (
            "⚙️ <b>Настройки</b>\n"
            f"Интервал: {settings.interval_minutes} мин\n"
            f"Часовой пояс: {html.escape(settings.timezone)}\n"
            f"Мониторинг: {'пауза' if settings.paused else 'включён'}\n\n"
            "Уведомления переключаются кнопками ниже."
        )

    async def settings_keyboard(self) -> InlineKeyboardMarkup:
        settings = await self.repository.settings.get()
        n = settings.notifications

        def mark(enabled: bool) -> str:
            return "✅" if enabled else "▫️"

        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="5 мин", callback_data="v1:int:5"),
                    InlineKeyboardButton(text="15 мин", callback_data="v1:int:15"),
                    InlineKeyboardButton(text="30 мин", callback_data="v1:int:30"),
                    InlineKeyboardButton(text="60 мин", callback_data="v1:int:60"),
                ],
                [InlineKeyboardButton(text="Свой интервал", callback_data="v1:int:custom")],
                [InlineKeyboardButton(text="Часовой пояс", callback_data="v1:timezone")],
                [
                    InlineKeyboardButton(
                        text="▶️ Возобновить" if settings.paused else "⏸ Пауза",
                        callback_data="v1:pause",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=f"{mark(n.window_updates)} Обновления окон",
                        callback_data="v1:notif:window_updates",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=f"{mark(n.usage_restored)} Восстановление",
                        callback_data="v1:notif:usage_restored",
                    ),
                    InlineKeyboardButton(
                        text=f"{mark(n.reset_credits)} Reset-кредиты",
                        callback_data="v1:notif:reset_credits",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=f"{mark(n.significant_changes)} Другие изменения",
                        callback_data="v1:notif:significant_changes",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=f"{mark(n.credit_expiry_reminder)} Срок reset-кредита",
                        callback_data="v1:notif:credit_expiry_reminder",
                    )
                ],
                [InlineKeyboardButton(text="← Назад", callback_data="v1:close")],
            ]
        )

    async def account_text(self) -> str:
        state = await self.repository.state.get()
        if state.account and state.auth_status == "connected":
            email = state.account.masked_email or "email недоступен"
            plan = state.account.plan or "план неизвестен"
            return f"👤 <b>Codex-аккаунт</b>\n{html.escape(email)}\nПлан: {html.escape(plan)}"
        return "👤 <b>Codex-аккаунт</b>\nАккаунт не подключён."

    async def account_keyboard(self) -> InlineKeyboardMarkup:
        state = await self.repository.state.get()
        rows: list[list[InlineKeyboardButton]] = []
        if state.auth_status == "connected":
            rows.append(
                [InlineKeyboardButton(text="Выйти из Codex", callback_data="v1:logout:ask")]
            )
        else:
            rows.append(
                [InlineKeyboardButton(text="🔐 Подключить Codex", callback_data="v1:login")]
            )
        rows.append([InlineKeyboardButton(text="← Назад", callback_data="v1:close")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    async def diagnostics_text(self) -> str:
        state = await self.repository.state.get()
        codex_state = "работает" if self.rpc.running else "перезапускается"
        delivery = "работает" if self.delivery.running else "остановлена"
        update_status = read_update_status(self.config.data_dir / "update-status.json")
        return (
            "🧰 <b>Диагностика</b>\n"
            f"Версия: {html.escape(self.config.version)}\n"
            f"Commit: <code>{html.escape(self.config.commit)}</code>\n"
            "Codex: 0.154.0\n"
            f"App Server: {codex_state}\n"
            f"Планировщик: {'работает' if self.monitor.next_check_at is not None else 'ожидает'}\n"
            f"Доставка: {delivery}\n"
            f"Очередь: {len(state.outbox)}\n"
            f"Обновление: {html.escape(update_status)}\n"
            f"Последняя ошибка: {html.escape(state.last_error or self.delivery.last_error or 'нет')}"
        )

    @staticmethod
    def help_text() -> str:
        return (
            "❓ <b>Помощь</b>\n"
            "«Статус» показывает последние достоверные данные. «Проверить сейчас» выполняет чтение "
            "без запроса к модели. При паузе ручная проверка доступна.\n\n"
            "Для входа откройте «Аккаунт» → «Подключить Codex». Device-code вход может требовать "
            "разрешения в настройках безопасности ChatGPT или от администратора workspace. Никогда "
            "не присылайте боту пароль, cookies, токены или auth.json."
        )

    async def show_history(self, message: Message, page: int) -> None:
        state = await self.repository.state.get()
        settings = await self.repository.settings.get()
        page_size = 5
        events = list(reversed(state.events))
        pages = max(1, (len(events) + page_size - 1) // page_size)
        page = min(max(page, 0), pages - 1)
        selected = events[page * page_size : (page + 1) * page_size]
        if selected:
            body = "\n\n".join(
                f"<b>{html.escape(event.title)}</b>\n{html.escape(event.details)}\n"
                f"{_format_datetime(event.detected_at, settings.timezone)}"
                for event in selected
            )
        else:
            body = "Событий пока нет. Первое наблюдение становится baseline."
        buttons: list[InlineKeyboardButton] = []
        if page > 0:
            buttons.append(InlineKeyboardButton(text="←", callback_data=f"v1:hist:{page - 1}"))
        buttons.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="v1:noop"))
        if page + 1 < pages:
            buttons.append(InlineKeyboardButton(text="→", callback_data=f"v1:hist:{page + 1}"))
        await message.answer(
            f"🕘 <b>История</b>\n\n{body}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons]),
        )

    async def handle_callback(self, query: CallbackQuery, state: FSMContext) -> None:
        if not await self._authorized_callback(query):
            return
        data = query.data or ""
        message = query.message
        if not isinstance(message, Message):
            await query.answer()
            return
        if not data.startswith("v1:"):
            await query.answer("Эта кнопка устарела. Откройте меню заново.", show_alert=True)
            return
        parts = data.split(":")
        action = parts[1]
        if action == "int" and len(parts) == 3:
            if parts[2] == "custom":
                await state.set_state(InputState.interval)
                await message.answer("Введите интервал от 5 до 1440 минут или /cancel.")
            else:
                if not parts[2].isdecimal() or not 5 <= int(parts[2]) <= 1440:
                    await query.answer("Эта кнопка устарела.", show_alert=True)
                    return
                minutes = int(parts[2])

                def update(settings: Settings) -> None:
                    settings.interval_minutes = minutes

                await self.repository.settings.mutate(update)
                self.monitor.settings_changed()
                await self._edit_settings(message)
        elif action == "timezone":
            await state.set_state(InputState.timezone)
            await message.answer("Введите IANA timezone, например Europe/Moscow, или /cancel.")
        elif action == "pause":

            def toggle(settings: Settings) -> None:
                settings.paused = not settings.paused

            await self.repository.settings.mutate(toggle)
            self.monitor.settings_changed()
            await self._edit_settings(message)
        elif action == "notif" and len(parts) == 3:
            field = parts[2]
            allowed = set(NotificationSettings.model_fields)
            if field not in allowed:
                await query.answer("Эта кнопка устарела.", show_alert=True)
                return

            def toggle_notification(settings: Settings) -> None:
                current = getattr(settings.notifications, field)
                setattr(settings.notifications, field, not current)

            await self.repository.settings.mutate(toggle_notification)
            await self._edit_settings(message)
        elif action == "hist" and len(parts) == 3:
            if not parts[2].isdecimal():
                await query.answer("Эта кнопка устарела.", show_alert=True)
                return
            await self._edit_history(message, int(parts[2]))
        elif action == "login":
            await self._start_login(message)
        elif action == "login_cancel" and len(parts) == 3:
            cancelled = await self.login.cancel_by_tag(parts[2])
            await message.edit_text("Вход отменён." if cancelled else "Этот вход уже завершён.")
        elif action == "logout" and len(parts) == 3 and parts[2] == "ask":
            await message.edit_text(
                "Выйти из Codex? Авторизация будет удалена официальным CLI.",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(text="Да, выйти", callback_data="v1:logout:yes"),
                            InlineKeyboardButton(text="Отмена", callback_data="v1:close"),
                        ]
                    ]
                ),
            )
        elif action == "logout" and len(parts) == 3 and parts[2] == "yes":
            await self.login.cancel()
            try:
                await self.rpc.request("account/logout")
            except Exception:
                await query.answer("Не удалось выйти из Codex. Повторите позже.", show_alert=True)
                return

            def logout(current: AppState) -> None:
                current.account = None
                current.auth_status = "disconnected"
                current.snapshot = None
                current.credit_baseline = None
                current.outbox.clear()
                current.pending_login = None
                current.auth_required_notified = False

            await self.repository.state.mutate(logout)
            self.monitor.settings_changed()
            await message.edit_text("Вы вышли из Codex. Старые показания больше не сравниваются.")
        elif action == "close":
            await message.edit_reply_markup(reply_markup=None)
        elif action == "noop":
            pass
        else:
            await query.answer("Эта кнопка устарела. Откройте меню заново.", show_alert=True)
            return
        await query.answer()

    async def _edit_settings(self, message: Message) -> None:
        with contextlib.suppress(TelegramBadRequest):
            await message.edit_text(
                await self.settings_text(), reply_markup=await self.settings_keyboard()
            )

    async def _edit_history(self, message: Message, page: int) -> None:
        state = await self.repository.state.get()
        settings = await self.repository.settings.get()
        events = list(reversed(state.events))
        page_size = 5
        pages = max(1, (len(events) + page_size - 1) // page_size)
        page = min(max(page, 0), pages - 1)
        selected = events[page * page_size : (page + 1) * page_size]
        body = (
            "\n\n".join(
                f"<b>{html.escape(event.title)}</b>\n{html.escape(event.details)}\n"
                f"{_format_datetime(event.detected_at, settings.timezone)}"
                for event in selected
            )
            or "Событий пока нет."
        )
        buttons: list[InlineKeyboardButton] = []
        if page > 0:
            buttons.append(InlineKeyboardButton(text="←", callback_data=f"v1:hist:{page - 1}"))
        buttons.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="v1:noop"))
        if page + 1 < pages:
            buttons.append(InlineKeyboardButton(text="→", callback_data=f"v1:hist:{page + 1}"))
        await message.edit_text(
            f"🕘 <b>История</b>\n\n{body}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[buttons]),
        )

    async def _start_login(self, message: Message) -> None:
        try:
            device = await self.login.start()
        except LoginAlreadyRunning as exc:
            await message.answer(str(exc))
            return
        except Exception:
            await message.answer("Не удалось начать официальный вход. Повторите позже.")
            return
        login_message = await message.answer(
            "🔐 <b>Вход в Codex</b>\n"
            f"1. Откройте официальную страницу: {html.escape(device.verification_url)}\n"
            f"2. Введите одноразовый код: <code>{html.escape(device.user_code)}</code>\n\n"
            "Device-code вход может требовать разрешения в настройках безопасности ChatGPT или "
            "от администратора workspace. Не отправляйте сюда пароль или токены.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [InlineKeyboardButton(text="Открыть OpenAI", url=device.verification_url)],
                    [
                        InlineKeyboardButton(
                            text="Отменить вход",
                            callback_data=f"v1:login_cancel:{login_callback_tag(device.login_id)}",
                        )
                    ],
                ]
            ),
        )
        await self.login.attach_message(
            device.login_id, login_message.chat.id, login_message.message_id
        )
        task = asyncio.create_task(
            self._finish_login(device, login_message), name=f"login-{device.login_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _finish_login(self, device: DeviceLogin, message: Message) -> None:
        completion = await self.login.wait(device.login_id)
        self.delivery.wake()
        with contextlib.suppress(TelegramBadRequest):
            await message.edit_text(completion.message)

    async def recover_interrupted_login(self) -> None:
        pending = await self.login.recover_interrupted()
        if pending is None:
            return
        if pending.message_chat_id and pending.message_id:
            with contextlib.suppress(TelegramBadRequest):
                await self.bot.edit_message_text(
                    "Поток входа был прерван перезапуском. Код больше не используется; начните вход снова.",
                    chat_id=pending.message_chat_id,
                    message_id=pending.message_id,
                )

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
