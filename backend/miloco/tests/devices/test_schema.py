# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Unified smart-home device schema tests."""

from __future__ import annotations

from miloco.devices.schema import DeviceSource, make_ha_device_id, parse_ha_device_id


def test_ha_device_id_round_trips() -> None:
    did = make_ha_device_id("primary", "light.kitchen")

    assert did == "ha:primary:light.kitchen"
    assert parse_ha_device_id(did) == ("primary", "light.kitchen")


def test_miot_id_is_not_parsed_as_ha() -> None:
    assert parse_ha_device_id("123456789") is None


def test_source_values_are_stable() -> None:
    assert DeviceSource.MIOT.value == "miot"
    assert DeviceSource.HOME_ASSISTANT.value == "home_assistant"

