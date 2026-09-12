from __future__ import annotations

import asyncio
import contextlib
import html
import logging
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from aiogram import Bot, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)

from .access import OwnerAccessMiddleware
from .binding import consume_binding
from .codex_rpc import CodexAppServer
from .config import AppConfig
from .delivery import OutboxDelivery
from .i18n import SUPPORTED_LANGUAGES, has_translation, tr
from .login import DeviceLogin, LoginAlreadyRunning, LoginManager, login_callback_tag
from .models import AppState, Language, NotificationSettings, Settings
from .monitor import Monitor
from .presentation import format_datetime, render_details, render_event, render_status
from .storage import Repository
from .update_status import read_update_status

_COMMAND_ACTIONS = {
    "/status": "status",
    "/check": "check",
    "/settings": "settings",
    "/history": "history",
    "/account": "account",
    "/help": "help",
    "/diagnostics": "diagnostics",
}
_LOGGER = logging.getLogger(__name__)


class InputState(StatesGroup):
    interval = State()
    timezone = State()


def _button_actions() -> dict[str, str]:
    actions: dict[str, str] = {}
    for language in SUPPORTED_LANGUAGES:
        actions.update(
            {
                tr(language, "menu.status"): "status",
                tr(language, "menu.check"): "check",
                tr(language, "menu.settings"): "settings",
                tr(language, "menu.history"): "history",
                tr(language, "menu.account"): "account",
                tr(language, "menu.help"): "help",
                tr(language, "legacy.menu.check.v1"): "check",
            }
        )
    return actions


MAIN_BUTTONS = _button_actions()


def main_keyboard(language: Language) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text=tr(language, "menu.status")),
                KeyboardButton(text=tr(language, "menu.check")),
            ],
            [
                KeyboardButton(text=tr(language, "menu.settings")),
                KeyboardButton(text=tr(language, "menu.history")),
            ],
            [
                KeyboardButton(text=tr(language, "menu.account")),
                KeyboardButton(text=tr(language, "menu.help")),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def status_keyboard(language: Language) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=tr(language, "status.refresh"), callback_data="v2:refresh"
                ),
                InlineKeyboardButton(
                    text=tr(language, "status.details"), callback_data="v2:details"
                ),
            ]
        ]
    )


def details_keyboard(language: Language) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=tr(language, "settings.back"), callback_data="v2:status")]
        ]
    )


