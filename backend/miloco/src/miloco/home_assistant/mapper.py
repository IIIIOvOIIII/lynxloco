# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Map Home Assistant entities into Miloco's source-aware device model."""

from __future__ import annotations

from typing import Any

from miloco.config.settings import HomeAssistantEntityPolicy
from miloco.devices.schema import (
    DeviceSource,
    UnifiedDeviceInfo,
    UnifiedSpecEntry,
    make_ha_device_id,
)
from miloco.home_assistant.schema import (
    HaEntityState,
    HaErrorCode,
    HaServiceCall,
    HaServiceCatalog,
    HomeAssistantError,
)

SUPPORTED_CONTROL_DOMAINS = {
    "switch",
    "light",
    "fan",
    "cover",
    "climate",
    "scene",
    "script",
}
BLOCKED_CONTROL_DOMAINS = {
    "lock",
    "alarm_control_panel",
    "valve",
    "water_heater",
    "siren",
    "button",
}
_ACCESS_CONTROL_COVER_CLASSES = {"garage", "gate", "door"}
_OFF_STATES = {"off", "closed", "idle", "standby"}
_ON_STATES = {"on", "open", "opening", "closing", "heat", "cool", "dry", "fan_only"}


def domain_of(entity_id: str) -> str:
    """Return the Home Assistant entity domain."""
    return entity_id.split(".", 1)[0]


def control_blocked_reason(
    entity: HaEntityState,
    services: HaServiceCatalog,
) -> str | None:
    """Return why an entity cannot be controlled by Miloco, if blocked."""
    domain = domain_of(entity.entity_id)
    if domain in BLOCKED_CONTROL_DOMAINS:
        return "blocked-risk"
    if domain not in SUPPORTED_CONTROL_DOMAINS:
        return "unsupported-domain"
    if domain == "cover":
        device_class = str(entity.attributes.get("device_class") or "").lower()
        if device_class in _ACCESS_CONTROL_COVER_CLASSES:
            return "blocked-risk"
    if not services.get(domain):
        return "service-unavailable"
    return None


def map_entity_to_device(
    entity: HaEntityState,
    services: HaServiceCatalog,
    policy: HomeAssistantEntityPolicy,
    instance_key: str,
) -> UnifiedDeviceInfo | None:
    """Map an imported HA entity into a unified device, or skip if not included."""
    if not policy.included:
        return None

    domain = domain_of(entity.entity_id)
    device = UnifiedDeviceInfo(
        did=make_ha_device_id(instance_key, entity.entity_id),
        source=DeviceSource.HOME_ASSISTANT,
        source_label="Home Assistant",
        name=str(entity.attributes.get("friendly_name") or entity.entity_id),
        online=entity.state not in {"unavailable", "unknown"},
        model=f"home_assistant.{domain}",
        room=str(
            entity.attributes.get("area")
            or entity.attributes.get("area_id")
            or entity.attributes.get("room")
            or "未分配"
        ),
        category=domain,
        spec=_spec_for_entity(entity, services),
        included=policy.included,
        control_enabled=policy.control_enabled,
    )

    reason = control_blocked_reason(entity, services)
    if not policy.control_enabled:
        return _strip_control_specs(device, "control-disabled")
    if reason is not None:
        return _strip_control_specs(device, reason)
    return device


def map_entity_status_properties(
    entity: HaEntityState,
    services: HaServiceCatalog,
    iids: list[str] | None = None,
) -> list[dict[str, object]]:
    """Map the current HA entity state into Miloco-style readable properties."""
    spec = _spec_for_entity(entity, services)
    selected = iids if iids is not None else list(spec)
    return [
        {"iid": iid, "value": _status_value(entity, iid), "code": 0}
        for iid in selected
        if iid in spec
    ]


