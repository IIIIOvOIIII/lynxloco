# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Home Assistant entity-to-device mapper tests."""

from __future__ import annotations

import pytest

from miloco.config.settings import HomeAssistantEntityPolicy
from miloco.home_assistant.mapper import map_entity_to_device, control_spec_to_service
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


def test_control_spec_to_service_rejects_unknown_iid() -> None:
    with pytest.raises(HomeAssistantError) as exc:
        control_spec_to_service(
            "light.kitchen",
            "raw-service",
            True,
            {"light": {"turn_on", "turn_off"}},
        )

    assert exc.value.code == HaErrorCode.UNSUPPORTED_DOMAIN

