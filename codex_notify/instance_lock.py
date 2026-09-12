from __future__ import annotations

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
                import msvcrt

                stream.seek(0)
                if not stream.read(1):
                    stream.write("0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(  # type: ignore[attr-defined]
                    stream.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
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
                import msvcrt

                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(  # type: ignore[attr-defined]
                    stream.fileno(),
                    fcntl.LOCK_UN,  # type: ignore[attr-defined]
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
