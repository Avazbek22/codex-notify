from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from .models import AppState, Settings

T = TypeVar("T", bound=BaseModel)


class StorageError(RuntimeError):
    pass


class StorageRecoveryError(StorageError):
    pass


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temporary.unlink(missing_ok=True)
        except PermissionError:
            temporary.unlink(missing_ok=True)
        raise


class AtomicModelFile[T: BaseModel]:
    def __init__(self, path: Path, model_type: type[T], factory: Callable[[], T]) -> None:
        self.path = path
        self.backup_path = path.with_suffix(path.suffix + ".bak")
        self.model_type = model_type
        self.factory = factory
        self._lock = asyncio.Lock()
        self._value: T | None = None

    def _decode(self, raw: bytes) -> T:
        return self.model_type.model_validate_json(raw)

    def _encode(self, value: T) -> bytes:
        return (value.model_dump_json(indent=2) + "\n").encode()

    def _recover_corrupt(self, error: Exception) -> T:
        stamp = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{time.time_ns()}"
        corrupt = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        os.replace(self.path, corrupt)
        _fsync_directory(self.path.parent)
        if not self.backup_path.exists():
            raise StorageRecoveryError(
                f"{self.path.name} is corrupt; preserved as {corrupt.name}; no backup exists"
            ) from error
        try:
            backup_raw = self.backup_path.read_bytes()
            recovered = self._decode(backup_raw)
        except (OSError, ValidationError, ValueError) as backup_error:
            raise StorageRecoveryError(
                f"{self.path.name} and its backup are invalid; registration remains closed"
            ) from backup_error
        _atomic_bytes(self.path, backup_raw)
        return recovered

    async def load(self, *, allow_create: bool) -> T:
        async with self._lock:
            if not self.path.exists():
                if not allow_create:
                    raise StorageRecoveryError(f"required file is missing: {self.path.name}")
                value = self.factory()
                _atomic_bytes(self.path, self._encode(value))
                self._value = value
                return value.model_copy(deep=True)
            try:
                value = self._decode(self.path.read_bytes())
            except (OSError, ValidationError, ValueError) as exc:
                value = self._recover_corrupt(exc)
            self._value = value
            return value.model_copy(deep=True)

    async def get(self) -> T:
        async with self._lock:
            if self._value is None:
                raise StorageError(f"{self.path.name} was not initialized")
            return self._value.model_copy(deep=True)

    async def replace(self, value: T) -> T:
        async with self._lock:
            validated = self.model_type.model_validate(value.model_dump())
            payload = self._encode(validated)
            if self.path.exists():
                current_raw = self.path.read_bytes()
                self._decode(current_raw)
                _atomic_bytes(self.backup_path, current_raw)
            _atomic_bytes(self.path, payload)
            self._value = validated
            return validated.model_copy(deep=True)

    async def mutate(self, callback: Callable[[T], None]) -> T:
        async with self._lock:
            if self._value is None:
                raise StorageError(f"{self.path.name} was not initialized")
            candidate = self._value.model_copy(deep=True)
            callback(candidate)
            validated = self.model_type.model_validate(candidate.model_dump())
            payload = self._encode(validated)
            current_raw = self.path.read_bytes()
            self._decode(current_raw)
            _atomic_bytes(self.backup_path, current_raw)
            _atomic_bytes(self.path, payload)
            self._value = validated
            return validated.model_copy(deep=True)


class Repository:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.settings = AtomicModelFile(data_dir / "settings.json", Settings, Settings)
        self.state = AtomicModelFile(data_dir / "state.json", AppState, AppState)

    async def initialize(self) -> tuple[Settings, AppState]:
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data_dir, 0o700)
        settings_exists = self.settings.path.exists()
        state_exists = self.state.path.exists()
        if settings_exists != state_exists:
            missing = "settings.json" if not settings_exists else "state.json"
            raise StorageRecoveryError(
                f"{missing} is missing from an existing installation; refusing unsafe reset"
            )
        allow_create = not settings_exists and not state_exists
        loaded_settings = await self.settings.load(allow_create=allow_create)
        loaded_state = await self.state.load(allow_create=allow_create)
        return loaded_settings, loaded_state

    def snapshot_json_files(self, destination: Path) -> None:
        destination.mkdir(parents=True, exist_ok=False, mode=0o700)
        for name in ("settings.json", "state.json"):
            source = self.data_dir / name
            if source.exists():
                shutil.copy2(source, destination / name)
