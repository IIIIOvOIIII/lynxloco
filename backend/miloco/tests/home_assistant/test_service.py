# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Home Assistant bulk entity policy service tests."""

from __future__ import annotations

from typing import Any

import miloco.home_assistant.service as service_module
import pytest
from miloco.config import get_settings, reset_settings
from miloco.home_assistant.schema import (
    HaEntityState,
    HomeAssistantEntityPolicyBulkPolicyUpdate,
    HomeAssistantEntityPolicyBulkUpdate,
)
from miloco.home_assistant.service import HomeAssistantService
from miloco.utils.agent_config import update_shared_config


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_SERVER__TOKEN", "test-token")
    reset_settings()
    yield
    reset_settings()


def _configure_ha(*, entities: dict[str, dict[str, Any]] | None = None) -> None:
    update_shared_config(
        home_assistant={
            "enabled": True,
            "base_url": "http://ha.local:8123",
            "token": "secret-token",
            "verify_tls": False,
            "entities": entities or {},
        }
    )
    reset_settings()


def _state(entity_id: str, *, name: str, state: str = "off", **attributes: Any) -> HaEntityState:
    return HaEntityState(
        entity_id=entity_id,
        state=state,
        attributes={"friendly_name": name, **attributes},
    )


@pytest.mark.asyncio
async def test_bulk_import_writes_once_and_defaults_read_only(monkeypatch) -> None:
    _configure_ha()
    service = HomeAssistantService()

    async def fake_states_and_services(settings, *, refresh: bool):
        assert settings.base_url == "http://ha.local:8123"
        assert refresh is True
        return (
            [
                _state("light.kitchen", name="Kitchen Light", area="Kitchen"),
                _state("sensor.temperature", name="Temperature", area="Kitchen"),
            ],
            {"light": {"turn_on", "turn_off"}},
        )

    calls: list[dict[str, Any]] = []
    real_update_shared_config = service_module.update_shared_config

    def recording_update_shared_config(**kwargs):
        calls.append(kwargs)
        return real_update_shared_config(**kwargs)

    monkeypatch.setattr(service, "_states_and_services", fake_states_and_services)
    monkeypatch.setattr(service_module, "update_shared_config", recording_update_shared_config)

    result = await service.update_entity_policies(
        HomeAssistantEntityPolicyBulkUpdate(
            entity_ids=["light.kitchen", " light.kitchen ", "sensor.temperature"],
            included=True,
            control_enabled=False,
        )
    )

    assert result.updated_count == 2
    assert result.skipped_count == 0
    assert [entity.entity_id for entity in result.updated] == [
        "light.kitchen",
        "sensor.temperature",
    ]
    assert all(entity.included for entity in result.updated)
    assert all(not entity.control_enabled for entity in result.updated)
    assert len(calls) == 1

    settings = get_settings().home_assistant
    assert settings.entities["light.kitchen"].included is True
    assert settings.entities["light.kitchen"].control_enabled is False
    assert settings.entities["sensor.temperature"].included is True
    assert settings.entities["sensor.temperature"].control_enabled is False


@pytest.mark.asyncio
async def test_bulk_allow_control_skips_unsafe_and_not_imported(monkeypatch) -> None:
    _configure_ha(
        entities={
            "light.kitchen": {
                "entity_id": "light.kitchen",
                "included": True,
                "control_enabled": False,
            },
            "lock.front_door": {
                "entity_id": "lock.front_door",
                "included": True,
                "control_enabled": False,
            },
        }
    )
    service = HomeAssistantService()

    async def fake_states_and_services(settings, *, refresh: bool):
        del settings
        assert refresh is True
        return (
            [
                _state("light.kitchen", name="Kitchen Light"),
                _state("lock.front_door", name="Front Door", state="locked"),
                _state("switch.freezer", name="Freezer"),
            ],
            {
                "light": {"turn_on", "turn_off"},
                "lock": {"lock", "unlock"},
                "switch": {"turn_on", "turn_off"},
            },
        )

    monkeypatch.setattr(service, "_states_and_services", fake_states_and_services)

    result = await service.update_entity_policies(
        HomeAssistantEntityPolicyBulkUpdate(
            entity_ids=["light.kitchen", "lock.front_door", "switch.freezer"],
            control_enabled=True,
        )
    )

    assert result.updated_count == 1
    assert result.updated[0].entity_id == "light.kitchen"
    assert result.updated[0].control_enabled is True
    assert [(item.entity_id, item.reason) for item in result.skipped] == [
        ("lock.front_door", "blocked-risk"),
        ("switch.freezer", "not-imported"),
    ]

    settings = get_settings().home_assistant
    assert settings.entities["light.kitchen"].control_enabled is True
    assert settings.entities["lock.front_door"].control_enabled is False
    assert "switch.freezer" not in settings.entities


