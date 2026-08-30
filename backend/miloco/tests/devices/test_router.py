# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Unified devices API route tests."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from miloco.config import reset_settings
from miloco.home_assistant.schema import HaErrorCode, HomeAssistantError
from miloco.main import app
from miloco.manager import get_manager


class _FakeMiotService:
    async def get_home_info(self, *, refresh: bool = False) -> dict[str, Any]:
        del refresh
        return {
            "home_name": "一尺山居",
            "devices": [
                {
                    "did": "miot-light-1",
                    "name": "米家灯",
                    "online": True,
                    "model": "miot.light",
                    "room": "客厅",
                    "category": "light",
                    "spec": {
                        "prop.2.1": {
                            "iid": "prop.2.1",
                            "type_name": "on",
                            "description": "开关",
                            "format": "bool",
                            "readable": True,
                            "writeable": True,
                        }
                    },
                }
            ],
            "scenes": [{"scene_id": "scene-1", "scene_name": "回家"}],
            "areas": [{"name": "客厅"}],
        }

    async def get_device_spec(self, did: str) -> dict[str, Any]:
        return (await self.get_home_info())["devices"][0] | {"did": did}

    async def control_device(self, did: str, request) -> dict[str, Any]:
        return {"did": did, "type": request.type, "results": [{"code": 0}]}

    async def trigger_scene(self, scene_id: str) -> bool:
        return scene_id == "scene-1"


class _FakeHaService:
    async def list_imported_devices(self, *, refresh: bool = False) -> list:
        del refresh
        return []

    async def list_scenes(self) -> list:
        return []

    async def control(self, entity_id: str, request) -> UnifiedActionResult:
        del request
        raise HomeAssistantError(
            HaErrorCode.CONTROL_DISABLED,
            f"Home Assistant entity '{entity_id}' control is disabled",
        )


@pytest.fixture(autouse=True)
def _isolated_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_SERVER__TOKEN", "test-token")
    reset_settings()
    manager = get_manager()
    monkeypatch.setattr(manager, "_miot_service", _FakeMiotService(), raising=False)
    monkeypatch.setattr(
        manager,
        "_home_assistant_service",
        _FakeHaService(),
        raising=False,
    )
    if hasattr(manager, "_devices_service"):
        delattr(manager, "_devices_service")
    yield
    reset_settings()


def test_devices_home_returns_unified_payload() -> None:
    client = TestClient(app)

    response = client.get(
        "/api/devices/home",
        headers={"Authorization": "Bearer test-token"},
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["home_name"] == "一尺山居"
    assert "devices" in body
    assert "scenes" in body
    assert "areas" in body
    assert body["devices"][0]["source"] == "miot"


def test_ha_control_disabled_is_rejected() -> None:
    client = TestClient(app)

    response = client.post(
        "/api/devices/ha%3Aprimary%3Alight.kitchen/control",
        headers={"Authorization": "Bearer test-token"},
        json={"type": "set_property", "iid": "on", "value": True},
    )

    assert response.status_code in {400, 404}