def control_spec_to_service(
    entity_id: str,
    iid: str,
    value: object,
    services: HaServiceCatalog,
) -> HaServiceCall:
    """Translate a Miloco spec IID into a safe HA service call."""
    domain = domain_of(entity_id)
    if domain not in SUPPORTED_CONTROL_DOMAINS or domain in BLOCKED_CONTROL_DOMAINS:
        raise HomeAssistantError(
            HaErrorCode.UNSUPPORTED_DOMAIN,
            f"Unsupported Home Assistant domain: {domain}",
        )

    if domain in {"light", "switch", "fan"} and iid == "on":
        service = "turn_on" if _truthy_bool(value) else "turn_off"
        return _service_call(domain, service, {"entity_id": entity_id}, services)

    if domain == "light" and iid == "brightness":
        return _service_call(
            "light",
            "turn_on",
            {"entity_id": entity_id, "brightness": _bounded_int(value, 0, 255)},
            services,
        )

    if domain == "fan" and iid == "percentage":
        return _service_call(
            "fan",
            "set_percentage",
            {"entity_id": entity_id, "percentage": _bounded_int(value, 0, 100)},
            services,
        )

    if domain == "cover" and iid in {"open", "close", "stop"}:
        return _service_call(
            "cover",
            {"open": "open_cover", "close": "close_cover", "stop": "stop_cover"}[iid],
            {"entity_id": entity_id},
            services,
        )

    if domain == "cover" and iid == "position":
        return _service_call(
            "cover",
            "set_cover_position",
            {"entity_id": entity_id, "position": _bounded_int(value, 0, 100)},
            services,
        )

    if domain == "climate" and iid == "hvac_mode":
        return _service_call(
            "climate",
            "set_hvac_mode",
            {"entity_id": entity_id, "hvac_mode": str(value)},
            services,
        )

    if domain == "climate" and iid == "temperature":
        return _service_call(
            "climate",
            "set_temperature",
            {"entity_id": entity_id, "temperature": value},
            services,
        )

    if domain == "climate" and iid == "fan_mode":
        return _service_call(
            "climate",
            "set_fan_mode",
            {"entity_id": entity_id, "fan_mode": str(value)},
            services,
        )

    if domain in {"scene", "script"} and iid == "activate":
        return _service_call(
            domain,
            "turn_on",
            {"entity_id": entity_id},
            services,
        )

    raise HomeAssistantError(
        HaErrorCode.UNSUPPORTED_DOMAIN,
        f"Unsupported Home Assistant spec iid: {iid}",
    )


def _spec_for_entity(
    entity: HaEntityState,
    services: HaServiceCatalog,
) -> dict[str, UnifiedSpecEntry]:
    domain = domain_of(entity.entity_id)
    if domain in {"scene", "script"}:
        return _scene_like_spec(domain, services)

    spec: dict[str, UnifiedSpecEntry] = {
        "state": UnifiedSpecEntry(
            iid="state",
            type_name="state",
            description="当前状态",
            format="string",
            readable=True,
        )
    }

    if domain in {"light", "switch", "fan"}:
        spec["on"] = UnifiedSpecEntry(
            iid="on",
            type_name="on",
            description="开关",
            format="bool",
            readable=True,
            writeable=_has_services(domain, services, "turn_on", "turn_off"),
        )

    if domain == "light" and _has_services(domain, services, "turn_on"):
        if "brightness" in entity.attributes:
            spec["brightness"] = UnifiedSpecEntry(
                iid="brightness",
                type_name="brightness",
                description="亮度",
                format="uint8",
                readable=True,
                writeable=True,
                value_range=[0, 255, 1],
            )

    if domain == "fan" and _has_services(domain, services, "set_percentage"):
        spec["percentage"] = UnifiedSpecEntry(
            iid="percentage",
            type_name="percentage",
            description="风量百分比",
            format="uint8",
            readable=True,
            writeable=True,
            value_range=[0, 100, 1],
        )

    if domain == "cover":
        if _has_services(domain, services, "open_cover"):
            spec["open"] = UnifiedSpecEntry(
                iid="open",
                type_name="open",
                description="打开",
                executable=True,
            )
        if _has_services(domain, services, "close_cover"):
            spec["close"] = UnifiedSpecEntry(
                iid="close",
                type_name="close",
                description="关闭",
                executable=True,
            )
        if _has_services(domain, services, "stop_cover"):
            spec["stop"] = UnifiedSpecEntry(
                iid="stop",
                type_name="stop",
                description="停止",
                executable=True,
            )
        if _has_services(domain, services, "set_cover_position"):
            spec["position"] = UnifiedSpecEntry(
                iid="position",
                type_name="position",
                description="位置",
                format="uint8",
                readable=True,
                writeable=True,
                value_range=[0, 100, 1],
            )

    if domain == "climate":
        modes = [
            str(mode)
            for mode in entity.attributes.get("hvac_modes", [])
            if isinstance(mode, str)
        ]
        if _has_services(domain, services, "set_hvac_mode"):
            spec["hvac_mode"] = UnifiedSpecEntry(
                iid="hvac_mode",
                type_name="hvac_mode",
                description="空调模式",
                format="string",
                readable=True,
                writeable=True,
                value_list=[
                    {"value": mode, "description": mode}
                    for mode in modes
                ]
                or None,
            )
        if _has_services(domain, services, "set_temperature"):
            spec["temperature"] = UnifiedSpecEntry(
                iid="temperature",
                type_name="temperature",
                description="目标温度",
                format="float",
                readable=True,
                writeable=True,
                unit="°C",
                value_range=_temperature_range(entity.attributes),
            )
        fan_modes = [
            str(mode)
            for mode in entity.attributes.get("fan_modes", [])
            if isinstance(mode, str)
        ]
        if _has_services(domain, services, "set_fan_mode") and fan_modes:
            spec["fan_mode"] = UnifiedSpecEntry(
                iid="fan_mode",
                type_name="fan_mode",
                description="风速",
                format="string",
                readable=True,
                writeable=True,
                value_list=[
                    {"value": mode, "description": mode}
                    for mode in fan_modes
                ],
            )

    return spec


