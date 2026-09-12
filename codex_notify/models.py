from __future__ import annotations

from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = 2
HISTORY_LIMIT = 200

Language = Literal["en", "ru"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NotificationSettings(StrictModel):
    window_updates: bool = True
    usage_restored: bool = True
    reset_credits: bool = True
    significant_changes: bool = True
    credit_expiry_reminder: bool = False
    service_health: bool = True


class BindingState(StrictModel):
    token_hash: str
    expires_at: str
    created_at: str


class Settings(StrictModel):
    schema_version: Literal[2] = 2
    owner_id: int | None = None
    interval_minutes: int = Field(default=30, ge=5, le=1440)
    timezone: str = "UTC"
    language: Language = "en"
    paused: bool = False
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    binding: BindingState | None = None

    @model_validator(mode="before")
    @classmethod
    def migrate_v1(cls, value: Any) -> Any:
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            return value
        migrated = dict(value)
        migrated["schema_version"] = 2
        # v1 was Russian-only. Preserve that experience for existing installations;
        # new Settings instances use English and UTC.
        migrated["language"] = "ru"
        return migrated

    @field_validator("owner_id")
    @classmethod
    def validate_owner(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("owner_id must be a positive Telegram numeric ID")
        return value

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("timezone must be an IANA timezone") from exc
        return value


class AccountIdentity(StrictModel):
    key: str
    masked_email: str | None = None
    plan: str | None = None


class RateWindow(StrictModel):
    limit_id: str
    limit_name: str | None = None
    window: Literal["primary", "secondary"]
    used_percent: int = Field(ge=0, le=100)
    window_duration_mins: int | None = Field(default=None, ge=0)
    resets_at: int | None = None

    @property
    def key(self) -> str:
        return f"{self.limit_id}:{self.window}"


class RateBucket(StrictModel):
    limit_id: str
    limit_name: str | None = None
    plan: str | None = None
    unlimited: bool | None = None
    reached_type: str | None = None
    spend_control_reached: bool | None = None
    windows: list[RateWindow] = Field(default_factory=list)


class ResetCredit(StrictModel):
    id: str
    status: str
    reset_type: str
    granted_at: int
    expires_at: int | None = None
    title: str | None = None


class RateSnapshot(StrictModel):
    observed_at: str
    account_key: str
    account_id_present: bool
    buckets: list[RateBucket] = Field(default_factory=list)
    ordinary_usage_status: Literal["unsupported", "unknown", "known"]
    ordinary_usage_allowed: bool | None = None
    reset_credits_status: Literal["unsupported", "unknown", "known"]
    reset_credits_available: int | None = Field(default=None, ge=0)
    reset_credit_details: list[ResetCredit] | None = None


class CreditBaseline(StrictModel):
    observed_at: str
    available_count: int
    known_grants: dict[str, int] = Field(default_factory=dict)


EventType = Literal[
    "window_reset",
    "usage_restored",
    "reset_credit_granted",
    "significant_change",
    "credit_expiring",
    "monitor_unavailable",
    "monitor_recovered",
    "auth_required",
    "account_changed",
]

EventCode = Literal[
    "window_reset_confirmed",
    "window_changed_early",
    "window_duration_changed",
    "backend_limit_status_changed",
    "usage_restored_backend",
    "reset_credit_granted",
    "credit_expiring_24h",
    "monitor_unavailable",
    "monitor_recovered",
    "auth_required",
    "account_changed",
    "legacy_v1",
]


class EventPayload(StrictModel):
    limit_label: str | None = None
    window: Literal["primary", "secondary"] | None = None
    previous_used_percent: int | None = Field(default=None, ge=0, le=100)
    used_percent: int | None = Field(default=None, ge=0, le=100)
    duration_minutes: int | None = Field(default=None, ge=0)
    available_count: int | None = Field(default=None, ge=0)
    credit_title: str | None = None
    expires_at: int | None = None


class LegacyEventText(StrictModel):
    title: str
    details: str
    rationale: str


class Event(StrictModel):
    id: str
    type: EventType
    detected_at: str
    code: EventCode
    payload: EventPayload = Field(default_factory=EventPayload)
    legacy_text: LegacyEventText | None = None


class OutboxItem(StrictModel):
    id: str
    event_ids: list[str]
    events: list[Event] = Field(default_factory=list)
    legacy_text: str | None = None
    created_at: str
    detected_at: str
    attempts: int = 0
    not_before: str | None = None


class PendingLogin(StrictModel):
    login_id: str
    started_at: str
    message_chat_id: int | None = None
    message_id: int | None = None


class AppState(StrictModel):
    schema_version: Literal[2] = 2
    account: AccountIdentity | None = None
    auth_status: Literal["unknown", "disconnected", "connected", "reauth_required"] = "unknown"
    snapshot: RateSnapshot | None = None
    credit_baseline: CreditBaseline | None = None
    events: list[Event] = Field(default_factory=list)
    dedupe_event_ids: list[str] = Field(default_factory=list)
    outbox: list[OutboxItem] = Field(default_factory=list)
    last_success_at: str | None = None
    next_check_at: str | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    outage_started_at: str | None = None
    outage_notified: bool = False
    auth_required_notified: bool = False
    pending_login: PendingLogin | None = None
    post_reset_checks: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def migrate_v1(cls, value: Any) -> Any:
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            return value
        migrated = dict(value)
        migrated["schema_version"] = 2
        if value.get("last_error") is not None:
            migrated["last_error"] = (
                "codex_auth_required"
                if value.get("auth_status") in {"disconnected", "reauth_required"}
                else "codex_unavailable"
            )
        migrated_events: list[dict[str, Any]] = []
        raw_events = value.get("events", [])
        if not isinstance(raw_events, list):
            raise ValueError("v1 events must be a list")
        for raw_event in raw_events:
            if not isinstance(raw_event, dict):
                raise ValueError("v1 event must be an object")
            required_event = {"id", "type", "detected_at", "title", "details", "rationale"}
            if not required_event <= raw_event.keys():
                raise ValueError("v1 event is incomplete")
            migrated_events.append(
                {
                    "id": raw_event["id"],
                    "type": raw_event["type"],
                    "detected_at": raw_event["detected_at"],
                    "code": "legacy_v1",
                    "payload": {},
                    "legacy_text": {
                        "title": raw_event["title"],
                        "details": raw_event["details"],
                        "rationale": raw_event["rationale"],
                    },
                }
            )
        migrated["events"] = migrated_events
        migrated_outbox: list[dict[str, Any]] = []
        raw_outbox = value.get("outbox", [])
        if not isinstance(raw_outbox, list):
            raise ValueError("v1 outbox must be a list")
        for raw_item in raw_outbox:
            if not isinstance(raw_item, dict):
                raise ValueError("v1 outbox item must be an object")
            item = dict(raw_item)
            if not isinstance(item.get("text"), str):
                raise ValueError("v1 outbox item has no text")
            item["events"] = []
            item["legacy_text"] = item.pop("text")
            migrated_outbox.append(item)
        migrated["outbox"] = migrated_outbox
        return migrated

    def bounded(self) -> AppState:
        self.events = self.events[-HISTORY_LIMIT:]
        self.dedupe_event_ids = self.dedupe_event_ids[-1000:]
        self.post_reset_checks = self.post_reset_checks[-100:]
        return self
