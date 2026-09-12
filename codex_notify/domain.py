from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from .models import (
    AccountIdentity,
    CreditBaseline,
    Event,
    EventCode,
    EventPayload,
    EventType,
    NotificationSettings,
    RateBucket,
    RateSnapshot,
    RateWindow,
    ResetCredit,
)
from .timeutil import utc_now_iso


def _event_id(kind: str, evidence: Mapping[str, object]) -> str:
    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(f"{kind}:{canonical}".encode()).hexdigest()[:24]
    return f"{kind}:{digest}"


def make_event(
    kind: EventType,
    code: EventCode,
    evidence: Mapping[str, object],
    payload: EventPayload | None = None,
) -> Event:
    return Event(
        id=_event_id(kind, evidence),
        type=kind,
        detected_at=utc_now_iso(),
        code=code,
        payload=payload or EventPayload(),
    )


def mask_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    local, domain = email.rsplit("@", 1)
    visible = local[:1]
    return f"{visible}{'*' * max(3, min(len(local) - 1, 8))}@{domain}"


def make_account_identity(account: dict[str, Any], account_id: str | None) -> AccountIdentity:
    email = account.get("email") if isinstance(account.get("email"), str) else None
    stable_source = account_id or (email.lower() if email else None)
    if stable_source is None:
        stable_source = f"unknown:{account.get('type')}:{account.get('planType')}"
    key = hashlib.sha256(stable_source.encode()).hexdigest()
    plan = account.get("planType") if isinstance(account.get("planType"), str) else None
    return AccountIdentity(key=key, masked_email=mask_email(email), plan=plan)


def normalize_snapshot(
    raw: dict[str, Any], account: dict[str, Any]
) -> tuple[RateSnapshot, AccountIdentity]:
    account_id_value = raw.get("accountId")
    account_id = (
        account_id_value if isinstance(account_id_value, str) and account_id_value else None
    )
    identity = make_account_identity(account, account_id)

    multi_present = "rateLimitsByLimitId" in raw and isinstance(
        raw.get("rateLimitsByLimitId"), dict
    )
    if multi_present:
        source = raw["rateLimitsByLimitId"]
    else:
        legacy = raw.get("rateLimits")
        source = {"legacy": legacy} if isinstance(legacy, dict) else {}

    buckets: list[RateBucket] = []
    for map_key, value in source.items():
        if not isinstance(value, dict):
            continue
        explicit_id = value.get("limitId")
        limit_id = explicit_id if isinstance(explicit_id, str) and explicit_id else str(map_key)
        limit_name = value.get("limitName") if isinstance(value.get("limitName"), str) else None
        windows: list[RateWindow] = []
        for window_name in ("primary", "secondary"):
            window = value.get(window_name)
            if not isinstance(window, dict) or type(window.get("usedPercent")) is not int:
                continue
            duration = window.get("windowDurationMins")
            reset = window.get("resetsAt")
            windows.append(
                RateWindow(
                    limit_id=limit_id,
                    limit_name=limit_name,
                    window=window_name,
                    used_percent=window["usedPercent"],
                    window_duration_mins=duration if type(duration) is int else None,
                    resets_at=reset if type(reset) is int else None,
                )
            )
        credits = value.get("credits")
        unlimited = credits.get("unlimited") if isinstance(credits, dict) else None
        buckets.append(
            RateBucket(
                limit_id=limit_id,
                limit_name=limit_name,
                plan=value.get("planType") if isinstance(value.get("planType"), str) else None,
                unlimited=unlimited if isinstance(unlimited, bool) else None,
                reached_type=(
                    value.get("rateLimitReachedType")
                    if isinstance(value.get("rateLimitReachedType"), str)
                    else None
                ),
                spend_control_reached=(
                    value.get("spendControlReached")
                    if isinstance(value.get("spendControlReached"), bool)
                    else None
                ),
                windows=windows,
            )
        )

    if "ordinaryUsageAllowed" not in raw:
        usage_status: Literal["unsupported", "unknown", "known"] = "unsupported"
        usage_allowed = None
    elif raw.get("ordinaryUsageAllowed") is None:
        usage_status = "unknown"
        usage_allowed = None
    elif isinstance(raw.get("ordinaryUsageAllowed"), bool):
        usage_status = "known"
        usage_allowed = raw["ordinaryUsageAllowed"]
    else:
        usage_status = "unknown"
        usage_allowed = None

    reset_details: list[ResetCredit] | None = None
    reset_available: int | None = None
    if "rateLimitResetCredits" not in raw:
        reset_status: Literal["unsupported", "unknown", "known"] = "unsupported"
    elif raw.get("rateLimitResetCredits") is None:
        reset_status = "unknown"
    else:
        summary = raw["rateLimitResetCredits"]
        if not isinstance(summary, dict) or type(summary.get("availableCount")) is not int:
            reset_status = "unknown"
        else:
            reset_status = "known"
            reset_available = max(0, summary["availableCount"])
            detail_rows = summary.get("credits")
            if isinstance(detail_rows, list):
                reset_details = []
                for row in detail_rows:
                    if not isinstance(row, dict):
                        continue
                    required = ("id", "status", "resetType", "grantedAt")
                    if not all(key in row for key in required):
                        continue
                    if type(row["grantedAt"]) is not int:
                        continue
                    reset_details.append(
                        ResetCredit(
                            id=str(row["id"]),
                            status=str(row["status"]),
                            reset_type=str(row["resetType"]),
                            granted_at=row["grantedAt"],
                            expires_at=row.get("expiresAt")
                            if type(row.get("expiresAt")) is int
                            else None,
                            title=row.get("title") if isinstance(row.get("title"), str) else None,
                        )
                    )

    snapshot = RateSnapshot(
        observed_at=utc_now_iso(),
        account_key=identity.key,
        account_id_present=account_id is not None,
        buckets=buckets,
        ordinary_usage_status=usage_status,
        ordinary_usage_allowed=usage_allowed,
        reset_credits_status=reset_status,
        reset_credits_available=reset_available,
        reset_credit_details=reset_details,
    )
    return snapshot, identity


