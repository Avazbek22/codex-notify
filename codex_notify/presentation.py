from __future__ import annotations

import html
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

from .i18n import tr
from .models import AppState, Event, Language, RateBucket, RateSnapshot, RateWindow, Settings
from .timeutil import from_unix, parse_utc, utc_now


def format_duration(minutes: int | None, language: Language) -> str:
    if minutes is None:
        return tr(language, "status.window_unknown_duration")
    if minutes and minutes % 1440 == 0:
        return tr(language, "duration.days", value=minutes // 1440)
    if minutes and minutes % 60 == 0:
        return tr(language, "duration.hours", value=minutes // 60)
    return tr(language, "duration.minutes", value=minutes)


def format_datetime(value: str | None, timezone: str, language: Language) -> str:
    parsed = parse_utc(value)
    if parsed is None:
        return tr(language, "status.reset_unknown")
    return _format_datetime_value(parsed, timezone, language)


def format_unix(value: int | None, timezone: str, language: Language) -> str:
    parsed = from_unix(value)
    if parsed is None:
        return tr(language, "status.reset_unknown")
    return _format_datetime_value(parsed, timezone, language)


def _format_datetime_value(value: datetime, timezone: str, language: Language) -> str:
    local = value.astimezone(ZoneInfo(timezone))
    month = tr(language, f"month.{local.month}")
    zone = local.tzname() or timezone
    if language == "en":
        date = f"{month} {local.day}"
    else:
        date = f"{local.day} {month}"
    if local.year != utc_now().astimezone(ZoneInfo(timezone)).year:
        date = f"{date}, {local.year}"
    return f"{date}, {local:%H:%M} {zone}"


def format_relative(value: str | None, timezone: str, language: Language) -> str:
    parsed = parse_utc(value)
    if parsed is None:
        return tr(language, "status.never")
    seconds = max(0, int((utc_now() - parsed).total_seconds()))
    if seconds < 90:
        return tr(language, "status.just_now")
    if seconds < 3600:
        return tr(language, "status.minutes_ago", value=seconds // 60)
    if seconds < 86_400:
        return tr(language, "status.hours_ago", value=seconds // 3600)
    return format_datetime(value, timezone, language)


def _plan_suffix(state: AppState) -> str:
    if not state.account or not state.account.plan:
        return ""
    return f" · {html.escape(state.account.plan.title())}"


def _window_kind(window: RateWindow, language: Language) -> str:
    return tr(language, f"status.{window.window}")


def _limit_label(bucket: RateBucket) -> str:
    label = bucket.limit_name or bucket.limit_id
    return "Codex" if label.casefold() == "codex" else label


def render_status(settings: Settings, state: AppState) -> str:
    language = settings.language
    lines = [tr(language, "status.title")]
    plan = _plan_suffix(state)
    snapshot = state.snapshot
    if state.auth_status == "reauth_required":
        lines.append(tr(language, "status.reauth"))
    elif state.auth_status != "connected":
        lines.append(tr(language, "status.disconnected"))
    elif state.last_error:
        lines.append(tr(language, "status.connection_stale", plan=plan))
    elif (
        snapshot
        and snapshot.ordinary_usage_status == "known"
        and snapshot.ordinary_usage_allowed is False
    ):
        lines.append(tr(language, "status.blocked", plan=plan))
    elif (
        snapshot
        and snapshot.ordinary_usage_status == "known"
        and snapshot.ordinary_usage_allowed is True
    ):
        lines.append(tr(language, "status.available", plan=plan))
    else:
        lines.append(tr(language, "status.connected", plan=plan))

    if state.last_error:
        lines.extend(["", tr(language, "status.stale")])
    if snapshot:
        lines.extend(["", *_render_compact_snapshot(snapshot, settings)])
    else:
        lines.extend(["", tr(language, "status.no_data")])

    updated = format_relative(state.last_success_at, settings.timezone, language)
    lines.append("")
    if settings.paused:
        lines.append(tr(language, "status.updated_paused", when=updated))
    else:
        lines.append(
            tr(
                language,
                "status.updated",
                when=updated,
                minutes=settings.interval_minutes,
            )
        )
    if state.last_error and state.next_check_at:
        lines.append(
            tr(
                language,
                "status.next_attempt",
                when=format_datetime(state.next_check_at, settings.timezone, language),
            )
        )
    return "\n".join(lines)


def _render_compact_snapshot(snapshot: RateSnapshot, settings: Settings) -> list[str]:
    language = settings.language
    lines: list[str] = []
    window_count = sum(len(bucket.windows) for bucket in snapshot.buckets)
    if window_count == 0:
        lines.append(tr(language, "status.no_windows"))
    for bucket in snapshot.buckets:
        label = html.escape(_limit_label(bucket))
        if not bucket.windows:
            key = "status.unlimited" if bucket.unlimited is True else "status.window_missing"
            lines.append(tr(language, key, label=label))
            continue
        durations = Counter(window.window_duration_mins for window in bucket.windows)
        for window in bucket.windows:
            kind = (
                _window_kind(window, language) if durations[window.window_duration_mins] > 1 else ""
            )
            lines.append(
                tr(
                    language,
                    "status.window",
                    label=label,
                    duration=format_duration(window.window_duration_mins, language),
                    kind=kind,
                    remaining=max(0, 100 - window.used_percent),
                    reset=format_unix(window.resets_at, settings.timezone, language),
                )
            )
    if snapshot.reset_credits_status == "known" and snapshot.reset_credits_available:
        count = snapshot.reset_credits_available
        suffix = "s" if language == "en" and count != 1 else ""
        credit_line = tr(language, "status.credits", count=count, suffix=suffix)
        expiries = [
            credit.expires_at
            for credit in snapshot.reset_credit_details or []
            if credit.status == "available" and credit.expires_at is not None
        ]
        if expiries:
            credit_line += tr(
                language,
                "status.credit_expiry",
                expiry=format_unix(min(expiries), settings.timezone, language),
            )
        lines.extend(["", credit_line])
    return lines


def render_details(settings: Settings, state: AppState) -> str:
    language = settings.language
    lines = [tr(language, "details.title")]
    if state.account:
        if state.account.masked_email:
            lines.append(
                tr(language, "details.account", email=html.escape(state.account.masked_email))
            )
        if state.account.plan:
            lines.append(tr(language, "details.plan", plan=html.escape(state.account.plan.title())))
    snapshot = state.snapshot
    if not snapshot:
        lines.extend(["", tr(language, "status.no_data")])
    else:
        lines.append("")
        lines.extend(_render_detailed_snapshot(snapshot, settings))
    if state.last_error:
        lines.extend(["", tr(language, "status.stale")])
    lines.extend(
        [
            "",
            tr(
                language,
                "details.last_success",
                when=format_datetime(state.last_success_at, settings.timezone, language),
            ),
            tr(
                language,
                "details.next_attempt",
                when=format_datetime(state.next_check_at, settings.timezone, language),
            ),
            tr(
                language,
                "details.monitoring",
                state=tr(
                    language,
                    "details.monitoring_paused" if settings.paused else "details.monitoring_on",
                ),
                minutes=settings.interval_minutes,
            ),
        ]
    )
    return "\n".join(lines)


def _render_detailed_snapshot(snapshot: RateSnapshot, settings: Settings) -> list[str]:
    language = settings.language
    lines: list[str] = []
    if not snapshot.buckets:
        lines.append(tr(language, "status.no_windows"))
    for bucket in snapshot.buckets:
        label = html.escape(_limit_label(bucket))
        if not bucket.windows:
            key = "status.unlimited" if bucket.unlimited is True else "status.window_missing"
            lines.append(tr(language, key, label=label))
        for window in bucket.windows:
            values = {
                "label": label,
                "kind": tr(language, f"status.{window.window}").removeprefix(" · "),
                "used": window.used_percent,
                "remaining": max(0, 100 - window.used_percent),
                "reset": format_unix(window.resets_at, settings.timezone, language),
            }
            if window.window_duration_mins is None:
                lines.append(tr(language, "details.window_no_duration", **values))
            else:
                lines.append(
                    tr(
                        language,
                        "details.window",
                        duration=format_duration(window.window_duration_mins, language),
                        **values,
                    )
                )
        if bucket.reached_type:
            lines.append(
                tr(language, "details.backend_limit", value=html.escape(bucket.reached_type))
            )
    if snapshot.ordinary_usage_status != "known":
        lines.append(tr(language, "details.usage_unknown"))
    elif snapshot.ordinary_usage_allowed:
        lines.append(tr(language, "details.usage_allowed"))
    else:
        lines.append(tr(language, "details.usage_blocked"))
    if snapshot.reset_credits_status == "known":
        lines.append(tr(language, "details.credits", count=snapshot.reset_credits_available or 0))
        for credit in snapshot.reset_credit_details or []:
            if credit.status == "available":
                lines.append(
                    tr(
                        language,
                        "details.credit",
                        title=html.escape(credit.title or tr(language, "credit.default_title")),
                        expiry=format_unix(credit.expires_at, settings.timezone, language),
                    )
                )
    elif snapshot.reset_credits_status == "unknown":
        lines.append(tr(language, "details.credits_unknown"))
    else:
        lines.append(tr(language, "details.credits_unsupported"))
    return lines


def render_event(
    event: Event, language: Language, timezone: str, *, rationale: bool = False
) -> str:
    if event.code == "legacy_v1" and event.legacy_text:
        title = html.escape(event.legacy_text.title)
        details = html.escape(event.legacy_text.details)
        body = f"<b>{title}</b>\n{details}\n<i>{tr(language, 'history.legacy')}</i>"
        if rationale and event.legacy_text.rationale:
            body += "\n" + tr(
                language, "event.why", reason=html.escape(event.legacy_text.rationale)
            )
        return body

    payload = event.payload
    window = payload.window or "primary"
    values: dict[str, object] = {
        "label": html.escape(payload.limit_label or "Codex"),
        "window": tr(language, f"status.{window}").removeprefix(" · "),
        "before": payload.previous_used_percent
        if payload.previous_used_percent is not None
        else "?",
        "after": payload.used_percent if payload.used_percent is not None else "?",
        "remaining": (
            max(0, 100 - payload.used_percent) if payload.used_percent is not None else "?"
        ),
        "duration": format_duration(payload.duration_minutes, language),
        "count": payload.available_count if payload.available_count is not None else "?",
        "title": html.escape(payload.credit_title or tr(language, "credit.default_title")),
        "expiry": format_unix(payload.expires_at, timezone, language),
    }
    title = tr(language, f"event.{event.code}.title", **values)
    details = tr(language, f"event.{event.code}.details", **values)
    body = f"<b>{title}</b>\n{details}"
    if rationale:
        reason = tr(language, f"event.{event.code}.reason", **values)
        body += "\n" + tr(language, "event.why", reason=reason)
    return body


def render_event_bundle(
    events: list[Event], language: Language, timezone: str, detected_at: str
) -> str:
    blocks = [f"🔔 {render_event(event, language, timezone)}" for event in events]
    blocks.append(
        tr(
            language,
            "event.detected",
            when=format_datetime(detected_at, timezone, language),
        )
    )
    return "\n\n".join(blocks)
