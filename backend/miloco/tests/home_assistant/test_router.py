# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Home Assistant management API tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from miloco.config import reset_settings
from miloco.home_assistant.schema import HomeAssistantTestResult
from miloco.main import app
from miloco.manager import get_manager


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_SERVER__TOKEN", "test-token")
    reset_settings()
    yield
    reset_settings()


def test_home_assistant_config_readback_masks_token() -> None:
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-token"}

    response = client.post(
        "/api/home-assistant/config",
        headers=headers,
        json={
            "enabled": True,
            "base_url": "http://ha.local:8123",
            "token": "secret-token",
        },
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["enabled"] is True
    assert body["base_url"] == "http://ha.local:8123"
    assert body["token_configured"] is True
    assert "secret-token" not in response.text


def test_home_assistant_preserve_token_does_not_clear_saved_secret() -> None:
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-token"}

    first = client.post(
        "/api/home-assistant/config",
        headers=headers,
        json={
            "enabled": True,
            "base_url": "http://ha.local:8123",
            "token": "secret-token",
        },
    )
    second = client.post(
        "/api/home-assistant/config",
        headers=headers,
        json={
            "enabled": False,
            "base_url": "http://ha.local:8123",
            "preserve_token": True,
        },
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["data"]["token_configured"] is True
    assert "secret-token" not in second.text


def test_control_policy_rejects_blocked_domain() -> None:
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-token"}

    response = client.put(
        "/api/home-assistant/entities/lock.front_door/policy",
        headers=headers,
        json={"included": True, "control_enabled": True},
    )

    assert response.status_code == 400
    assert "secret-token" not in response.text


def test_home_assistant_test_alias_matches_public_spec(monkeypatch) -> None:
    class _FakeHaService:
        async def test_config(self, *, base_url: str, token: str, verify_tls: bool):
            assert base_url == "http://ha.local:8123"
            assert token == "secret-token"
            assert verify_tls is True
            return HomeAssistantTestResult(
                ok=True,
                connected=True,
                message="ok",
            )

    manager = get_manager()
    monkeypatch.setattr(
        manager,
        "_home_assistant_service",
        _FakeHaService(),
        raising=False,
    )
    client = TestClient(app)

    response = client.post(
        "/api/home-assistant/test",
        headers={"Authorization": "Bearer test-token"},
        json={
            "enabled": True,
            "base_url": "http://ha.local:8123",
            "token": "secret-token",
        },
    )

    assert response.status_code == 200
    assert response.json()["data"]["connected"] is True
    assert "secret-token" not in response.text


def test_bulk_policy_endpoint_forwards_request_and_masks_secrets(monkeypatch) -> None:
    from miloco.home_assistant.schema import (
        HomeAssistantEntityPolicyBulkResult,
        HomeAssistantEntityPolicyBulkUpdate,
        HomeAssistantEntityView,
    )

    class _FakeHaService:
        def __init__(self) -> None:
            self.received: HomeAssistantEntityPolicyBulkUpdate | None = None

        async def update_entity_policies(self, body: HomeAssistantEntityPolicyBulkUpdate):
            self.received = body
            return HomeAssistantEntityPolicyBulkResult(
                updated=[
                    HomeAssistantEntityView(
                        entity_id="light.kitchen",
                        name="Kitchen Light",
                        domain="light",
                        state="off",
                        room="Kitchen",
                        included=True,
                        control_enabled=False,
                        control_supported=True,
                    )
                ],
                skipped=[],
                updated_count=1,
                skipped_count=0,
            )

    fake_service = _FakeHaService()
    manager = get_manager()
    monkeypatch.setattr(
        manager,
        "_home_assistant_service",
        fake_service,
        raising=False,
    )

    client = TestClient(app)
    response = client.put(
        "/api/home-assistant/entities/policies",
        headers={"Authorization": "Bearer test-token"},
        json={
            "entity_ids": ["light.kitchen", " light.kitchen "],
            "included": True,
            "control_enabled": False,
        },
    )

    assert response.status_code == 200
    assert fake_service.received is not None
    assert fake_service.received.entity_ids == ["light.kitchen"]
    assert fake_service.received.included is True
    assert fake_service.received.control_enabled is False
    body = response.json()["data"]
    assert body["updated_count"] == 1
    assert body["skipped_count"] == 0
    assert body["updated"][0]["entity_id"] == "light.kitchen"
    assert "secret-token" not in response.text


def test_bulk_policy_endpoint_requires_a_patch() -> None:
    client = TestClient(app)
    response = client.put(
        "/api/home-assistant/entities/policies",
        headers={"Authorization": "Bearer test-token"},
        json={"entity_ids": ["light.kitchen"]},
    )

    assert response.status_code == 422
    assert "secret-token" not in response.text


def test_bulk_policy_endpoint_requires_auth(monkeypatch) -> None:
    monkeypatch.delenv("MILOCO_TEST_DEFAULT_SERVICE_AUTH", raising=False)
    client = TestClient(app)
    response = client.put(
        "/api/home-assistant/entities/policies",
        json={
            "entity_ids": ["light.kitchen"],
            "included": True,
            "control_enabled": False,
        },
    )

    assert response.status_code in {401, 403}
