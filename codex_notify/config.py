from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import __version__


@dataclass(frozen=True, slots=True)
class AppConfig:
    bot_token: str
    data_dir: Path
    codex_home: Path
    codex_command: tuple[str, ...]
    rpc_timeout_seconds: float
    login_timeout_seconds: float
    health_file: Path
    version: str
    commit: str

    @classmethod
    def from_env(cls) -> AppConfig:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
        command = os.environ.get("CODEX_COMMAND", "codex app-server --stdio --strict-config")
        return cls(
            bot_token=token,
            data_dir=Path(os.environ.get("DATA_DIR", "/app/data")),
            codex_home=Path(os.environ.get("CODEX_HOME", "/app/codex-home")),
            codex_command=tuple(command.split()),
            rpc_timeout_seconds=float(os.environ.get("CODEX_RPC_TIMEOUT_SECONDS", "20")),
            login_timeout_seconds=float(os.environ.get("CODEX_LOGIN_TIMEOUT_SECONDS", "900")),
            health_file=Path(os.environ.get("HEALTH_FILE", "/tmp/codex-notify-health.json")),
            version=os.environ.get("APP_VERSION", __version__),
            commit=os.environ.get("APP_COMMIT", "unknown")[:12],
        )
