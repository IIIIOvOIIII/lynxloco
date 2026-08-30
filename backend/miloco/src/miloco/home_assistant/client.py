# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Home Assistant REST API client."""

from __future__ import annotations

from typing import Any

import httpx

from miloco.home_assistant.schema import HaErrorCode, HomeAssistantError


class HomeAssistantClient:
    """Small async client for the Home Assistant REST API."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = 8.0,
        verify_tls: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        normalized_base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=normalized_base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=timeout_seconds,
            verify=verify_tls,
            transport=transport,
        )

    async def aclose(self) -> None:
        """Close the underlying HTTP connection pool."""
        await self._client.aclose()

    async def __aenter__(self) -> "HomeAssistantClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def ping(self) -> object:
        """Return the Home Assistant API status payload."""
        return await self._request("GET", "/api/")

    async def get_config(self) -> object:
        """Return Home Assistant instance configuration."""
        return await self._request("GET", "/api/config")

    async def get_states(self) -> object:
        """Return Home Assistant entity states."""
        return await self._request("GET", "/api/states")

    async def get_services(self) -> object:
        """Return Home Assistant service catalog."""
        return await self._request("GET", "/api/services")

    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, object],
    ) -> object:
        """Call an allowlisted Home Assistant service."""
        return await self._request(
            "POST",
            f"/api/services/{domain}/{service}",
            json_body=data,
        )

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, object] | None = None,
    ) -> Any:
        try:
            response = await self._client.request(method, path, json=json_body)
        except httpx.TimeoutException as exc:
            raise HomeAssistantError(
                HaErrorCode.TIMEOUT,
                "Home Assistant request timed out",
            ) from exc
        except httpx.HTTPError as exc:
            raise HomeAssistantError(
                HaErrorCode.UNREACHABLE,
                "Home Assistant is unreachable",
            ) from exc

        if response.status_code in {401, 403}:
            raise HomeAssistantError(
                HaErrorCode.UNAUTHORIZED,
                "Home Assistant token was rejected",
                response.status_code,
            )
        if response.status_code >= 400:
            raise HomeAssistantError(
                HaErrorCode.SERVICE_REJECTED,
                f"Home Assistant returned HTTP {response.status_code}",
                response.status_code,
            )

        try:
            return response.json()
        except ValueError as exc:
            raise HomeAssistantError(
                HaErrorCode.INVALID_JSON,
                "Home Assistant returned invalid JSON",
            ) from exc

