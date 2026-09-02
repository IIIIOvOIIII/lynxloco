# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Home Assistant entity-to-device mapper tests."""

from __future__ import annotations

import pytest
from miloco.config.settings import HomeAssistantEntityPolicy
from miloco.home_assistant.mapper import (
    control_spec_to_service,
    map_entity_status_properties,
    map_entity_to_device,
)
from miloco.home_assistant.schema import HaEntityState, HaErrorCode, HomeAssistantError


def test_discovered_entity_not_included_returns_none() -> None:
    entity = HaEntityState(
        entity_id="light.kitchen",
        state="off",
        attributes={"friendly_name": "厨房灯"},
    )
    policy = HomeAssistantEntityPolicy(
        entity_id="light.kitchen",
        included=False,
    )

    assert (
        map_entity_to_device(
            entity,
            {"light": {"turn_on", "turn_off"}},
            policy,
            "primary",
        )
        is None
    )


def test_imported_light_without_control_is_read_only() -> None:
    entity = HaEntityState(
        entity_id="light.kitchen",
        state="off",
        attributes={"friendly_name": "厨房灯"},
    )
    policy = HomeAssistantEntityPolicy(
        entity_id="light.kitchen",
        included=True,
        control_enabled=False,
    )

    device = map_entity_to_device(
        entity,
        {"light": {"turn_on", "turn_off"}},
        policy,
        "primary",
    )

    assert device is not None
    assert device.did == "ha:primary:light.kitchen"
    assert device.source.value == "home_assistant"
    assert device.control_enabled is False
    assert all(
        not item.writeable and not item.executable for item in device.spec.values()
    )


def test_imported_control_enabled_light_has_on_spec() -> None:
    entity = HaEntityState(
        entity_id="light.kitchen",
        state="off",
        attributes={"friendly_name": "厨房灯"},
    )
    policy = HomeAssistantEntityPolicy(
        entity_id="light.kitchen",
        included=True,
        control_enabled=True,
    )

    device = map_entity_to_device(
        entity,
        {"light": {"turn_on", "turn_off"}},
        policy,
        "primary",
    )

    assert device is not None
    assert device.spec["on"].readable is True
    assert device.spec["on"].writeable is True


def test_imported_control_enabled_climate_exposes_fan_mode_spec() -> None:
    entity = HaEntityState(
        entity_id="climate.zhonghong_hvac_1_0",
        state="cool",
        attributes={
            "friendly_name": "客厅空调",
            "fan_mode": "high",
            "fan_modes": ["auto", "low", "medium", "high", "silent"],
            "hvac_modes": ["off", "cool", "heat", "dry", "fan_only"],
            "min_temp": 18,
            "max_temp": 30,
            "target_temp_step": 1,
        },
    )
    policy = HomeAssistantEntityPolicy(
        entity_id="climate.zhonghong_hvac_1_0",
        included=True,
        control_enabled=True,
    )

    device = map_entity_to_device(
        entity,
        {"climate": {"set_fan_mode", "set_hvac_mode", "set_temperature"}},
        policy,
        "primary",
    )

    assert device is not None
    assert device.spec["fan_mode"].iid == "fan_mode"
    assert device.spec["fan_mode"].description == "风速"
    assert device.spec["fan_mode"].format == "string"
    assert device.spec["fan_mode"].readable is True
    assert device.spec["fan_mode"].writeable is True
    assert device.spec["fan_mode"].value_list == [
        {"value": "auto", "description": "auto"},
        {"value": "low", "description": "low"},
        {"value": "medium", "description": "medium"},
        {"value": "high", "description": "high"},
        {"value": "silent", "description": "silent"},
    ]


def test_climate_status_maps_current_ha_attributes() -> None:
    entity = HaEntityState(
        entity_id="climate.zhonghong_hvac_1_2",
        state="cool",
        attributes={
            "friendly_name": "主卧空调",
            "fan_mode": "medium",
            "fan_modes": ["auto", "low", "medium", "high"],
            "hvac_modes": ["off", "cool", "heat", "dry", "fan_only"],
            "temperature": 25,
            "min_temp": 18,
            "max_temp": 30,
            "target_temp_step": 1,
        },
    )

    assert map_entity_status_properties(
        entity,
        {"climate": {"set_fan_mode", "set_hvac_mode", "set_temperature"}},
        ["state", "hvac_mode", "temperature", "fan_mode"],
    ) == [
        {"iid": "state", "value": "cool", "code": 0},
        {"iid": "hvac_mode", "value": "cool", "code": 0},
        {"iid": "temperature", "value": 25, "code": 0},
        {"iid": "fan_mode", "value": "medium", "code": 0},
    ]


def test_blocked_lock_never_exposes_writable_specs() -> None:
    entity = HaEntityState(
        entity_id="lock.front_door",
        state="locked",
        attributes={"friendly_name": "前门锁"},
    )
    policy = HomeAssistantEntityPolicy(
        entity_id="lock.front_door",
        included=True,
        control_enabled=True,
    )

    device = map_entity_to_device(
        entity,
        {"lock": {"lock", "unlock"}},
        policy,
        "primary",
    )

    assert device is not None
    assert device.control_enabled is False
    assert device.read_only_reason == "blocked-risk"
    assert all(
        not item.writeable and not item.executable for item in device.spec.values()
    )


def test_control_spec_to_service_maps_light_on() -> None:
    call = control_spec_to_service(
        "light.kitchen",
        "on",
        True,
        {"light": {"turn_on", "turn_off"}},
    )

    assert call.domain == "light"
    assert call.service == "turn_on"
    assert call.data == {"entity_id": "light.kitchen"}


def test_control_spec_to_service_maps_climate_fan_mode() -> None:
    call = control_spec_to_service(
        "climate.zhonghong_hvac_1_0",
        "fan_mode",
        "silent",
        {"climate": {"set_fan_mode", "set_hvac_mode", "set_temperature"}},
    )

    assert call.domain == "climate"
    assert call.service == "set_fan_mode"
    assert call.data == {
        "entity_id": "climate.zhonghong_hvac_1_0",
        "fan_mode": "silent",
    }


def test_control_spec_to_service_rejects_unknown_iid() -> None:
    with pytest.raises(HomeAssistantError) as exc:
        control_spec_to_service(
            "light.kitchen",
            "raw-service",
            True,
            {"light": {"turn_on", "turn_off"}},
        )

    assert exc.value.code == HaErrorCode.UNSUPPORTED_DOMAIN
