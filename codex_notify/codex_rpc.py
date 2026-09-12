from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__

LOGGER = logging.getLogger(__name__)

PUBLIC_METHODS = frozenset(
    {
        "account/read",
        "account/login/start",
        "account/login/cancel",
        "account/logout",
        "account/rateLimits/read",
    }
)


class CodexRpcError(RuntimeError):
    def __init__(
        self,
        code: int | None,
        *,
        auth_related: bool = False,
        retry_after: float | None = None,
        message: str = "Codex App Server returned an error",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.auth_related = auth_related
        self.retry_after = retry_after


class CodexProcessError(RuntimeError):
    pass


class CodexProtocolError(RuntimeError):
    pass


class CodexTimeoutError(TimeoutError):
    pass


class CodexAppServer:
    def __init__(
        self,
        command: tuple[str, ...],
        codex_home: Path,
        *,
        request_timeout: float = 20,
    ) -> None:
        self.command = command
        self.codex_home = codex_home
        self.request_timeout = request_timeout
        self.process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._wait_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 1
        self._write_lock = asyncio.Lock()
        self._start_lock = asyncio.Lock()
        self._closed = False
        self._notifications: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self.last_failure: str | None = None
        self.restart_count = 0

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    def _child_environment(self) -> dict[str, str]:
        allowed = {
            "PATH",
            "LANG",
            "LC_ALL",
            "SSL_CERT_FILE",
            "CODEX_CA_CERTIFICATE",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "NO_PROXY",
        }
        environment = {key: value for key, value in os.environ.items() if key in allowed}
        environment["CODEX_HOME"] = str(self.codex_home)
        environment["HOME"] = str(self.codex_home)
        return environment

    async def start(self) -> None:
        async with self._start_lock:
            if self.running:
                return
            if self._closed:
                raise CodexProcessError("Codex App Server adapter is closed")
            self.codex_home.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(self.codex_home, 0o700)
            try:
                self.process = await asyncio.create_subprocess_exec(
                    *self.command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=self._child_environment(),
                    cwd=self.codex_home,
                    limit=1024 * 1024,
                )
            except OSError as exc:
                self.last_failure = "Codex App Server could not start"
                raise CodexProcessError(self.last_failure) from exc
            self._reader_task = asyncio.create_task(self._read_stdout(), name="codex-stdout")
            self._stderr_task = asyncio.create_task(self._drain_stderr(), name="codex-stderr")
            self._wait_task = asyncio.create_task(self._wait_for_exit(), name="codex-exit")
            try:
                await self._request_raw(
                    "initialize",
                    {
                        "clientInfo": {
                            "name": "codex_notify",
                            "title": "Codex Notify",
                            "version": __version__,
                        },
                        "capabilities": {
                            "optOutNotificationMethods": [
                                "thread/started",
                                "item/started",
                                "item/completed",
                                "item/agentMessage/delta",
                            ]
                        },
                    },
                )
                await self._send({"method": "initialized"})
                self.last_failure = None
            except BaseException:
                await self._terminate_process()
                raise

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if method not in PUBLIC_METHODS:
            raise ValueError(f"Codex method is not allowed: {method}")
        if not self.running:
            if self.restart_count:
                await asyncio.sleep(min(30.0, 2 ** min(self.restart_count, 5)))
            await self.start()
        return await self._request_raw(method, params)

    async def _request_raw(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        message: dict[str, Any] = {"method": method, "id": request_id}
        if params is not None:
            message["params"] = params
        try:
            await self._send(message)
            async with asyncio.timeout(self.request_timeout):
                return await future
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise CodexTimeoutError(f"Codex request timed out: {method}") from exc
        except BaseException:
            self._pending.pop(request_id, None)
            raise

    async def _send(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.returncode is not None or process.stdin is None:
            raise CodexProcessError("Codex App Server is not running")
        payload = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        async with self._write_lock:
            try:
                process.stdin.write(payload)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionError) as exc:
                raise CodexProcessError("Codex App Server stdin closed") from exc

    async def _read_stdout(self) -> None:
        process = self.process
        assert process is not None and process.stdout is not None
        try:
            while line := await process.stdout.readline():
                if len(line) > 1024 * 1024:
                    raise CodexProtocolError("Codex response exceeded the size limit")
                try:
                    message = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise CodexProtocolError("Codex returned malformed JSON") from exc
                if not isinstance(message, dict):
                    raise CodexProtocolError("Codex returned a non-object message")
                response_id = message.get("id")
                if isinstance(response_id, int) and ("result" in message or "error" in message):
                    future = self._pending.pop(response_id, None)
                    if future is None or future.done():
                        continue
                    error = message.get("error")
                    if isinstance(error, dict):
                        code = error.get("code") if isinstance(error.get("code"), int) else None
                        raw_message = error.get("message")
                        lowered = raw_message.lower() if isinstance(raw_message, str) else ""
                        auth_related = any(
                            marker in lowered
                            for marker in (
                                "unauthorized",
                                "authentication",
                                "not logged in",
                                "token expired",
                            )
                        )
                        data = error.get("data")
                        retry_value = None
                        if isinstance(data, dict):
                            candidate = data.get("retryAfter", data.get("retry_after"))
                            if isinstance(candidate, (int, float)) and candidate >= 0:
                                retry_value = float(candidate)
                        future.set_exception(
                            CodexRpcError(
                                code,
                                auth_related=auth_related,
                                retry_after=retry_value,
                            )
                        )
                    elif isinstance(message.get("result"), dict):
                        future.set_result(message["result"])
                    elif message.get("result") is None:
                        future.set_result({})
                    else:
                        future.set_exception(
                            CodexProtocolError("Codex result has an invalid shape")
                        )
                elif isinstance(message.get("method"), str) and "id" not in message:
                    if self._notifications.full():
                        self._notifications.get_nowait()
                    self._notifications.put_nowait(message)
                elif "id" in message and "method" in message:
                    await self._send(
                        {
                            "id": message["id"],
                            "error": {"code": -32601, "message": "Client method not supported"},
                        }
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_failure = str(exc)
            self._fail_pending(exc)
            await self._terminate_process()

    async def _drain_stderr(self) -> None:
        process = self.process
        assert process is not None and process.stderr is not None
        total = 0
        try:
            while chunk := await process.stderr.read(4096):
                total += len(chunk)
            if total:
                LOGGER.warning(
                    "Codex App Server wrote %d bytes to stderr; content suppressed", total
                )
        except asyncio.CancelledError:
            raise

    async def _wait_for_exit(self) -> None:
        process = self.process
        assert process is not None
        return_code = await process.wait()
        if not self._closed:
            self.restart_count += 1
            self.last_failure = f"Codex App Server exited with status {return_code}"
            self._fail_pending(CodexProcessError(self.last_failure))

    def _fail_pending(self, exc: BaseException) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    async def next_notification(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        async with asyncio.timeout(timeout):
            while True:
                notification = await self._notifications.get()
                if predicate(notification):
                    return notification

    async def _terminate_process(self) -> None:
        process = self.process
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            async with asyncio.timeout(5):
                await process.wait()
        except TimeoutError:
            process.kill()
            await process.wait()

    async def close(self) -> None:
        self._closed = True
        await self._terminate_process()
        current = asyncio.current_task()
        for task in (self._reader_task, self._stderr_task, self._wait_task):
            if task is not None and task is not current and not task.done():
                task.cancel()
        for task in (self._reader_task, self._stderr_task, self._wait_task):
            if task is not None and task is not current:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._fail_pending(CodexProcessError("Codex App Server stopped"))
