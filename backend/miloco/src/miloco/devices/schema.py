# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Source-aware smart-home device schemas."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field


class DeviceSource(str, Enum):
    """Smart-home provider backing a Miloco device."""

    MIOT = "miot"
    HOME_ASSISTANT = "home_assistant"


class UnifiedSpecEntry(BaseModel):
    """Normalized capability entry exposed to Web, CLI, rules, and Agent."""

    iid: str = Field(..., min_length=1)
    type_name: str = Field(..., min_length=1)
    description: str = ""
    service_type_name: str = ""
    service_description: str = ""
    prop_description: str = ""
    format: str = ""
    readable: bool = False
    writeable: bool = False
    executable: bool = False
    unit: str | None = None
    value_list: list[dict[str, object]] | None = None
    value_range: list[float] | None = None


class UnifiedDeviceInfo(BaseModel):
    """Normalized device record across MIoT and Home Assistant."""

    did: str = Field(..., min_length=1)
    source: DeviceSource
    source_label: str
    name: str
    online: bool = False
    model: str | None = None
    room: str | None = None
    category: str | None = None
    spec: dict[str, UnifiedSpecEntry] = Field(default_factory=dict)
    included: bool = True
    control_enabled: bool = True
    read_only_reason: str | None = None


class UnifiedSceneInfo(BaseModel):
    """Normalized scene record across MIoT and Home Assistant."""

    scene_id: str = Field(..., min_length=1)
    scene_name: str = Field(..., min_length=1)
    source: DeviceSource = DeviceSource.MIOT
    source_label: str = "MIoT"
    executable: bool = True


class UnifiedHomeInfo(BaseModel):
    """Merged smart-home inventory returned by `/api/devices/home`."""

    home_name: str = ""
    devices: list[UnifiedDeviceInfo] = Field(default_factory=list)
    scenes: list[UnifiedSceneInfo] = Field(default_factory=list)
    areas: list[dict[str, object]] = Field(default_factory=list)


class UnifiedPropertyItem(BaseModel):
    """Property update entry for batch control requests."""

    iid: str = Field(..., min_length=1)
    value: Any = None


class UnifiedDeviceControlRequest(BaseModel):
    """Provider-neutral control request using Miloco spec IIDs."""

    type: Literal["set_property", "set_properties", "call_action"]
    iid: str | None = None
    value: Any = None
    properties: list[UnifiedPropertyItem] | None = None
    params: list[Any] | None = None

    def to_miot_request(self):
        """Convert to the existing MIoT request DTO without changing MIoT routes."""
        from miloco.miot.schema import DeviceControlRequest, PropertyItem

        return DeviceControlRequest(
            type=self.type,
            iid=self.iid,
            value=self.value,
            properties=[
                PropertyItem(iid=item.iid, value=item.value)
                for item in (self.properties or [])
            ]
            or None,
            params=self.params,
        )


class UnifiedActionResult(BaseModel):
    """Normalized control result."""

    success: bool = False
    source: DeviceSource
    did: str
    message: str = ""
    data: object | None = None
    code: str | int | None = None


def make_ha_device_id(instance_key: str, entity_id: str) -> str:
    """Return Miloco's stable synthetic device ID for an HA entity."""
    return f"ha:{instance_key}:{entity_id}"


def parse_ha_device_id(device_id: str) -> tuple[str, str] | None:
    """Parse a Miloco HA synthetic device ID, or return None for non-HA IDs."""
    parts = device_id.split(":", 2)
    if len(parts) != 3 or parts[0] != "ha" or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]
