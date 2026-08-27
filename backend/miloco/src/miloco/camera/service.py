"""RTSP configuration lifecycle and generic camera aggregation."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from miloco.camera.schema import CameraSummary, RtspSourceUpsert
from miloco.config import get_settings
from miloco.config.settings import MilocoSettings, RtspSourceSettings
from miloco.perception.collect.camera_source import CameraSourceState
from miloco.perception.collect.rtsp_probe import RtspProbeResult, probe_rtsp_source
from miloco.utils.agent_config import update_shared_config

logger = logging.getLogger(__name__)


class _MiotCameraLister(Protocol):
    async def list_cameras_with_state(self) -> list[dict]: ...


class _CameraSourceSynchronizer(Protocol):
    async def sync_camera_sources(self) -> bool: ...


class CameraServiceError(RuntimeError):
    """Stable, credential-free management failure."""

    def __init__(self, code: str, safe_message: str) -> None:
        self.code = code
        self.safe_message = safe_message
        super().__init__(safe_message)


class CameraNotFoundError(CameraServiceError):
    def __init__(self) -> None:
        super().__init__("camera_not_found", "Camera was not found")


class CameraConflictError(CameraServiceError):
    pass


SettingsLoader = Callable[[], MilocoSettings | Any]
ConfigWriter = Callable[..., dict[str, Any]]
Probe = Callable[[RtspSourceSettings], Awaitable[RtspProbeResult]]


class CameraService:
    """Serializes read-modify-write operations and applies them at runtime."""

    def __init__(
        self,
        miot_service: _MiotCameraLister,
        perception_service: _CameraSourceSynchronizer,
        *,
        settings_loader: SettingsLoader = get_settings,
        config_writer: ConfigWriter = update_shared_config,
        probe: Probe = probe_rtsp_source,
    ) -> None:
        self._miot_service = miot_service
        self._perception_service = perception_service
        self._settings_loader = settings_loader
        self._config_writer = config_writer
        self._probe = probe
        self._write_lock = asyncio.Lock()

    async def list_cameras(self) -> list[CameraSummary]:
        miot_rows = await self._miot_service.list_cameras_with_state()
        summaries = [self._miot_summary(row) for row in miot_rows]
        summaries.extend(self._rtsp_summary(source) for source in self._sources())
        return summaries

    async def test_rtsp(self, body: RtspSourceUpsert) -> RtspProbeResult:
        source = self._source_from_upsert(
            f"rtsp:{uuid.uuid4()}", body, enabled=False, password=body.password
        )
        return await self._probe(source)

    async def create_rtsp(self, body: RtspSourceUpsert) -> CameraSummary:
        async with self._write_lock:
            sources = self._sources()
            source = self._source_from_upsert(
                f"rtsp:{uuid.uuid4()}", body, enabled=False, password=body.password
            )
            sources.append(source)
            await self._persist_and_sync(sources)
            return self._rtsp_summary(self._find(source.id))

    async def edit_rtsp(self, camera_id: str, body: RtspSourceUpsert) -> CameraSummary:
        async with self._write_lock:
            sources = self._sources()
            index, current = self._locate(sources, camera_id)
            password = body.password if body.password else current.password
            sources[index] = self._source_from_upsert(
                current.id,
                body,
                enabled=current.enabled,
                password=password,
            )
            await self._persist_and_sync(sources)
            return self._rtsp_summary(self._find(camera_id))

    async def enable(self, camera_id: str) -> CameraSummary:
        async with self._write_lock:
            sources = self._sources()
            index, current = self._locate(sources, camera_id)
            await self._probe(current)
            sources[index] = current.model_copy(update={"enabled": True})
            self._persist(sources)
            if not await self._sync_safely():
                compensated = self._sources()
                compensated_index, persisted = self._locate(compensated, camera_id)
                compensated[compensated_index] = persisted.model_copy(
                    update={"enabled": False}
                )
                try:
                    self._persist(compensated)
                except CameraConflictError as error:
                    raise CameraConflictError(
                        "compensation_failed",
                        "Camera update could not be rolled back",
                    ) from error
                await self._sync_safely()
                raise CameraConflictError(
                    "hot_apply_failed", "Camera update could not be applied"
                )
            return self._rtsp_summary(self._find(camera_id))

    async def disable(self, camera_id: str) -> CameraSummary:
        async with self._write_lock:
            sources = self._sources()
            index, current = self._locate(sources, camera_id)
            sources[index] = current.model_copy(update={"enabled": False})
            await self._persist_and_sync(sources)
            return self._rtsp_summary(self._find(camera_id))

    async def delete(self, camera_id: str) -> None:
        async with self._write_lock:
            sources = self._sources()
            index, _current = self._locate(sources, camera_id)
            del sources[index]
            await self._persist_and_sync(sources)

    def _sources(self) -> list[RtspSourceSettings]:
        return list(self._settings_loader().camera.rtsp_sources)

    def _find(self, camera_id: str) -> RtspSourceSettings:
        _index, source = self._locate(self._sources(), camera_id)
        return source

    @staticmethod
    def _locate(
        sources: list[RtspSourceSettings], camera_id: str
    ) -> tuple[int, RtspSourceSettings]:
        for index, source in enumerate(sources):
            if source.id == camera_id:
                return index, source
        raise CameraNotFoundError()

    @staticmethod
    def _source_from_upsert(
        source_id: str,
        body: RtspSourceUpsert,
        *,
        enabled: bool,
        password: str,
    ) -> RtspSourceSettings:
        return RtspSourceSettings(
            id=source_id,
            name=body.name,
            room_name=body.room_name,
            uri=body.uri,
            username=body.username,
            password=password,
            transport=body.transport,
            audio_enabled=body.audio_enabled,
            enabled=enabled,
        )

    def _persist(self, sources: list[RtspSourceSettings]) -> None:
        payload = [source.model_dump() for source in sources]
        try:
            self._config_writer(camera={"rtsp_sources": payload})
        except Exception as error:  # noqa: BLE001
            logger.error(
                "Camera configuration persistence failed (%s)",
                type(error).__name__,
            )
            raise CameraConflictError(
                "persistence_failed", "Camera configuration could not be saved"
            ) from None
        # update_shared_config resets the settings singleton; read it immediately so
        # validation or an unexpected writer contract fails before runtime apply.
        self._sources()

    async def _persist_and_sync(self, sources: list[RtspSourceSettings]) -> None:
        self._persist(sources)
        if not await self._sync_safely():
            raise CameraConflictError(
                "hot_apply_failed", "Camera update could not be applied"
            )

    async def _sync_safely(self) -> bool:
        try:
            return await self._perception_service.sync_camera_sources()
        except Exception as error:  # noqa: BLE001
            logger.error("Camera hot apply failed (%s)", type(error).__name__)
            return False

    def _rtsp_state(self, camera_id: str) -> CameraSourceState:
        source = getattr(self._perception_service, "_rtsp_camera_source", None)
        if source is None:
            return CameraSourceState(connected=False)
        try:
            return source.get_state(camera_id)
        except Exception as error:  # noqa: BLE001
            logger.warning("Camera state read failed (%s)", type(error).__name__)
            return CameraSourceState(
                connected=False,
                error_code="state_unavailable",
                error_message="Camera state is unavailable",
            )

    def _rtsp_summary(self, source: RtspSourceSettings) -> CameraSummary:
        state = self._rtsp_state(source.id)
        return CameraSummary(
            id=source.id,
            source_type="rtsp",
            name=source.name,
            room_name=source.room_name,
            enabled=source.enabled,
            connected=state.connected,
            video_codec=state.video_codec,
            audio_codec=state.audio_codec,
            has_password=bool(source.password),
            error_code=state.error_code,
            error_message=state.error_message,
        )

    @staticmethod
    def _miot_summary(row: dict) -> CameraSummary:
        did = str(row.get("did") or "")
        channel_count = int(row.get("channel_count") or 1)
        channel = int(row.get("channel") or 0)
        camera_id = f"{did}:ch{channel}" if channel_count > 1 else did
        return CameraSummary(
            id=camera_id,
            source_type="miot",
            name=str(row.get("name") or "Unknown Camera"),
            room_name=str(row.get("room_name") or ""),
            enabled=bool(row.get("in_use", False)),
            connected=bool(row.get("connected", False)),
            video_codec=None,
            audio_codec=None,
        )
