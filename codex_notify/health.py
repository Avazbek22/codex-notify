from __future__ import annotations

import asyncio
import json
import os
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path


class HealthReporter:
    def __init__(self, path: Path, statuses: dict[str, Callable[[], bool]]) -> None:
        self.path = path
        self.statuses = statuses

    async def run(self) -> None:
        while True:
            payload = {
                "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                "tasks": {name: check() for name, check in self.statuses.items()},
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix=".health-", dir=self.path.parent)
            temporary = Path(name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(payload, stream, separators=(",", ":"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
            await asyncio.sleep(15)


def healthcheck(path: Path, *, max_age_seconds: float = 90) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(value["updated_at"].replace("Z", "+00:00"))
        age = (datetime.now(UTC) - updated.astimezone(UTC)).total_seconds()
        tasks = value["tasks"]
        return 0 <= age <= max_age_seconds and bool(tasks) and all(tasks.values())
    except (OSError, ValueError, KeyError, TypeError):
        return False
