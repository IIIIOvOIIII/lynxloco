"""RTSP configuration lifecycle and generic camera aggregation."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from miloco.camera.schema import CameraSummary, RtspSourceUpsert
from miloco.camera.stream import LiveStreamSource
from miloco.config import get_settings
from miloco.config.settings import MilocoSettings, RtspSourceSettings
from miloco.perception.collect.camera_source import CameraSourceState
from miloco.perception.collect.rtsp_probe import RtspProbeResult, probe_rtsp_source
from miloco.utils.agent_config import mutate_rtsp_sources

logger = logging.getLogger(__name__)


class _MiotCameraLister(Protocol):
    async def list_cameras_with_state(self) -> list[dict]: ...


class _CameraSourceSynchronizer(Protocol):
    async def sync_camera_sources(self) -> bool: ...

    async def retry_camera_source(self, camera_id: str) -> bool: ...


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
SourcesMutation = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]
SourcesMutator = Callable[[SourcesMutation], dict[str, Any]]
Probe = Callable[[RtspSourceSettings], Awaitable[RtspProbeResult]]


class CameraService:
    """Serializes read-modify-write operations and applies them at runtime."""

    def __init__(
        self,
        miot_service: _MiotCameraLister,
        perception_service: _CameraSourceSynchronizer,
        *,
        settings_loader: SettingsLoader = get_settings,
        sources_mutator: SourcesMutator = mutate_rtsp_sources,
        probe: Probe = probe_rtsp_source,
    ) -> None:
        self._miot_service = miot_service
        self._perception_service = perception_service
        self._settings_loader = settings_loader
        self._sources_mutator = sources_mutator
        self._probe = probe
        self._write_lock = asyncio.Lock()

    async def list_cameras(self) -> list[CameraSummary]:
        miot_rows = await self._miot_service.list_cameras_with_state()
        summaries = [self._miot_summary(row) for row in miot_rows]
        sources = await asyncio.to_thread(self._load_sources_safely)
        summaries.extend(self._rtsp_summary(source) for source in sources)
        return summaries

    async def resolve_live_stream(self, camera_id: str) -> LiveStreamSource:
        """Resolve an existing runtime backend without creating a connection."""
        if camera_id.startswith("rtsp:"):
            sources = await asyncio.to_thread(self._load_sources_safely)
            _index, source = self._locate(sources, camera_id)
            if not source.enabled:
                raise CameraConflictError("camera_disabled", "Camera is disabled")
            registry = getattr(self._perception_service, "_rtsp_camera_source", None)
            if registry is None:
                raise CameraConflictError(
                    "camera_unavailable", "Camera stream is unavailable"
                )
            session = registry.get_session(camera_id)
            if session is None:
                raise CameraConflictError(
                    "camera_unavailable", "Camera stream is unavailable"
                )
            is_active = getattr(session, "is_active", None)
            is_terminal = getattr(session, "is_terminal", None)
            try:
                inactive = callable(is_active) and is_active() is not True
                terminal = callable(is_terminal) and is_terminal() is True
            except Exception as error:  # noqa: BLE001
                logger.warning(
                    "Camera stream lifecycle read failed (%s)", type(error).__name__
                )
                inactive = True
                terminal = False
            if inactive or terminal:
                raise CameraConflictError(
                    "camera_unavailable", "Camera stream is unavailable"
                )
            state = registry.get_state(camera_id)
            return LiveStreamSource(
                camera_id=camera_id,
                source_type="rtsp",
                backend=session,
                channel=0,
                input_codec=state.video_codec,
            )

        rows = await self._miot_service.list_cameras_with_state()
        for row in rows:
            did = str(row.get("did") or "")
            channel = int(row.get("channel") or 0)
            channel_count = int(row.get("channel_count") or 1)
            resolved_id = f"{did}:ch{channel}" if channel_count > 1 else did
            if resolved_id != camera_id:
                continue
            if not bool(row.get("in_use", False)):
                raise CameraConflictError("camera_disabled", "Camera is disabled")
            return LiveStreamSource(
                camera_id=did,
                source_type="miot",
                backend=self._miot_service,
                channel=channel,
                input_codec=None,
            )
        raise CameraNotFoundError()

    async def test_rtsp(self, body: RtspSourceUpsert) -> RtspProbeResult:
        source = self._source_from_upsert(
            f"rtsp:{uuid.uuid4()}", body, enabled=False, password=body.password
        )
        return await self._probe(source)

    async def create_rtsp(self, body: RtspSourceUpsert) -> CameraSummary:
        async with self._write_lock:
            source = self._source_from_upsert(
                f"rtsp:{uuid.uuid4()}", body, enabled=False, password=body.password
            )
            sources = await self._await_shielded_transaction(
                self._mutate_and_sync(lambda current: [*current, source.model_dump()])
            )
            return self._rtsp_summary(self._locate(sources, source.id)[1])

    async def edit_rtsp(self, camera_id: str, body: RtspSourceUpsert) -> CameraSummary:
        async with self._write_lock:

            def edit(raw_sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
                sources = self._validate_sources(raw_sources)
                index, current = self._locate(sources, camera_id)
                password = body.password if body.password else current.password
                sources[index] = self._source_from_upsert(
                    current.id,
                    body,
                    enabled=current.enabled,
                    password=password,
                )
                return [source.model_dump() for source in sources]

            sources = await self._await_shielded_transaction(
                self._mutate_and_sync(edit)
            )
            return self._rtsp_summary(self._locate(sources, camera_id)[1])

    async def enable(self, camera_id: str) -> CameraSummary:
        async with self._write_lock:
            sources = await asyncio.to_thread(self._load_sources_safely)
            _index, current = self._locate(sources, camera_id)
            await self._probe(current)
            if current.enabled:
                retried = await self._await_shielded_transaction(
                    self._retry_safely(camera_id)
                )
                if not retried:
                    raise CameraConflictError(
                        "hot_apply_failed", "Camera update could not be applied"
                    )
                return self._rtsp_summary(current)
            enabled = await self._await_shielded_transaction(
                self._enable_transaction(camera_id)
            )
            return self._rtsp_summary(enabled)

    async def disable(self, camera_id: str) -> CameraSummary:
        async with self._write_lock:
            sources = await self._await_shielded_transaction(
                self._mutate_and_sync(self._set_enabled(camera_id, False))
            )
            return self._rtsp_summary(self._locate(sources, camera_id)[1])

    async def delete(self, camera_id: str) -> None:
        async with self._write_lock:

            def delete(raw_sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
                sources = self._validate_sources(raw_sources)
                index, _current = self._locate(sources, camera_id)
                del sources[index]
                return [source.model_dump() for source in sources]

            await self._await_shielded_transaction(self._mutate_and_sync(delete))

    def _sources(self) -> list[RtspSourceSettings]:
        return list(self._settings_loader().camera.rtsp_sources)

    def _load_sources_safely(self) -> list[RtspSourceSettings]:
        try:
            return self._sources()
        except CameraServiceError:
            raise
        except Exception as error:  # noqa: BLE001
            self._log_persistence_failure(error)
            raise CameraConflictError(
                "persistence_failed", "Camera configuration could not be loaded"
            ) from None

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

    @staticmethod
    def _validate_sources(
        raw_sources: list[dict[str, Any]],
    ) -> list[RtspSourceSettings]:
        return [RtspSourceSettings.model_validate(source) for source in raw_sources]

    async def _mutate_sources(
        self, mutation: SourcesMutation
    ) -> list[RtspSourceSettings]:
        def mutate_and_reload() -> list[RtspSourceSettings]:
            self._sources_mutator(mutation)
            return self._sources()

        try:
            return await asyncio.to_thread(mutate_and_reload)
        except CameraServiceError:
            raise
        except Exception as error:  # noqa: BLE001
            self._log_persistence_failure(error)
            raise CameraConflictError(
                "persistence_failed", "Camera configuration could not be saved"
            ) from None

    async def _mutate_and_sync(
        self, mutation: SourcesMutation
    ) -> list[RtspSourceSettings]:
        sources = await self._mutate_sources(mutation)
        if not await self._sync_safely():
            raise CameraConflictError(
                "hot_apply_failed", "Camera update could not be applied"
            )
        return sources

    async def _enable_transaction(self, camera_id: str) -> RtspSourceSettings:
        enabled_sources = await self._mutate_sources(self._set_enabled(camera_id, True))
        enabled = self._locate(enabled_sources, camera_id)[1]
        if await self._sync_safely():
            return enabled

        try:
            await self._mutate_sources(self._set_enabled(camera_id, False))
        except CameraConflictError as error:
            raise CameraConflictError(
                "compensation_failed", "Camera update could not be rolled back"
            ) from error
        if not await self._sync_safely():
            raise CameraConflictError(
                "cleanup_failed", "Camera rollback cleanup could not be applied"
            )
        raise CameraConflictError(
            "hot_apply_failed", "Camera update could not be applied"
        )

    def _set_enabled(self, camera_id: str, enabled: bool) -> SourcesMutation:
        def mutate(raw_sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
            sources = self._validate_sources(raw_sources)
            index, current = self._locate(sources, camera_id)
            sources[index] = current.model_copy(update={"enabled": enabled})
            return [source.model_dump() for source in sources]

        return mutate

    @staticmethod
    async def _await_shielded_transaction(transaction):
        task = asyncio.create_task(transaction)
        cancellation: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                cancellation = error
            except Exception:
                break
        try:
            result = task.result()
        except Exception:
            if cancellation is not None:
                raise cancellation
            raise
        if cancellation is not None:
            raise cancellation
        return result

    @staticmethod
    def _log_persistence_failure(error: BaseException) -> None:
        logger.error(
            "Camera configuration persistence failed (%s)", type(error).__name__
        )

    async def _sync_safely(self) -> bool:
        try:
            return await self._perception_service.sync_camera_sources()
        except Exception as error:  # noqa: BLE001
            logger.error("Camera hot apply failed (%s)", type(error).__name__)
            return False

    async def _retry_safely(self, camera_id: str) -> bool:
        try:
            return await self._perception_service.retry_camera_source(camera_id)
        except Exception as error:  # noqa: BLE001
            logger.error("Camera retry failed (%s)", type(error).__name__)
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
            last_frame_unix_ms=state.last_frame_unix_ms,
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
