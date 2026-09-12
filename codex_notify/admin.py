from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .binding import create_binding, set_owner
from .instance_lock import InstanceLock
from .models import SCHEMA_VERSION
from .storage import Repository
from .validate_data import validate_data


async def _run(arguments: argparse.Namespace) -> None:
    data_dir = Path(arguments.data_dir)
    with InstanceLock(data_dir / "instance.lock"):
        if arguments.command == "validate-data":
            validate_data(data_dir)
            print(f"JSON schema is compatible with application version {SCHEMA_VERSION}")
            return
        repository = Repository(data_dir)
        await repository.initialize()
        if arguments.command == "binding-link":
            token = await create_binding(repository.settings, ttl_minutes=arguments.ttl)
            print(f"https://t.me/{arguments.bot_username}?start=claim_{token}")
        elif arguments.command == "set-owner":
            await set_owner(repository.settings, arguments.telegram_id)
            print("Telegram owner configured")


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Offline Codex Notify administration")
    root.add_argument("--data-dir", default="/app/data")
    sub = root.add_subparsers(dest="command", required=True)
    binding = sub.add_parser("binding-link")
    binding.add_argument("--bot-username", required=True)
    binding.add_argument("--ttl", type=int, default=15)
    owner = sub.add_parser("set-owner")
    owner.add_argument("telegram_id", type=int)
    sub.add_parser("validate-data")
    sub.add_parser("max-schema")
    return root


def main() -> None:
    arguments = parser().parse_args()
    if arguments.command == "max-schema":
        print(SCHEMA_VERSION)
        return
    asyncio.run(_run(arguments))


if __name__ == "__main__":
    main()
