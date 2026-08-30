# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Home Assistant integration schemas."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


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

