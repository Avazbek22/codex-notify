from __future__ import annotations

from typing import Any


def rate_payload(
    *,
    account_id: str = "acct-A",
    used: int = 80,
    reset: int = 2_000,
    duration: int = 300,
    ordinary: bool | object | None = True,
    credits: dict[str, Any] | object | None = None,
    second_used: int | None = None,
) -> dict[str, Any]:
    bucket: dict[str, Any] = {
        "limitId": "codex",
        "limitName": "Codex",
        "primary": {
            "usedPercent": used,
            "windowDurationMins": duration,
            "resetsAt": reset,
        },
        "secondary": None,
        "rateLimitReachedType": None,
    }
    if second_used is not None:
        bucket["secondary"] = {
            "usedPercent": second_used,
            "windowDurationMins": 10_080,
            "resetsAt": reset + 100_000,
        }
    payload: dict[str, Any] = {
        "accountId": account_id,
        "rateLimits": bucket,
        "rateLimitsByLimitId": {"codex": bucket},
    }
    if ordinary is not OMITTED:
        payload["ordinaryUsageAllowed"] = ordinary
    if credits is not OMITTED:
        payload["rateLimitResetCredits"] = credits
    return payload


OMITTED = object()
