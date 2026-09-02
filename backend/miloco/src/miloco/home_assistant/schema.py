# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Home Assistant integration schemas."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class HaErrorCode(str, Enum):
    """Stable Home Assistant integration error codes."""

    NOT_CONFIGURED = "ha_not_configured"
    UNREACHABLE = "ha_unreachable"
    UNAUTHORIZED = "ha_unauthorized"
    TIMEOUT = "ha_timeout"
    INVALID_JSON = "ha_invalid_json"
    SERVICE_REJECTED = "ha_service_rejected"
    CONTROL_DISABLED = "ha_control_disabled"
    UNSUPPORTED_DOMAIN = "ha_unsupported_domain"


class HomeAssistantError(Exception):
    """Operational Home Assistant error with a stable public code."""

    def __init__(
        self,
        code: HaErrorCode,
        message: str,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class HaEntityState(BaseModel):
    """A single Home Assistant entity state returned by `/api/states`."""

    entity_id: str = Field(..., min_length=1)
    state: str = Field(default="unknown")
    attributes: dict[str, object] = Field(default_factory=dict)


class HaServiceCall(BaseModel):
    """Allowlisted Home Assistant service call translated from a Miloco spec."""

    domain: str = Field(..., min_length=1)
    service: str = Field(..., min_length=1)
    data: dict[str, object] = Field(default_factory=dict)


HaServiceCatalog = dict[str, set[str]]


class HomeAssistantConfigUpdate(BaseModel):
    """Config update payload accepted by Miloco's HA management API."""

    enabled: bool
    base_url: str = ""
    token: str | None = Field(default=None, repr=False)
    preserve_token: bool = False
    verify_tls: bool = True


class HomeAssistantPublicConfig(BaseModel):
    """Secret-free HA config readback."""

    enabled: bool
    base_url: str
    instance_key: str
    verify_tls: bool
    token_configured: bool
    token_mask: str = "••••••••"


class HomeAssistantStatus(BaseModel):
    """Management page status summary."""

    config: HomeAssistantPublicConfig
    configured: bool
    enabled: bool
    connected: bool
    error_code: HaErrorCode | None = None
    message: str = ""


class HomeAssistantTestResult(BaseModel):
    """Result of testing a proposed HA connection without saving it."""

    ok: bool
    connected: bool
    error_code: HaErrorCode | None = None
    message: str = ""


class HomeAssistantEntityPolicyUpdate(BaseModel):
    """Partial entity import/control policy update."""

    included: bool | None = None
    control_enabled: bool | None = None


HomeAssistantBulkSkippedReason = Literal[
    "invalid-entity-id",
    "not-found",
    "not-imported",
    "blocked-risk",
    "unsupported-domain",
    "service-unavailable",
]


class HomeAssistantEntityPolicyBulkSkipped(BaseModel):
    """One HA entity that was not updated by a bulk policy request."""

    entity_id: str
    reason: HomeAssistantBulkSkippedReason


class HomeAssistantEntityPolicyBulkUpdate(BaseModel):
    """Bulk HA entity import/control policy update."""

    entity_ids: list[str] = Field(..., min_length=1, max_length=1000)
    included: bool | None = None
    control_enabled: bool | None = None

    @field_validator("entity_ids")
    @classmethod
    def _normalize_entity_ids(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_value in values:
            value = raw_value.strip()
            if value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        return normalized

    @model_validator(mode="after")
    def _require_patch(self) -> "HomeAssistantEntityPolicyBulkUpdate":
        if self.included is None and self.control_enabled is None:
            raise ValueError("At least one Home Assistant policy field is required")
        return self


class HomeAssistantEntityView(BaseModel):
    """HA entity row rendered by the management UI."""

    entity_id: str
    name: str
    domain: str
    state: str = "unknown"
    room: str | None = None
    included: bool = False
    control_enabled: bool = False
    control_supported: bool = False
    control_blocked_reason: str | None = None
    last_seen_at: int | None = None
    last_control_at: int | None = None
    last_error: str | None = None


class HomeAssistantEntityPolicyBulkResult(BaseModel):
    """Bulk HA entity policy update result."""

    updated: list[HomeAssistantEntityView] = Field(default_factory=list)
    skipped: list[HomeAssistantEntityPolicyBulkSkipped] = Field(default_factory=list)
    updated_count: int = 0
    skipped_count: int = 0
