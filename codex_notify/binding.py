from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from .models import BindingState, Settings
from .storage import AtomicModelFile
from .timeutil import parse_utc, utc_now, utc_now_iso


def _hash_token(token: str) -> str:
    return hashlib.sha256(f"codex-notify-owner-binding\0{token}".encode()).hexdigest()


async def create_binding(store: AtomicModelFile[Settings], *, ttl_minutes: int = 15) -> str:
    token = secrets.token_urlsafe(24)
    expires = (utc_now() + timedelta(minutes=ttl_minutes)).isoformat().replace("+00:00", "Z")

    def update(settings: Settings) -> None:
        if settings.owner_id is not None:
            raise RuntimeError("owner is already configured")
        settings.binding = BindingState(
            token_hash=_hash_token(token),
            expires_at=expires,
            created_at=utc_now_iso(),
        )

    await store.mutate(update)
    return token


async def consume_binding(store: AtomicModelFile[Settings], token: str, telegram_id: int) -> bool:
    current = await store.get()
    if current.owner_id is not None or current.binding is None:
        return False
    if not hmac.compare_digest(current.binding.token_hash, _hash_token(token)):
        return False
    accepted = False

    def consume(settings: Settings) -> None:
        nonlocal accepted
        binding = settings.binding
        if settings.owner_id is not None or binding is None:
            return
        expires = parse_utc(binding.expires_at)
        if expires is None or expires <= utc_now():
            settings.binding = None
            return
        if not hmac.compare_digest(binding.token_hash, _hash_token(token)):
            return
        settings.owner_id = telegram_id
        settings.binding = None
        accepted = True

    await store.mutate(consume)
    return accepted


async def set_owner(store: AtomicModelFile[Settings], telegram_id: int) -> None:
    def update(settings: Settings) -> None:
        if settings.owner_id is not None and settings.owner_id != telegram_id:
            raise RuntimeError("refusing to replace the configured owner")
        settings.owner_id = telegram_id
        settings.binding = None

    await store.mutate(update)