@pytest.mark.asyncio
async def test_bulk_import_with_control_request_imports_read_only_and_reports_skip(monkeypatch) -> None:
    _configure_ha()
    service = HomeAssistantService()

    async def fake_states_and_services(settings, *, refresh: bool):
        del settings
        assert refresh is True
        return (
            [_state("light.kitchen", name="Kitchen Light")],
            {"light": {"turn_on", "turn_off"}},
        )

    monkeypatch.setattr(service, "_states_and_services", fake_states_and_services)

    result = await service.update_entity_policies(
        HomeAssistantEntityPolicyBulkUpdate(
            entity_ids=["light.kitchen"],
            included=True,
            control_enabled=True,
        )
    )

    assert result.updated_count == 1
    assert result.updated[0].included is True
    assert result.updated[0].control_enabled is False
    assert [(item.entity_id, item.reason) for item in result.skipped] == [
        ("light.kitchen", "not-imported")
    ]

    policy = get_settings().home_assistant.entities["light.kitchen"]
    assert policy.included is True
    assert policy.control_enabled is False


@pytest.mark.asyncio
async def test_bulk_permission_reduction_does_not_call_home_assistant(monkeypatch) -> None:
    _configure_ha(
        entities={
            "light.kitchen": {
                "entity_id": "light.kitchen",
                "included": True,
                "control_enabled": True,
            }
        }
    )
    service = HomeAssistantService()

    async def fail_if_live_ha_is_called(settings, *, refresh: bool):
        del settings, refresh
        raise AssertionError("permission reduction must not require live Home Assistant")

    monkeypatch.setattr(service, "_states_and_services", fail_if_live_ha_is_called)

    result = await service.update_entity_policies(
        HomeAssistantEntityPolicyBulkUpdate(
            entity_ids=["light.kitchen"],
            included=False,
        )
    )

    assert result.updated_count == 1
    assert result.skipped_count == 0
    assert result.updated[0].included is False
    assert result.updated[0].control_enabled is False

    policy = get_settings().home_assistant.entities["light.kitchen"]
    assert policy.included is False
    assert policy.control_enabled is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entity_id",
    ["light.kitchen", "lock.front_door"],
)
async def test_bulk_remove_takes_precedence_over_control_enablement(
    monkeypatch,
    entity_id: str,
) -> None:
    _configure_ha(
        entities={
            entity_id: {
                "entity_id": entity_id,
                "included": True,
                "control_enabled": True,
            }
        }
    )
    service = HomeAssistantService()

    async def fail_if_live_ha_is_called(settings, *, refresh: bool):
        del settings, refresh
        raise AssertionError("import removal must not require live Home Assistant")

    monkeypatch.setattr(service, "_states_and_services", fail_if_live_ha_is_called)

    result = await service.update_entity_policies(
        HomeAssistantEntityPolicyBulkUpdate(
            entity_ids=[entity_id],
            included=False,
            control_enabled=True,
        )
    )

    assert result.updated_count == 1
    assert result.skipped_count == 0
    policy = get_settings().home_assistant.entities[entity_id]
    assert policy.included is False
    assert policy.control_enabled is False


@pytest.mark.asyncio
async def test_bulk_first_import_clears_latent_control_enabled(monkeypatch) -> None:
    _configure_ha(
        entities={
            "light.kitchen": {
                "entity_id": "light.kitchen",
                "included": False,
                "control_enabled": True,
            }
        }
    )
    service = HomeAssistantService()

    async def fake_states_and_services(settings, *, refresh: bool):
        del settings
        assert refresh is True
        return (
            [_state("light.kitchen", name="Kitchen Light")],
            {"light": {"turn_on", "turn_off"}},
        )

    monkeypatch.setattr(service, "_states_and_services", fake_states_and_services)

    result = await service.update_entity_policies(
        HomeAssistantEntityPolicyBulkUpdate(
            entity_ids=["light.kitchen"],
            included=True,
        )
    )

    assert result.updated_count == 1
    assert result.updated[0].included is True
    assert result.updated[0].control_enabled is False
    policy = get_settings().home_assistant.entities["light.kitchen"]
    assert policy.included is True
    assert policy.control_enabled is False


@pytest.mark.asyncio
async def test_bulk_permission_reduction_returns_policy_only_update(monkeypatch) -> None:
    _configure_ha(
        entities={
            "light.kitchen": {
                "entity_id": "light.kitchen",
                "included": True,
                "control_enabled": True,
            }
        }
    )
    service = HomeAssistantService()

    async def fail_if_live_ha_is_called(settings, *, refresh: bool):
        del settings, refresh
        raise AssertionError("permission reduction must not require live Home Assistant")

    monkeypatch.setattr(service, "_states_and_services", fail_if_live_ha_is_called)

    result = await service.update_entity_policies(
        HomeAssistantEntityPolicyBulkUpdate(
            entity_ids=["light.kitchen"],
            control_enabled=False,
        )
    )

    assert result.updated_count == 1
    assert isinstance(result.updated[0], HomeAssistantEntityPolicyBulkPolicyUpdate)
    assert result.updated[0].model_dump() == {
        "entity_id": "light.kitchen",
        "included": True,
        "control_enabled": False,
    }
