from __future__ import annotations

import os
from pathlib import Path

from .health import healthcheck


def main() -> None:
    path = Path(os.environ.get("HEALTH_FILE", "/tmp/codex-notify-health.json"))
    raise SystemExit(0 if healthcheck(path) else 1)


if __name__ == "__main__":
    main()
