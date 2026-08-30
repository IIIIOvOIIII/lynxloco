# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""Home Assistant integration service."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable

from pydantic import ValidationError

from miloco.config import get_settings, reset_settings
from miloco.config.settings import HomeAssistantEntityPolicy, HomeAssistantSettings
from miloco.devices.schema import (
    DeviceSource,
    UnifiedActionResult,
    UnifiedDeviceControlRequest,
    UnifiedDeviceInfo,
    UnifiedSceneInfo,
)
from miloco.home_assistant.client import HomeAssistantClient
from miloco.home_assistant.mapper import (
    control_blocked_reason,
    control_spec_to_service,
    domain_of,
    map_entity_to_device,
)
from miloco.home_assistant.schema import (
    HaEntityState,
    HaErrorCode,
    HaServiceCatalog,
    HomeAssistantConfigUpdate,
    HomeAssistantEntityView,
    HomeAssistantError,
    HomeAssistantPublicConfig,
    HomeAssistantStatus,
    HomeAssistantTestResult,
)
from miloco.middleware.exceptions import BadRequestException, ResourceNotFoundException
from miloco.utils.agent_config import update_shared_config

logger = logging.getLogger(__name__)


class HomeAssistantService:
    """Coordinate HA settings, discovery, policy, and safe controls."""

    def public_config(self) -> HomeAssistantPublicConfig:
        """Return HA config without exposing the long-lived access token."""
        settings = get_settings().home_assistant
        return HomeAssistantPublicConfig(
            enabled=settings.enabled,
            base_url=settings.base_url,
            instance_key=settings.instance_key,
            verify_tls=settings.verify_tls,
            token_configured=bool(settings.token),
        )

    async def status(self) -> HomeAssistantStatus:
        """Return connection status without throwing for expected HA failures."""
        settings = get_settings().home_assistant
        configured = bool(settings.base_url and settings.token)
        if not settings.enabled or not configured:
            return HomeAssistantStatus(
                config=self.public_config(),
                configured=configured,
                enabled=settings.enabled,
                connected=False,
                error_code=None if configured else HaErrorCode.NOT_CONFIGURED,
                message="Home Assistant is disabled"
                if not settings.enabled
                else "Home Assistant config is incomplete",
            )

        try:
            await self._ping_current()
        except HomeAssistantError as exc:
            return HomeAssistantStatus(
                config=self.public_config(),
                configured=True,
                enabled=True,
                connected=False,
                error_code=exc.code,
                message=str(exc),
            )
        return HomeAssistantStatus(
            config=self.public_config(),
            configured=True,
            enabled=True,
            connected=True,
            message="Home Assistant connected",
        )

    async def test_config(
        self,
        *,
        base_url: str,
        token: str,
        verify_tls: bool,
    ) -> HomeAssistantTestResult:
        """Test a proposed HA endpoint/token without saving it."""
        try:
            config = HomeAssistantSettings(
                enabled=True,
                base_url=base_url,
                token=token,
                verify_tls=verify_tls,
            )
        except ValidationError as exc:
            raise BadRequestException("Home Assistant config invalid") from exc

        if not config.base_url or not config.token:
            return HomeAssistantTestResult(
                ok=False,
                connected=False,
                error_code=HaErrorCode.NOT_CONFIGURED,
                message="Home Assistant base_url and token are required",
            )

        try:
            async with self._client_from_config(config) as client:
                await client.ping()
        except HomeAssistantError as exc:
            return HomeAssistantTestResult(
                ok=False,
                connected=False,
                error_code=exc.code,
                message=str(exc),
            )
        return HomeAssistantTestResult(
            ok=True,
            connected=True,
            message="Home Assistant connected",
        )

    def save_config(
        self,
        update: HomeAssistantConfigUpdate,
    ) -> HomeAssistantPublicConfig:
        """Persist HA config through the shared config writer."""
        current = get_settings().home_assistant
        token = current.token if update.preserve_token else (update.token or "")
        payload = {
            "enabled": update.enabled,
            "base_url": update.base_url,
            "token": token,
            "instance_key": current.instance_key,
            "verify_tls": update.verify_tls,
            "timeout_seconds": current.timeout_seconds,
            "state_cache_ttl_seconds": current.state_cache_ttl_seconds,
            "entities": {
                entity_id: policy.model_dump()
                for entity_id, policy in current.entities.items()
            },
        }
        try:
            HomeAssistantSettings(**payload)
        except ValidationError as exc:
            raise BadRequestException("Home Assistant config invalid") from exc

        update_shared_config(home_assistant=payload)
        reset_settings()
        return self.public_config()

    async def list_entities(self, *, refresh: bool = False) -> list[HomeAssistantEntityView]:
        """List discoverable HA entities joined with local Miloco policy."""
        settings = get_settings().home_assistant
        if not self._is_configured(settings):
            return []
        states, services = await self._states_and_services(settings, refresh=refresh)
        return [
            self._entity_view(entity, services, self._policy_for(settings, entity.entity_id))
            for entity in states
        ]

    async def list_imported_devices(
        self,
        *,
        refresh: bool = False,
    ) -> list[UnifiedDeviceInfo]:
        """Return imported HA entities as unified Miloco devices."""
        settings = get_settings().home_assistant
        if not self._is_configured(settings):
            return []
        states, services = await self._states_and_services(settings, refresh=refresh)
        devices: list[UnifiedDeviceInfo] = []
        for entity in states:
            policy = self._policy_for(settings, entity.entity_id)
            device = map_entity_to_device(
                entity,
                services,
                policy,
                settings.instance_key,
            )
            if device is not None:
                devices.append(device)
        return devices

    async def get_device(
        self,
        entity_id: str,
        *,
        refresh: bool = False,
    ) -> UnifiedDeviceInfo:
        """Return a single imported HA entity as a unified device."""
        settings = get_settings().home_assistant
        if not self._is_configured(settings):
            raise HomeAssistantError(
                HaErrorCode.NOT_CONFIGURED,
                "Home Assistant config is incomplete",
            )
        states, services = await self._states_and_services(settings, refresh=refresh)
        entity = self._find_entity(states, entity_id)
        policy = self._policy_for(settings, entity.entity_id)
        device = map_entity_to_device(entity, services, policy, settings.instance_key)
        if device is None:
            raise ResourceNotFoundException(f"Home Assistant entity '{entity_id}' is not imported")
        return device

    async def list_scenes(self) -> list[UnifiedSceneInfo]:
        """Expose imported HA scene/script entities as executable scenes."""
        devices = await self.list_imported_devices()
        return [
            UnifiedSceneInfo(
                scene_id=device.did,
                scene_name=device.name,
                source=DeviceSource.HOME_ASSISTANT,
                source_label="Home Assistant",
                executable=device.control_enabled,
            )
            for device in devices
            if device.category in {"scene", "script"}
        ]

    def update_entity_policy(
        self,
        entity_id: str,
        included: bool | None,
        control_enabled: bool | None,
    ) -> HomeAssistantEntityView:
        """Persist import/control policy for a single HA entity."""
        settings = get_settings().home_assistant
        current = self._policy_for(settings, entity_id)

        next_included = current.included if included is None else included
        next_control_enabled = (
            current.control_enabled if control_enabled is None else control_enabled
        )
        synthetic_entity = HaEntityState(entity_id=entity_id, state="unknown")
        synthetic_services = {domain_of(entity_id): {"turn_on", "turn_off"}}
        reason = control_blocked_reason(synthetic_entity, synthetic_services)
        if next_control_enabled and reason in {"blocked-risk", "unsupported-domain"}:
            raise BadRequestException(
                f"Home Assistant entity '{entity_id}' cannot be controlled: {reason}"
            )

        next_policy = HomeAssistantEntityPolicy(
            entity_id=entity_id,
            included=next_included,
            control_enabled=next_control_enabled,
            last_seen_at=current.last_seen_at,
            last_control_at=current.last_control_at,
            last_error=current.last_error,
        )
        entities = {
            key: value.model_dump()
            for key, value in settings.entities.items()
        }
        entities[entity_id] = next_policy.model_dump()
        update_shared_config(home_assistant={"entities": entities})
        reset_settings()
        return self._entity_view(
            synthetic_entity,
            synthetic_services,
            next_policy,
        )

    async def control(
        self,
        entity_id: str,
        request: UnifiedDeviceControlRequest,
    ) -> UnifiedActionResult:
        """Execute one allowlisted HA service translated from a Miloco IID."""
        settings = get_settings().home_assistant
        if not self._is_configured(settings):
            raise HomeAssistantError(
                HaErrorCode.NOT_CONFIGURED,
                "Home Assistant config is incomplete",
            )
        states, services = await self._states_and_services(settings, refresh=False)
        entity = self._find_entity(states, entity_id)
        policy = self._policy_for(settings, entity_id)
        if not policy.included:
            raise ResourceNotFoundException(f"Home Assistant entity '{entity_id}' is not imported")
        if not policy.control_enabled:
            raise HomeAssistantError(
                HaErrorCode.CONTROL_DISABLED,
                "Home Assistant entity control is disabled",
            )
        reason = control_blocked_reason(entity, services)
        if reason is not None:
            raise HomeAssistantError(
                HaErrorCode.UNSUPPORTED_DOMAIN,
                f"Home Assistant entity cannot be controlled: {reason}",
            )

        if request.type == "set_properties":
            calls = [
                control_spec_to_service(entity_id, item.iid, item.value, services)
                for item in (request.properties or [])
            ]
        else:
            if request.iid is None:
                raise BadRequestException("iid is required")
            calls = [
                control_spec_to_service(
                    entity_id,
                    request.iid,
                    request.value if request.type == "set_property" else True,
                    services,
                )
            ]

        async with self._client_from_config(settings) as client:
            results = [
                await client.call_service(call.domain, call.service, call.data)
                for call in calls
            ]
        self._touch_policy(settings, entity_id, last_control_at=int(time.time() * 1000))
        return UnifiedActionResult(
            success=True,
            source=DeviceSource.HOME_ASSISTANT,
            did=entity_id,
            message="Home Assistant service call executed",
            data={"results": results},
        )

    async def _ping_current(self) -> None:
        settings = get_settings().home_assistant
        async with self._client_from_config(settings) as client:
            await client.ping()

    def _client_from_config(
        self,
        config: HomeAssistantSettings,
    ) -> HomeAssistantClient:
        if not self._is_configured(config):
            raise HomeAssistantError(
                HaErrorCode.NOT_CONFIGURED,
                "Home Assistant config is incomplete",
            )
        return HomeAssistantClient(
            config.base_url,
            config.token,
            timeout_seconds=config.timeout_seconds,
            verify_tls=config.verify_tls,
        )

    def _is_configured(self, config: HomeAssistantSettings) -> bool:
        return bool(config.enabled and config.base_url and config.token)

    async def _states_and_services(
        self,
        settings: HomeAssistantSettings,
        *,
        refresh: bool,
    ) -> tuple[list[HaEntityState], HaServiceCatalog]:
        # `refresh` is currently reserved for a future cache layer; the REST API is
        # queried live for MVP so policy changes are reflected immediately.
        del refresh
        async with self._client_from_config(settings) as client:
            states_payload = await client.get_states()
            services_payload = await client.get_services()
        return _parse_states(states_payload), _parse_services(services_payload)

    def _policy_for(
        self,
        settings: HomeAssistantSettings,
        entity_id: str,
    ) -> HomeAssistantEntityPolicy:
        existing = settings.entities.get(entity_id)
        if existing is not None:
            return existing
        return HomeAssistantEntityPolicy(entity_id=entity_id)

    def _entity_view(
        self,
        entity: HaEntityState,
        services: HaServiceCatalog,
        policy: HomeAssistantEntityPolicy,
    ) -> HomeAssistantEntityView:
        reason = control_blocked_reason(entity, services)
        return HomeAssistantEntityView(
            entity_id=entity.entity_id,
            name=str(entity.attributes.get("friendly_name") or entity.entity_id),
            domain=domain_of(entity.entity_id),
            state=entity.state,
            room=str(
                entity.attributes.get("area")
                or entity.attributes.get("area_id")
                or entity.attributes.get("room")
                or "未分配"
            ),
            included=policy.included,
            control_enabled=policy.control_enabled and reason is None,
            control_supported=reason is None,
            control_blocked_reason=reason,
            last_seen_at=policy.last_seen_at,
            last_control_at=policy.last_control_at,
            last_error=policy.last_error,
        )

    def _find_entity(
        self,
        states: Iterable[HaEntityState],
        entity_id: str,
    ) -> HaEntityState:
        for entity in states:
            if entity.entity_id == entity_id:
                return entity
        raise ResourceNotFoundException(f"Home Assistant entity '{entity_id}' not found")

    def _touch_policy(
        self,
        settings: HomeAssistantSettings,
        entity_id: str,
        *,
        last_control_at: int | None = None,
        last_error: str | None = None,
    ) -> None:
        current = self._policy_for(settings, entity_id)
        entities = {
            key: value.model_dump()
            for key, value in settings.entities.items()
        }
        entities[entity_id] = current.model_copy(
            update={
                "last_control_at": last_control_at or current.last_control_at,
                "last_error": last_error,
            }
        ).model_dump()
        try:
            update_shared_config(home_assistant={"entities": entities})
            reset_settings()
        except Exception:  # noqa: BLE001 - HA control must not fail because audit metadata fails.
            logger.warning("Failed to update Home Assistant entity policy metadata")


def _parse_states(payload: object) -> list[HaEntityState]:
    if not isinstance(payload, list):
        raise HomeAssistantError(
            HaErrorCode.INVALID_JSON,
            "Home Assistant states payload is invalid",
        )
    return [
        HaEntityState.model_validate(item)
        for item in payload
        if isinstance(item, dict) and isinstance(item.get("entity_id"), str)
    ]


def _parse_services(payload: object) -> HaServiceCatalog:
    if not isinstance(payload, list):
        raise HomeAssistantError(
            HaErrorCode.INVALID_JSON,
            "Home Assistant services payload is invalid",
        )
    catalog: HaServiceCatalog = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        domain = item.get("domain")
        services = item.get("services")
        if not isinstance(domain, str) or not isinstance(services, dict):
            continue
        catalog[domain] = {
            name
            for name in services
            if isinstance(name, str)
        }
    return catalog