@dataclass(slots=True)
class ChangeAnalysis:
    events: list[Event]
    credit_baseline: CreditBaseline | None
    requires_confirmation: bool = False


def _window_map(snapshot: RateSnapshot) -> dict[str, RateWindow]:
    return {window.key: window for bucket in snapshot.buckets for window in bucket.windows}


def _bucket_map(snapshot: RateSnapshot) -> dict[str, RateBucket]:
    return {bucket.limit_id: bucket for bucket in snapshot.buckets}


def _credit_baseline(snapshot: RateSnapshot, old: CreditBaseline | None) -> CreditBaseline | None:
    if snapshot.reset_credits_status != "known" or snapshot.reset_credits_available is None:
        return old
    grants = dict(old.known_grants) if old else {}
    for credit in snapshot.reset_credit_details or []:
        grants[credit.id] = credit.granted_at
    if len(grants) > 200:
        grants = dict(sorted(grants.items(), key=lambda pair: pair[1])[-200:])
    return CreditBaseline(
        observed_at=snapshot.observed_at,
        available_count=snapshot.reset_credits_available,
        known_grants=grants,
    )


def analyze_changes(
    old: RateSnapshot | None,
    new: RateSnapshot,
    baseline: CreditBaseline | None,
    *,
    now: datetime,
    confirmed: bool = False,
) -> ChangeAnalysis:
    next_baseline = _credit_baseline(new, baseline)
    if old is None or old.account_key != new.account_key:
        return ChangeAnalysis([], next_baseline)

    events: list[Event] = []
    needs_confirmation = False
    old_windows = _window_map(old)
    now_timestamp = int(now.astimezone(UTC).timestamp())

    for key, current in _window_map(new).items():
        previous = old_windows.get(key)
        if previous is None:
            continue
        duration_changed = previous.window_duration_mins != current.window_duration_mins
        reset_advanced = (
            previous.resets_at is not None
            and current.resets_at is not None
            and current.resets_at > previous.resets_at
        )
        usage_dropped = current.used_percent < previous.used_percent
        reset_evidence = reset_advanced and usage_dropped and not duration_changed
        label = current.limit_name or current.limit_id
        payload = EventPayload(
            limit_label=label,
            window=current.window,
            previous_used_percent=previous.used_percent,
            used_percent=current.used_percent,
            duration_minutes=current.window_duration_mins,
        )

        if reset_evidence:
            expected = previous.resets_at is not None and now_timestamp >= previous.resets_at - 120
            evidence: dict[str, object] = {
                "account": new.account_key,
                "window": key,
                "from_reset": previous.resets_at,
                "to_reset": current.resets_at,
                "from_used": previous.used_percent,
                "to_used": current.used_percent,
            }
            if expected:
                events.append(
                    make_event(
                        "window_reset",
                        "window_reset_confirmed",
                        evidence,
                        payload,
                    )
                )
            elif confirmed:
                events.append(
                    make_event(
                        "significant_change",
                        "window_changed_early",
                        evidence,
                        payload,
                    )
                )
            else:
                needs_confirmation = True
        elif duration_changed:
            events.append(
                make_event(
                    "significant_change",
                    "window_duration_changed",
                    {
                        "account": new.account_key,
                        "window": key,
                        "duration": current.window_duration_mins,
                        "reset": current.resets_at,
                    },
                    payload,
                )
            )

    old_buckets = _bucket_map(old)
    for limit_id, current_bucket in _bucket_map(new).items():
        previous_bucket = old_buckets.get(limit_id)
        if previous_bucket and (
            previous_bucket.reached_type != current_bucket.reached_type
            or previous_bucket.spend_control_reached != current_bucket.spend_control_reached
        ):
            events.append(
                make_event(
                    "significant_change",
                    "backend_limit_status_changed",
                    {
                        "account": new.account_key,
                        "limit": limit_id,
                        "reached": current_bucket.reached_type,
                        "spend": current_bucket.spend_control_reached,
                    },
                    EventPayload(limit_label=current_bucket.limit_name or limit_id),
                )
            )

    if (
        old.ordinary_usage_status == "known"
        and new.ordinary_usage_status == "known"
        and old.ordinary_usage_allowed is False
        and new.ordinary_usage_allowed is True
    ):
        events.append(
            make_event(
                "usage_restored",
                "usage_restored_backend",
                {"account": new.account_key, "observed": new.observed_at},
            )
        )

    if new.reset_credits_status == "known" and new.reset_credits_available is not None and baseline:
        count_increased = new.reset_credits_available > baseline.available_count
        fresh_ids = [
            credit
            for credit in new.reset_credit_details or []
            if credit.id not in baseline.known_grants
            and credit.granted_at
            > int(datetime.fromisoformat(baseline.observed_at.replace("Z", "+00:00")).timestamp())
        ]
        if count_increased or fresh_ids:
            credit_evidence: dict[str, object] = {
                "account": new.account_key,
                "from": baseline.available_count,
                "to": new.reset_credits_available,
                "fresh": sorted(credit.id for credit in fresh_ids),
            }
            events.append(
                make_event(
                    "reset_credit_granted",
                    "reset_credit_granted",
                    credit_evidence,
                    EventPayload(available_count=new.reset_credits_available),
                )
            )

    return ChangeAnalysis(events, next_baseline, needs_confirmation)


def credit_expiry_events(snapshot: RateSnapshot, *, now: datetime) -> list[Event]:
    result: list[Event] = []
    now_ts = int(now.astimezone(UTC).timestamp())
    for credit in snapshot.reset_credit_details or []:
        if credit.status != "available" or credit.expires_at is None:
            continue
        remaining = credit.expires_at - now_ts
        if 0 < remaining <= 86_400:
            result.append(
                make_event(
                    "credit_expiring",
                    "credit_expiring_24h",
                    {
                        "account": snapshot.account_key,
                        "credit": credit.id,
                        "expires": credit.expires_at,
                    },
                    EventPayload(
                        credit_title=credit.title,
                        expires_at=credit.expires_at,
                    ),
                )
            )
    return result


def notification_enabled(event: Event, settings: NotificationSettings) -> bool:
    mapping = {
        "window_reset": settings.window_updates,
        "usage_restored": settings.usage_restored,
        "reset_credit_granted": settings.reset_credits,
        "significant_change": settings.significant_changes,
        "credit_expiring": settings.credit_expiry_reminder,
        "monitor_unavailable": settings.service_health,
        "monitor_recovered": settings.service_health,
        "auth_required": True,
        "account_changed": True,
    }
    return mapping[event.type]
