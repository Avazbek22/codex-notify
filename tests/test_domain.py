from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from codex_notify.domain import analyze_changes, credit_expiry_events, normalize_snapshot

from .conftest import OMITTED, rate_payload

ACCOUNT = {"type": "chatgpt", "email": "owner@example.com", "planType": "plus"}


def test_normalizes_redacted_fixture_from_v0_154_schema() -> None:
    fixture = Path(__file__).parent / "fixtures" / "rate_limits_v0_154.json"
    raw = json.loads(fixture.read_text(encoding="utf-8"))
    snapshot, identity = normalize_snapshot(
        raw,
        {"type": "chatgpt", "email": "fixture@example.invalid", "planType": "plus"},
    )
    assert identity.plan == "plus"
    assert snapshot.account_id_present
    assert snapshot.reset_credits_available == 2
    assert snapshot.buckets


def snap(**kwargs: object):
    return normalize_snapshot(rate_payload(**kwargs), ACCOUNT)[0]


def event_types(analysis: object) -> set[str]:
    return {event.type for event in analysis.events}  # type: ignore[attr-defined]


def test_normal_window_reset_needs_both_usage_drop_and_advanced_timestamp() -> None:
    now = datetime.fromtimestamp(2_010, UTC)
    old = snap(used=90, reset=2_000)
    new = snap(used=5, reset=3_800)
    result = analyze_changes(old, new, None, now=now)
    assert event_types(result) == {"window_reset"}
    assert "прежнее окно" in result.events[0].rationale


def test_timestamp_change_alone_is_not_called_a_reset() -> None:
    old = snap(used=40, reset=2_000)
    new = snap(used=40, reset=3_000)
    result = analyze_changes(old, new, None, now=datetime.fromtimestamp(2_010, UTC))
    assert "window_reset" not in event_types(result)
    assert result.events == []


def test_early_drop_requires_one_confirmation_and_remains_ambiguous() -> None:
    old = snap(used=90, reset=5_000)
    new = snap(used=5, reset=8_000)
    now = datetime.fromtimestamp(2_000, UTC)
    first = analyze_changes(old, new, None, now=now)
    assert first.requires_confirmation
    assert first.events == []
    confirmed = analyze_changes(old, new, None, now=now, confirmed=True)
    assert event_types(confirmed) == {"significant_change"}
    assert "подарком" in confirmed.events[0].rationale


def test_new_reset_credit_uses_authoritative_count() -> None:
    first = snap(credits={"availableCount": 1, "credits": None})
    baseline = analyze_changes(None, first, None, now=datetime.now(UTC)).credit_baseline
    second = snap(credits={"availableCount": 2, "credits": []})
    result = analyze_changes(first, second, baseline, now=datetime.now(UTC))
    assert event_types(result) == {"reset_credit_granted"}


def test_incomplete_credit_details_do_not_invent_an_old_grant() -> None:
    first = snap(credits={"availableCount": 2, "credits": None})
    baseline = analyze_changes(None, first, None, now=datetime.now(UTC)).credit_baseline
    assert baseline is not None
    old_grant = (
        int(datetime.fromisoformat(baseline.observed_at.replace("Z", "+00:00")).timestamp()) - 5
    )
    second = snap(
        credits={
            "availableCount": 2,
            "credits": [
                {
                    "id": "old-row-first-seen",
                    "status": "available",
                    "resetType": "codexRateLimits",
                    "grantedAt": old_grant,
                    "expiresAt": None,
                }
            ],
        }
    )
    result = analyze_changes(first, second, baseline, now=datetime.now(UTC))
    assert "reset_credit_granted" not in event_types(result)


def test_null_then_return_does_not_announce_a_credit() -> None:
    first = snap(credits={"availableCount": 1, "credits": None})
    baseline = analyze_changes(None, first, None, now=datetime.now(UTC)).credit_baseline
    missing = snap(credits=None)
    absent_result = analyze_changes(first, missing, baseline, now=datetime.now(UTC))
    assert absent_result.credit_baseline == baseline
    returned = snap(credits={"availableCount": 1, "credits": []})
    result = analyze_changes(
        missing, returned, absent_result.credit_baseline, now=datetime.now(UTC)
    )
    assert "reset_credit_granted" not in event_types(result)


def test_first_observation_and_account_change_are_baselines() -> None:
    first = snap()
    assert analyze_changes(None, first, None, now=datetime.now(UTC)).events == []
    other = snap(account_id="acct-B", used=0, reset=9_000)
    assert analyze_changes(first, other, None, now=datetime.now(UTC)).events == []


def test_unknown_usage_never_becomes_a_recovery_even_if_one_window_resets() -> None:
    old = snap(used=100, reset=2_000, ordinary=OMITTED, second_used=100)
    new = snap(used=0, reset=3_000, ordinary=None, second_used=100)
    result = analyze_changes(old, new, None, now=datetime.fromtimestamp(2_010, UTC))
    assert "usage_restored" not in event_types(result)


def test_only_explicit_global_backend_flag_causes_usage_recovery() -> None:
    old = snap(ordinary=False)
    new = snap(ordinary=True)
    result = analyze_changes(old, new, None, now=datetime.now(UTC))
    assert "usage_restored" in event_types(result)


def test_duration_change_is_not_mislabeled_as_reset() -> None:
    old = snap(used=90, reset=2_000, duration=300)
    new = snap(used=5, reset=4_000, duration=60)
    result = analyze_changes(old, new, None, now=datetime.fromtimestamp(2_010, UTC))
    assert "window_reset" not in event_types(result)
    assert "significant_change" in event_types(result)


def test_multibucket_view_does_not_double_count_legacy_view() -> None:
    snapshot = snap()
    assert len(snapshot.buckets) == 1
    assert len(snapshot.buckets[0].windows) == 1


def test_credit_expiry_reminder_requires_available_credit_with_known_date() -> None:
    now = datetime.fromtimestamp(2_000, UTC)
    snapshot = snap(
        credits={
            "availableCount": 1,
            "credits": [
                {
                    "id": "expiring",
                    "status": "available",
                    "resetType": "codexRateLimits",
                    "grantedAt": 1_000,
                    "expiresAt": 2_000 + 86_000,
                },
                {
                    "id": "unknown-expiry",
                    "status": "available",
                    "resetType": "codexRateLimits",
                    "grantedAt": 1_000,
                    "expiresAt": None,
                },
            ],
        }
    )
    events = credit_expiry_events(snapshot, now=now)
    assert len(events) == 1
    assert events[0].type == "credit_expiring"
