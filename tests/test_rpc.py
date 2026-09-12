from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from codex_notify.codex_rpc import (
    PUBLIC_METHODS,
    CodexAppServer,
    CodexProcessError,
    CodexProtocolError,
    CodexRpcError,
    CodexTimeoutError,
)

FAKE = Path(__file__).with_name("fake_app_server.py")


@pytest.mark.asyncio
async def test_real_stdio_handshake_and_response_correlation(tmp_path: Path) -> None:
    adapter = CodexAppServer((sys.executable, str(FAKE), "normal"), tmp_path, request_timeout=1)
    try:
        account = await adapter.request("account/read", {"refreshToken": False})
        limits = await adapter.request("account/rateLimits/read")
        assert account["account"]["type"] == "chatgpt"
        assert limits["rateLimits"]["primary"]["usedPercent"] == 25
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_adapter_rejects_every_model_shell_and_consume_method(tmp_path: Path) -> None:
    adapter = CodexAppServer((sys.executable, str(FAKE), "normal"), tmp_path)
    try:
        for method in (
            "thread/start",
            "turn/start",
            "command/exec",
            "account/rateLimitResetCredit/consume",
        ):
            with pytest.raises(ValueError, match="not allowed"):
                await adapter.request(method)
        assert PUBLIC_METHODS == {
            "account/read",
            "account/login/start",
            "account/login/cancel",
            "account/logout",
            "account/rateLimits/read",
        }
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_rpc_timeout_never_leaves_a_pending_waiter(tmp_path: Path) -> None:
    adapter = CodexAppServer((sys.executable, str(FAKE), "timeout"), tmp_path, request_timeout=0.05)
    try:
        with pytest.raises(CodexTimeoutError):
            await adapter.request("account/read")
        assert not adapter._pending
    finally:
        await adapter.close()


@pytest.mark.asyncio
async def test_process_death_and_malformed_output_are_explicit(tmp_path: Path) -> None:
    dead = CodexAppServer((sys.executable, str(FAKE), "die"), tmp_path / "dead", request_timeout=1)
    with pytest.raises(CodexProcessError):
        await dead.request("account/read")
    await dead.close()

    malformed = CodexAppServer(
        (sys.executable, str(FAKE), "malformed"), tmp_path / "malformed", request_timeout=1
    )
    with pytest.raises(CodexProtocolError):
        await malformed.request("account/read")
    await malformed.close()


@pytest.mark.asyncio
async def test_stderr_is_drained_without_logging_secrets(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    adapter = CodexAppServer((sys.executable, str(FAKE), "stderr"), tmp_path, request_timeout=1)
    await adapter.request("account/read")
    await adapter.close()
    log = caplog.text
    assert "content suppressed" in log
    assert "fake-secret-value-for-redaction" not in log


@pytest.mark.asyncio
async def test_rpc_error_exposes_only_classification_and_retry_delay(tmp_path: Path) -> None:
    limited = CodexAppServer(
        (sys.executable, str(FAKE), "rpc_error"), tmp_path / "limited", request_timeout=1
    )
    try:
        with pytest.raises(CodexRpcError) as caught:
            await limited.request("account/read")
        assert caught.value.code == 429
        assert caught.value.retry_after == 17
        assert "rate limited" not in str(caught.value)
    finally:
        await limited.close()

    expired = CodexAppServer(
        (sys.executable, str(FAKE), "auth_error"), tmp_path / "expired", request_timeout=1
    )
    try:
        with pytest.raises(CodexRpcError) as caught:
            await expired.request("account/read")
        assert caught.value.auth_related
    finally:
        await expired.close()
