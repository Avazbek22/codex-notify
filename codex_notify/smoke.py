from __future__ import annotations

import os
import subprocess

from .codex_rpc import PUBLIC_METHODS
from .models import AppState, Settings

FORBIDDEN_METHODS = {
    "thread/start",
    "turn/start",
    "command/exec",
    "account/rateLimitResetCredit/consume",
}


def main() -> None:
    Settings.model_validate(Settings().model_dump())
    AppState.model_validate(AppState().model_dump())
    if PUBLIC_METHODS & FORBIDDEN_METHODS:
        raise SystemExit("unsafe RPC allowlist")
    completed = subprocess.run(
        [os.environ.get("CODEX_EXECUTABLE", "codex"), "--version"],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if "0.154.0" not in completed.stdout:
        raise SystemExit("unexpected Codex version")
    print("smoke ok")


if __name__ == "__main__":
    main()
