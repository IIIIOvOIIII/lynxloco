"""Settings-backed RTSP implementation of the common camera source boundary."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from miloco.config.settings import RtspSourceSettings
from miloco.perception.collect.camera_source import (
    AudioFrameCallback,
    CameraSourceState,
    VideoFrameCallback,
)
from miloco.perception.collect.rtsp_session import RtspSession
from miloco.perception.types import PerceptionDevice

logger = logging.getLogger(__name__)

_CONNECTION_FIELDS = (
    "uri",
    "username",
    "password",
    "transport",
    "audio_enabled",
)


@dataclass(frozen=True)
class RtspApplyResult:
    success: bool
    reconcile_dids: frozenset[str] = frozenset()


@dataclass
class _SessionEntry:
    session: RtspSession
    setting: RtspSourceSettings
    video_cb: VideoFrameCallback
    audio_cb: AudioFrameCallback


class RtspCameraSource:
    """Own the one live ``RtspSession`` allowed for each enabled RTSP source."""

    source_type: Literal["miot", "rtsp"] = "rtsp"

    def __init__(self, settings_loader: Callable[[], list[RtspSourceSettings]]) -> None:
        self._settings_loader = settings_loader
        self._sessions: dict[str, _SessionEntry] = {}
        self._pending_cleanup: dict[str, _SessionEntry] = {}
        self._lifecycle_lock = asyncio.Lock()

    def _load_settings(self) -> dict[str, RtspSourceSettings]:
        return {setting.id: setting for setting in self._settings_loader()}

    async def discover_devices(
        self,
        all_devices: dict | None = None,
        **_: object,
    ) -> dict[str, PerceptionDevice]:
        del all_devices
        return {
            setting.id: self._as_device(setting)
            for setting in self._settings_loader()
            if setting.enabled
        }

    async def connect_device(
        self,
        did: str,
        video_cb: VideoFrameCallback,
        audio_cb: AudioFrameCallback,
    ) -> None:
        async with self._lifecycle_lock:
            if did in self._sessions:
                return
            if did in self._pending_cleanup and not await self._retry_pending(did):
                return
            setting = self._load_settings().get(did)
            if setting is None or not setting.enabled:
                return

            session: RtspSession | None = None
            try:
                session = RtspSession(setting)
                await session.start(video_cb, audio_cb)
            except Exception as error:  # noqa: BLE001
                self._log_lifecycle_failure("start", did, error)
                if session is not None:
                    await self._cleanup_failed_start(
                        did, _SessionEntry(session, setting, video_cb, audio_cb)
                    )
                return
            self._sessions[did] = _SessionEntry(session, setting, video_cb, audio_cb)

    async def disconnect_device(self, did: str) -> None:
        async with self._lifecycle_lock:
            if did in self._sessions:
                await self._stop_active(did)
            elif did in self._pending_cleanup:
                await self._retry_pending(did)

    async def apply_settings(self) -> RtspApplyResult:
        """Apply settings to active sessions without rebuilding unchanged sources."""
        async with self._lifecycle_lock:
            settings = self._load_settings()
            success = True
            reconcile_dids = set(self._pending_cleanup)

            for did in list(self._pending_cleanup):
                if not await self._retry_pending(did):
                    success = False

            for did in list(self._sessions):
                entry = self._sessions[did]
                new_setting = settings.get(did)
                if new_setting is None or not new_setting.enabled:
                    reconcile_dids.add(did)
                    if not await self._stop_active(did):
                        success = False
                    continue
                if not self._connection_changed(entry.setting, new_setting):
                    entry.setting = new_setting
                    continue

                if not await self._stop_active(did):
                    success = False
                    reconcile_dids.add(did)
                    continue

                replacement_session: RtspSession | None = None
                try:
                    replacement_session = RtspSession(new_setting)
                    await replacement_session.start(entry.video_cb, entry.audio_cb)
                except Exception as error:  # noqa: BLE001
                    self._log_lifecycle_failure("start", did, error)
                    success = False
                    reconcile_dids.add(did)
                    if replacement_session is not None:
                        await self._cleanup_failed_start(
                            did,
                            _SessionEntry(
                                replacement_session,
                                new_setting,
                                entry.video_cb,
                                entry.audio_cb,
                            ),
                        )
                    continue
                self._sessions[did] = _SessionEntry(
                    replacement_session,
                    new_setting,
                    entry.video_cb,
                    entry.audio_cb,
                )

            return RtspApplyResult(success, frozenset(reconcile_dids))

    def get_session(self, did: str) -> RtspSession | None:
        entry = self._sessions.get(did)
        return entry.session if entry is not None else None

    def retain_pending_connection(self, did: str) -> bool:
        """Keep adapter buffers while a registered session connects/reconnects."""
        entry = self._sessions.get(did)
        return entry is not None and entry.session.is_active() is True

    def get_state(self, did: str) -> CameraSourceState:
        entry = self._sessions.get(did) or self._pending_cleanup.get(did)
        if entry is None:
            return CameraSourceState(connected=False)
        return entry.session.state()

    def get_cached_device(self, did: str) -> PerceptionDevice | None:
        setting = self._load_settings().get(did)
        if setting is None or not setting.enabled:
            return None
        return self._as_device(setting)

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            pending_at_start = list(self._pending_cleanup)
            for did in pending_at_start:
                await self._retry_pending(did)
            for did in list(self._sessions):
                await self._stop_active(did)

    async def _stop_active(self, did: str) -> bool:
        entry = self._sessions.get(did)
        if entry is None:
            return True
        if await self._stop_entry(did, entry):
            self._sessions.pop(did, None)
            return True
        self._pending_cleanup[did] = entry
        self._sessions.pop(did, None)
        return False

    async def _retry_pending(self, did: str) -> bool:
        entry = self._pending_cleanup.get(did)
        if entry is None:
            return True
        if await self._stop_entry(did, entry):
            self._pending_cleanup.pop(did, None)
            return True
        return False

    async def _cleanup_failed_start(self, did: str, entry: _SessionEntry) -> bool:
        if await self._stop_entry(did, entry):
            return True
        self._pending_cleanup[did] = entry
        return False

    async def _stop_entry(self, did: str, entry: _SessionEntry) -> bool:
        try:
            await entry.session.stop()
        except Exception as error:  # noqa: BLE001
            self._log_lifecycle_failure("stop", did, error)
            return False
        return True

    @staticmethod
    def _log_lifecycle_failure(action: str, did: str, error: Exception) -> None:
        logger.error(
            "RTSP session %s failed for %s (%s)",
            action,
            did,
            type(error).__name__,
        )

    @staticmethod
    def _connection_changed(old: RtspSourceSettings, new: RtspSourceSettings) -> bool:
        return any(
            getattr(old, field) != getattr(new, field) for field in _CONNECTION_FIELDS
        )

    @staticmethod
    def _as_device(setting: RtspSourceSettings) -> PerceptionDevice:
        return PerceptionDevice(
            did=setting.id,
            name=setting.name,
            device_type="camera",
            room_id=setting.room_name,
            room_name=setting.room_name,
            online=True,
        )