def _commands(language: Language) -> list[BotCommand]:
    return [
        BotCommand(command="status", description=tr(language, "command.status")),
        BotCommand(command="check", description=tr(language, "command.check")),
        BotCommand(command="settings", description=tr(language, "command.settings")),
        BotCommand(command="history", description=tr(language, "command.history")),
        BotCommand(command="account", description=tr(language, "command.account")),
        BotCommand(command="diagnostics", description=tr(language, "command.diagnostics")),
        BotCommand(command="help", description=tr(language, "command.help")),
        BotCommand(command="cancel", description=tr(language, "command.cancel")),
    ]


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
        access = OwnerAccessMiddleware(repository)
        self.router.message.outer_middleware(access)
        self.router.callback_query.outer_middleware(access)
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
        # Earlier releases installed commands globally. Remove that scope so strangers see none.
        await self.bot.delete_my_commands()
        settings = await self.repository.settings.get()
        if settings.owner_id is not None:
            try:
                await self.bot.set_my_commands(
                    _commands(settings.language),
                    scope=BotCommandScopeChat(chat_id=settings.owner_id),
                )
            except TelegramBadRequest:
                # A directly configured owner may not have opened the bot yet. Polling must still
                # start; /start retries registration after Telegram knows the private chat.
                _LOGGER.info(
                    "Owner-scoped commands will be registered after the owner opens the bot"
                )

    async def _authorized_message(self, message: Message) -> bool:
        settings = await self.repository.settings.get()
        user = message.from_user
        return bool(
            message.chat.type == "private"
            and settings.owner_id is not None
            and user is not None
            and user.id == settings.owner_id
        )

    async def _authorized_callback(self, query: CallbackQuery) -> bool:
        settings = await self.repository.settings.get()
        message = query.message
        allowed = bool(
            message is not None
            and message.chat.type == "private"
            and settings.owner_id is not None
            and query.from_user.id == settings.owner_id
        )
        if not allowed:
            await query.answer()
        return allowed

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
                self.repository.settings,
                argument.removeprefix("claim_"),
                message.from_user.id,
            )
            if accepted:
                settings = await self.repository.settings.get()
                await self.register_commands()
                await message.answer(
                    tr(settings.language, "start.bound"),
                    reply_markup=main_keyboard(settings.language),
                )
                return
        if not await self._authorized_message(message):
            return
        settings = await self.repository.settings.get()
        await self.register_commands()
        await message.answer(
            tr(settings.language, "start.welcome"),
            reply_markup=main_keyboard(settings.language),
        )

    async def handle_cancel_input(self, message: Message, state: FSMContext) -> None:
        if not await self._authorized_message(message):
            return
        settings = await self.repository.settings.get()
        await state.clear()
        await message.answer(
            tr(settings.language, "input.cancelled"),
            reply_markup=main_keyboard(settings.language),
        )

    async def handle_interval_input(self, message: Message, state: FSMContext) -> None:
        if not await self._authorized_message(message):
            return
        settings = await self.repository.settings.get()
        value = (message.text or "").strip()
        if not value.isdecimal() or not 5 <= int(value) <= 1440:
            await message.answer(tr(settings.language, "input.interval_invalid"))
            return
        minutes = int(value)

        def update(current: Settings) -> None:
            current.interval_minutes = minutes

        settings = await self.repository.settings.mutate(update)
        await state.clear()
        self.monitor.settings_changed()
        with contextlib.suppress(TelegramBadRequest):
            await message.delete()
        await message.answer(
            tr(settings.language, "input.interval_changed", minutes=minutes),
            reply_markup=main_keyboard(settings.language),
        )

    async def handle_timezone_input(self, message: Message, state: FSMContext) -> None:
        if not await self._authorized_message(message):
            return
        settings = await self.repository.settings.get()
        value = (message.text or "").strip()
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            await message.answer(tr(settings.language, "input.timezone_invalid"))
            return

        def update(current: Settings) -> None:
            current.timezone = value

        settings = await self.repository.settings.mutate(update)
        await state.clear()
        with contextlib.suppress(TelegramBadRequest):
            await message.delete()
        await message.answer(
            tr(settings.language, "input.timezone_changed", timezone=html.escape(value)),
            reply_markup=main_keyboard(settings.language),
        )

    async def handle_message(self, message: Message) -> None:
        if not await self._authorized_message(message):
            return
        settings = await self.repository.settings.get()
        text = message.text or ""
        command = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        action = MAIN_BUTTONS.get(text) or _COMMAND_ACTIONS.get(command)
        if action == "status":
            await self.show_status(message)
        elif action == "check":
            waiting = await message.answer(tr(settings.language, "check.checking"))
            result = await self.monitor.check_now(manual=True)
            self.delivery.wake()
            settings = await self.repository.settings.get()
            app_state = await self.repository.state.get()
            await waiting.edit_text(
                self._checked_status(settings, app_state, result.message_key),
                reply_markup=status_keyboard(settings.language),
            )
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
            await message.answer(
                tr(settings.language, "help.text"),
                reply_markup=main_keyboard(settings.language),
            )
        else:
            await message.answer(
                tr(settings.language, "input.use_buttons"),
                reply_markup=main_keyboard(settings.language),
            )

    async def show_status(self, message: Message) -> None:
        settings = await self.repository.settings.get()
        await message.answer(
            render_status(settings, await self.repository.state.get()),
            reply_markup=status_keyboard(settings.language),
        )

    @staticmethod
    def _checked_status(settings: Settings, state: AppState, message_key: str) -> str:
        status = render_status(settings, state)
        if message_key == "check.updated":
            return status
        return f"{tr(settings.language, message_key)}\n\n{status}"

    async def settings_text(self) -> str:
        settings = await self.repository.settings.get()
        language = settings.language
        monitoring_key = (
            "settings.monitoring_paused" if settings.paused else "settings.monitoring_on"
        )
        return "\n".join(
            [
                tr(language, "settings.title"),
                tr(language, "settings.interval", minutes=settings.interval_minutes),
                tr(language, "settings.timezone", timezone=html.escape(settings.timezone)),
                tr(language, "settings.language"),
                tr(language, monitoring_key),
                "",
                tr(language, "settings.notifications_hint"),
            ]
        )

    async def settings_keyboard(self) -> InlineKeyboardMarkup:
        settings = await self.repository.settings.get()
        language = settings.language
        notifications = settings.notifications

        def mark(enabled: bool) -> str:
            return "✅" if enabled else "▫️"

        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="5m", callback_data="v2:int:5"),
                    InlineKeyboardButton(text="15m", callback_data="v2:int:15"),
                    InlineKeyboardButton(text="30m", callback_data="v2:int:30"),
                    InlineKeyboardButton(text="60m", callback_data="v2:int:60"),
                ],
                [
                    InlineKeyboardButton(
                        text=tr(language, "settings.custom_interval"),
                        callback_data="v2:int:custom",
                    ),
                    InlineKeyboardButton(
                        text=tr(language, "settings.timezone_button"),
                        callback_data="v2:timezone",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=tr(language, "settings.language_button"),
                        callback_data="v2:language",
                    ),
                    InlineKeyboardButton(
                        text=tr(
                            language,
                            "settings.resume" if settings.paused else "settings.pause",
                        ),
                        callback_data="v2:pause",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=f"{mark(notifications.window_updates)} "
                        f"{tr(language, 'settings.window_updates')}",
                        callback_data="v2:notif:window_updates",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=f"{mark(notifications.usage_restored)} "
                        f"{tr(language, 'settings.usage_restored')}",
                        callback_data="v2:notif:usage_restored",
                    ),
                    InlineKeyboardButton(
                        text=f"{mark(notifications.reset_credits)} "
                        f"{tr(language, 'settings.reset_credits')}",
                        callback_data="v2:notif:reset_credits",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text=f"{mark(notifications.significant_changes)} "
                        f"{tr(language, 'settings.significant_changes')}",
                        callback_data="v2:notif:significant_changes",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=f"{mark(notifications.credit_expiry_reminder)} "
                        f"{tr(language, 'settings.credit_expiry')}",
                        callback_data="v2:notif:credit_expiry_reminder",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text=tr(language, "settings.back"), callback_data="v2:close"
                    )
                ],
            ]
        )

    async def account_text(self) -> str:
        settings = await self.repository.settings.get()
        state = await self.repository.state.get()
        language = settings.language
        lines = [tr(language, "account.title")]
        if state.account and state.auth_status == "connected":
            lines.append(
                html.escape(state.account.masked_email or tr(language, "account.email_unavailable"))
            )
            lines.append(
                tr(
                    language,
                    "account.plan",
                    plan=html.escape(state.account.plan or tr(language, "account.plan_unknown")),
                )
            )
        else:
            lines.append(tr(language, "account.not_connected"))
        return "\n".join(lines)

    async def account_keyboard(self) -> InlineKeyboardMarkup:
        settings = await self.repository.settings.get()
        state = await self.repository.state.get()
        language = settings.language
        action = "logout" if state.auth_status == "connected" else "login"
        key = "account.logout" if action == "logout" else "account.connect"
        callback = "v2:logout:ask" if action == "logout" else "v2:login"
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=tr(language, key), callback_data=callback)],
                [
                    InlineKeyboardButton(
                        text=tr(language, "settings.back"), callback_data="v2:close"
                    )
                ],
            ]
        )

    async def diagnostics_text(self) -> str:
        settings = await self.repository.settings.get()
        state = await self.repository.state.get()
        language = settings.language
        error = state.last_error or self.delivery.last_error
        if error and has_translation(language, f"error.{error}"):
            error_text = tr(language, f"error.{error}")
        else:
            error_text = html.escape(error) if error else tr(language, "diagnostics.no_error")
        return "\n".join(
            [
                tr(language, "diagnostics.title"),
                tr(language, "diagnostics.version", version=html.escape(self.config.version)),
                tr(language, "diagnostics.commit", commit=html.escape(self.config.commit)),
                "Codex: 0.154.0",
                tr(
                    language,
                    "diagnostics.app_server_running"
                    if self.rpc.running
                    else "diagnostics.app_server_restarting",
                ),
                tr(
                    language,
                    "diagnostics.scheduler_running"
                    if self.monitor.next_check_at is not None
                    else "diagnostics.scheduler_waiting",
                ),
                tr(
                    language,
                    "diagnostics.delivery_running"
                    if self.delivery.running
                    else "diagnostics.delivery_stopped",
                ),
                tr(language, "diagnostics.queue", count=len(state.outbox)),
                tr(
                    language,
                    "diagnostics.update",
                    status=html.escape(
                        read_update_status(self.config.data_dir / "update-status.json", language)
                    ),
                ),
                tr(language, "diagnostics.last_error", error=error_text),
            ]
        )

    async def show_history(self, message: Message, page: int) -> None:
        state = await self.repository.state.get()
        settings = await self.repository.settings.get()
        text, keyboard = self._history_page(settings, state, page)
        await message.answer(text, reply_markup=keyboard)

    def _history_page(
        self, settings: Settings, state: AppState, page: int
    ) -> tuple[str, InlineKeyboardMarkup]:
        events = list(reversed(state.events))
        page_size = 5
        pages = max(1, (len(events) + page_size - 1) // page_size)
        page = min(max(page, 0), pages - 1)
        selected = events[page * page_size : (page + 1) * page_size]
        body = "\n\n".join(
            f"{render_event(event, settings.language, settings.timezone, rationale=True)}\n"
            f"{format_datetime(event.detected_at, settings.timezone, settings.language)}"
            for event in selected
        ) or tr(settings.language, "history.empty")
        buttons: list[InlineKeyboardButton] = []
        if page > 0:
            buttons.append(InlineKeyboardButton(text="←", callback_data=f"v2:hist:{page - 1}"))
        buttons.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="v2:noop"))
        if page + 1 < pages:
            buttons.append(InlineKeyboardButton(text="→", callback_data=f"v2:hist:{page + 1}"))
        return (
            f"{tr(settings.language, 'history.title')}\n\n{body}",
            InlineKeyboardMarkup(inline_keyboard=[buttons]),
        )

    async def handle_callback(self, query: CallbackQuery, state: FSMContext) -> None:
        if not await self._authorized_callback(query):
            return
        settings = await self.repository.settings.get()
        language = settings.language
        data = query.data or ""
        message = query.message
        if not isinstance(message, Message):
            await query.answer()
            return
        if not data.startswith("v2:"):
            await query.answer(tr(language, "callback.stale"), show_alert=True)
            return
        parts = data.split(":")
        action = parts[1]
        if action == "refresh":
            await query.answer(tr(language, "check.checking"))
            result = await self.monitor.check_now(manual=True)
            self.delivery.wake()
            settings = await self.repository.settings.get()
            app_state = await self.repository.state.get()
            await message.edit_text(
                self._checked_status(settings, app_state, result.message_key),
                reply_markup=status_keyboard(settings.language),
            )
            return
        if action == "status":
            await message.edit_text(
                render_status(settings, await self.repository.state.get()),
                reply_markup=status_keyboard(language),
            )
        elif action == "details":
            await message.edit_text(
                render_details(settings, await self.repository.state.get()),
                reply_markup=details_keyboard(language),
            )
        elif action == "int" and len(parts) == 3:
            if parts[2] == "custom":
                await state.set_state(InputState.interval)
                await message.answer(tr(language, "input.interval_prompt"))
            elif not parts[2].isdecimal() or not 5 <= int(parts[2]) <= 1440:
                await query.answer(tr(language, "callback.stale"), show_alert=True)
                return
            else:
                minutes = int(parts[2])

                def update_interval(current: Settings) -> None:
                    current.interval_minutes = minutes

                await self.repository.settings.mutate(update_interval)
                self.monitor.settings_changed()
                await self._edit_settings(message)
        elif action == "timezone":
            await state.set_state(InputState.timezone)
            await message.answer(tr(language, "input.timezone_prompt"))
        elif action == "language":
            await message.edit_text(
                tr(language, "language.title"),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=tr(language, "language.english"),
                                callback_data="v2:lang:en",
                            ),
                            InlineKeyboardButton(
                                text=tr(language, "language.russian"),
                                callback_data="v2:lang:ru",
                            ),
                        ],
                        [
                            InlineKeyboardButton(
                                text=tr(language, "settings.back"),
                                callback_data="v2:settings",
                            )
                        ],
                    ]
                ),
            )
        elif action == "lang" and len(parts) == 3 and parts[2] in SUPPORTED_LANGUAGES:
            selected: Language = "ru" if parts[2] == "ru" else "en"

            def update_language(current: Settings) -> None:
                current.language = selected

            settings = await self.repository.settings.mutate(update_language)
            await state.clear()
            await self.register_commands()
            await self._edit_settings(message)
            await message.answer(
                tr(settings.language, "language.changed"),
                reply_markup=main_keyboard(settings.language),
            )
        elif action == "settings":
            await message.edit_text(
                await self.settings_text(), reply_markup=await self.settings_keyboard()
            )
        elif action == "pause":

            def toggle_pause(current: Settings) -> None:
                current.paused = not current.paused

            await self.repository.settings.mutate(toggle_pause)
            self.monitor.settings_changed()
            await self._edit_settings(message)
        elif action == "notif" and len(parts) == 3:
            field = parts[2]
            if field not in NotificationSettings.model_fields:
                await query.answer(tr(language, "callback.stale"), show_alert=True)
                return

            def toggle_notification(current: Settings) -> None:
                enabled = getattr(current.notifications, field)
                setattr(current.notifications, field, not enabled)

            await self.repository.settings.mutate(toggle_notification)
            await self._edit_settings(message)
        elif action == "hist" and len(parts) == 3 and parts[2].isdecimal():
            text, keyboard = self._history_page(
                settings, await self.repository.state.get(), int(parts[2])
            )
            await message.edit_text(text, reply_markup=keyboard)
        elif action == "login":
            await self._start_login(message)
        elif action == "login_cancel" and len(parts) == 3:
            cancelled = await self.login.cancel_by_tag(parts[2])
            await message.edit_text(
                tr(language, "login.cancelled" if cancelled else "login.already_finished")
            )
        elif action == "logout" and len(parts) == 3 and parts[2] == "ask":
            await message.edit_text(
                tr(language, "logout.confirm"),
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text=tr(language, "logout.yes"),
                                callback_data="v2:logout:yes",
                            ),
                            InlineKeyboardButton(
                                text=tr(language, "login.cancel"),
                                callback_data="v2:close",
                            ),
                        ]
                    ]
                ),
            )
        elif action == "logout" and len(parts) == 3 and parts[2] == "yes":
            await self.login.cancel()
            try:
                await self.rpc.request("account/logout")
            except Exception:
                await query.answer(tr(language, "logout.failed"), show_alert=True)
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
            await message.edit_text(tr(language, "logout.done"))
        elif action == "close":
            await message.edit_reply_markup(reply_markup=None)
        elif action == "noop":
            pass
        else:
            await query.answer(tr(language, "callback.stale"), show_alert=True)
            return
        await query.answer()

    async def _edit_settings(self, message: Message) -> None:
        with contextlib.suppress(TelegramBadRequest):
            await message.edit_text(
                await self.settings_text(), reply_markup=await self.settings_keyboard()
            )

    async def _start_login(self, message: Message) -> None:
        settings = await self.repository.settings.get()
        language = settings.language
        try:
            device = await self.login.start()
        except LoginAlreadyRunning:
            await message.answer(tr(language, "login.already_running"))
            return
        except Exception:
            await message.answer(tr(language, "login.start_failed"))
            return
        login_message = await message.answer(
            f"{tr(language, 'login.title')}\n"
            + tr(
                language,
                "login.instructions",
                url=html.escape(device.verification_url),
                code=html.escape(device.user_code),
            ),
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=tr(language, "login.open"), url=device.verification_url
                        )
                    ],
                    [
                        InlineKeyboardButton(
                            text=tr(language, "login.cancel"),
                            callback_data=f"v2:login_cancel:{login_callback_tag(device.login_id)}",
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
        settings = await self.repository.settings.get()
        with contextlib.suppress(TelegramBadRequest):
            await message.edit_text(tr(settings.language, completion.message_key))

    async def recover_interrupted_login(self) -> None:
        pending = await self.login.recover_interrupted()
        if pending is None or not pending.message_chat_id or not pending.message_id:
            return
        settings = await self.repository.settings.get()
        with contextlib.suppress(TelegramBadRequest):
            await self.bot.edit_message_text(
                tr(settings.language, "login.restart_interrupted"),
                chat_id=pending.message_chat_id,
                message_id=pending.message_id,
            )

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
