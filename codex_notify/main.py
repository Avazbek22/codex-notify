from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from .codex_rpc import CodexAppServer
from .config import AppConfig
from .delivery import OutboxDelivery
from .health import HealthReporter
from .instance_lock import InstanceLock
from .login import LoginManager
from .monitor import Monitor
from .storage import Repository, StorageRecoveryError
from .telegram_ui import TelegramUI


class SecretFilter(logging.Filter):
    TOKEN_PATTERN = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b")

    def __init__(self, token: str) -> None:
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self.token:
            message = message.replace(self.token, "[REDACTED_TELEGRAM_TOKEN]")
        message = self.TOKEN_PATTERN.sub("[REDACTED_TELEGRAM_TOKEN]", message)
        record.msg = message
        record.args = ()
        return True


def configure_logging(token: str) -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    secret_filter = SecretFilter(token)
    for handler in logging.getLogger().handlers:
        handler.addFilter(secret_filter)


def ensure_codex_config(codex_home: Path) -> None:
    codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(codex_home, 0o700)
    config = codex_home / "config.toml"
    if config.exists():
        return
    temporary = codex_home / ".config.toml.tmp"
    temporary.write_text(
        'cli_auth_credentials_store = "file"\n\n[analytics]\nenabled = false\n',
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    with temporary.open("r+", encoding="utf-8") as stream:
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, config)


async def async_main() -> None:
    config = AppConfig.from_env()
    configure_logging(config.bot_token)
    ensure_codex_config(config.codex_home)
    lock = InstanceLock(config.data_dir / "instance.lock")
    lock.acquire()
    repository = Repository(config.data_dir)
    try:
        await repository.initialize()
    except StorageRecoveryError:
        logging.getLogger(__name__).critical(
            "Storage is corrupt and could not be recovered from a verified backup. "
            "The bot is in closed diagnostic mode; run scripts/restore-json.sh on the server."
        )
        health = HealthReporter(
            config.health_file,
            {"storage": lambda: False, "diagnostic_closed": lambda: False},
        )
        try:
            await health.run()
        finally:
            lock.release()
        return

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    rpc = CodexAppServer(
        config.codex_command, config.codex_home, request_timeout=config.rpc_timeout_seconds
    )
    monitor = Monitor(repository, rpc)
    delivery = OutboxDelivery(repository, bot)
    login = LoginManager(repository, rpc, monitor, timeout=config.login_timeout_seconds)
    ui = TelegramUI(bot, repository, monitor, login, rpc, delivery, config)
    dispatcher = Dispatcher(storage=MemoryStorage())
    dispatcher.include_router(ui.router)

    scheduler_task: asyncio.Task[None] | None = None
    delivery_task: asyncio.Task[None] | None = None
    health_task: asyncio.Task[None] | None = None
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await ui.register_commands()
        await ui.recover_interrupted_login()
        scheduler_task = asyncio.create_task(monitor.scheduler_loop(), name="scheduler")
        delivery_task = asyncio.create_task(delivery.run(), name="delivery")
        health = HealthReporter(
            config.health_file,
            {
                "scheduler": lambda: scheduler_task is not None and not scheduler_task.done(),
                "delivery": lambda: delivery_task is not None and not delivery_task.done(),
                "polling": lambda: True,
            },
        )
        health_task = asyncio.create_task(health.run(), name="health")
        await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())
    finally:
        for task in (scheduler_task, delivery_task, health_task):
            if task:
                task.cancel()
        await asyncio.gather(
            *(task for task in (scheduler_task, delivery_task, health_task) if task),
            return_exceptions=True,
        )
        await ui.close()
        await rpc.close()
        await bot.session.close()
        lock.release()


def run() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    run()