def _scene_like_spec(
    domain: str,
    services: HaServiceCatalog,
) -> dict[str, UnifiedSpecEntry]:
    return {
        "activate": UnifiedSpecEntry(
            iid="activate",
            type_name="activate",
            description="执行",
            executable=_has_services(domain, services, "turn_on"),
        )
    }


def _strip_control_specs(
    device: UnifiedDeviceInfo,
    reason: str,
) -> UnifiedDeviceInfo:
    spec = {
        key: item.model_copy(update={"writeable": False, "executable": False})
        for key, item in device.spec.items()
    }
    return device.model_copy(
        update={
            "spec": spec,
            "control_enabled": False,
            "read_only_reason": reason,
        }
    )


def _has_services(
    domain: str,
    services: HaServiceCatalog,
    *names: str,
) -> bool:
    available = services.get(domain, set())
    return all(name in available for name in names)


def _service_call(
    domain: str,
    service: str,
    data: dict[str, object],
    services: HaServiceCatalog,
) -> HaServiceCall:
    if not _has_services(domain, services, service):
        raise HomeAssistantError(
            HaErrorCode.SERVICE_REJECTED,
            f"Home Assistant service not available: {domain}.{service}",
        )
    return HaServiceCall(domain=domain, service=service, data=data)


def _truthy_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on", "open"}
    return bool(value)


def _bounded_int(value: object, minimum: int, maximum: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise HomeAssistantError(
            HaErrorCode.SERVICE_REJECTED,
            f"Value must be an integer between {minimum} and {maximum}",
        ) from exc
    return max(minimum, min(maximum, number))


def _status_value(entity: HaEntityState, iid: str) -> object:
    domain = domain_of(entity.entity_id)
    attributes = entity.attributes
    if iid == "state":
        return entity.state
    if iid == "on" and domain in {"light", "switch", "fan"}:
        if entity.state in {"unknown", "unavailable"}:
            return None
        return entity.state not in _OFF_STATES
    if iid == "brightness":
        return attributes.get("brightness")
    if iid == "percentage":
        return attributes.get("percentage")
    if iid == "position":
        if "current_position" in attributes:
            return attributes.get("current_position")
        return attributes.get("current_cover_position")
    if iid == "hvac_mode" and domain == "climate":
        if entity.state in {"unknown", "unavailable"}:
            return None
        return entity.state
    if iid == "temperature" and domain == "climate":
        return attributes.get("temperature")
    if iid == "fan_mode" and domain == "climate":
        return attributes.get("fan_mode")
    return None


def _temperature_range(attributes: dict[str, Any]) -> list[float] | None:
    min_temp = attributes.get("min_temp")
    max_temp = attributes.get("max_temp")
    target_step = attributes.get("target_temp_step") or attributes.get("precision") or 0.5
    if not isinstance(min_temp, int | float) or not isinstance(max_temp, int | float):
        return None
    if not isinstance(target_step, int | float):
        target_step = 0.5
    return [float(min_temp), float(max_temp), float(target_step)]
