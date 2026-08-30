# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Home Assistant REST client contract tests."""

from __future__ import annotations

import httpx
import pytest

from miloco.home_assistant.client import HomeAssistantClient
from miloco.home_assistant.schema import HaErrorCode, HomeAssistantError


@pytest.mark.asyncio
async def test_client_sends_bearer_token() -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"message": "API running."})

    client = HomeAssistantClient(
        "http://ha.local:8123/",
        "secret-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await client.ping() == {"message": "API running."}
    finally:
        await client.aclose()

    assert seen["auth"] == "Bearer secret-token"
    assert seen["url"] == "http://ha.local:8123/api/"


@pytest.mark.asyncio
async def test_client_unauthorized_redacts_token() -> None:
    client = HomeAssistantClient(
        "http://ha.local:8123",
        "secret-token",
        transport=httpx.MockTransport(lambda request: httpx.Response(401, json={})),
    )
    try:
        with pytest.raises(HomeAssistantError) as exc:
            await client.get_states()
    finally:
        await client.aclose()

    assert exc.value.code == HaErrorCode.UNAUTHORIZED
    assert exc.value.status_code == 401
    assert "secret-token" not in str(exc.value)
    assert "secret-token" not in repr(exc.value)


@pytest.mark.asyncio
async def test_client_rejects_invalid_json() -> None:
    client = HomeAssistantClient(
        "http://ha.local:8123",
        "secret-token",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=b"not json")
        ),
    )
    try:
        with pytest.raises(HomeAssistantError) as exc:
            await client.get_config()
    finally:
        await client.aclose()

    assert exc.value.code == HaErrorCode.INVALID_JSON


@pytest.mark.asyncio
async def test_client_maps_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("slow", request=request)

    client = HomeAssistantClient(
        "http://ha.local:8123",
        "secret-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(HomeAssistantError) as exc:
            await client.get_services()
    finally:
        await client.aclose()

    assert exc.value.code == HaErrorCode.TIMEOUT


@pytest.mark.asyncio
async def test_client_posts_service_call() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.read().decode("utf-8")
        return httpx.Response(200, json=[{"context": {"id": "abc"}}])

    client = HomeAssistantClient(
        "http://ha.local:8123",
        "secret-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.call_service(
            "light", "turn_on", {"entity_id": "light.kitchen"}
        )
    finally:
        await client.aclose()

    assert seen["method"] == "POST"
    assert seen["url"] == "http://ha.local:8123/api/services/light/turn_on"
    assert '"entity_id":"light.kitchen"' in str(seen["body"])
    assert result == [{"context": {"id": "abc"}}]


@pytest.mark.asyncio
async def test_client_maps_service_rejection_status() -> None:
    client = HomeAssistantClient(
        "http://ha.local:8123",
        "secret-token",
        transport=httpx.MockTransport(lambda request: httpx.Response(404, json={})),
    )
    try:
        with pytest.raises(HomeAssistantError) as exc:
            await client.call_service(
                "light", "missing", {"entity_id": "light.kitchen"}
            )
    finally:
        await client.aclose()

    assert exc.value.code == HaErrorCode.SERVICE_REJECTED
    assert exc.value.status_code == 404
