from __future__ import annotations

import importlib
import os
from pathlib import Path
from types import TracebackType
from typing import TextIO


class AlreadyRunningError(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._stream: TextIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        stream = self.path.open("a+", encoding="ascii")
        os.chmod(self.path, 0o600)
        try:
            if os.name == "nt":
                stream.seek(0)
                if not stream.read(1):
                    stream.write("0")
                    stream.flush()
                stream.seek(0)
                windows_lock = vars(importlib.import_module("msvcrt"))
                windows_lock["locking"](stream.fileno(), windows_lock["LK_NBLCK"], 1)
            else:
                unix_lock = vars(importlib.import_module("fcntl"))
                unix_lock["flock"](
                    stream.fileno(),
                    unix_lock["LOCK_EX"] | unix_lock["LOCK_NB"],
                )
        except OSError as exc:
            stream.close()
            raise AlreadyRunningError(
                "another Codex Notify instance owns this data directory"
            ) from exc
        stream.seek(0)
        stream.truncate()
        stream.write(str(os.getpid()))
        stream.flush()
        os.fsync(stream.fileno())
        self._stream = stream

    def release(self) -> None:
        stream = self._stream
        if stream is None:
            return
        try:
            if os.name == "nt":
                stream.seek(0)
                windows_lock = vars(importlib.import_module("msvcrt"))
                windows_lock["locking"](stream.fileno(), windows_lock["LK_UNLCK"], 1)
            else:
                unix_lock = vars(importlib.import_module("fcntl"))
                unix_lock["flock"](
                    stream.fileno(),
                    unix_lock["LOCK_UN"],
                )
        finally:
            stream.close()
            self._stream = None

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()
