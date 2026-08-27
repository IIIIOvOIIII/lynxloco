"""Settings-backed RTSP implementation of the common camera source boundary."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
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


class RtspCameraSource:
    """Own the one live ``RtspSession`` allowed for each enabled RTSP source."""

    source_type: Literal["miot", "rtsp"] = "rtsp"

    def __init__(self, settings_loader: Callable[[], list[RtspSourceSettings]]) -> None:
        self._settings_loader = settings_loader
        self._sessions: dict[str, RtspSession] = {}
        self._session_settings: dict[str, RtspSourceSettings] = {}
        self._callbacks: dict[str, tuple[VideoFrameCallback, AudioFrameCallback]] = {}
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
                    await self._stop_safely(did, session)
                return
            self._sessions[did] = session
            self._session_settings[did] = setting
            self._callbacks[did] = (video_cb, audio_cb)

    async def disconnect_device(self, did: str) -> None:
        async with self._lifecycle_lock:
            session = self._remove_session(did)
            if session is not None:
                await self._stop_safely(did, session)

    async def apply_settings(self) -> None:
        """Apply settings to active sessions without rebuilding unchanged sources."""
        async with self._lifecycle_lock:
            settings = self._load_settings()
            for did in list(self._sessions):
                old_setting = self._session_settings[did]
                new_setting = settings.get(did)
                if new_setting is None or not new_setting.enabled:
                    session = self._remove_session(did)
                    if session is not None:
                        await self._stop_safely(did, session)
                    continue
                if not self._connection_changed(old_setting, new_setting):
                    self._session_settings[did] = new_setting
                    continue

                callbacks = self._callbacks[did]
                old_session = self._remove_session(did)
                if old_session is None or not await self._stop_safely(did, old_session):
                    continue

                replacement_session = RtspSession(new_setting)
                try:
                    await replacement_session.start(*callbacks)
                except Exception as error:  # noqa: BLE001
                    self._log_lifecycle_failure("start", did, error)
                    continue
                self._sessions[did] = replacement_session
                self._session_settings[did] = new_setting
                self._callbacks[did] = callbacks

    def get_session(self, did: str) -> RtspSession | None:
        return self._sessions.get(did)

    def get_state(self, did: str) -> CameraSourceState:
        session = self._sessions.get(did)
        if session is None:
            return CameraSourceState(connected=False)
        # The adapter's ``connected`` flag means that the transport lifecycle is
        # registered. Network connection detail remains available on the shared
        # session itself and may still be establishing or reconnecting.
        return replace(session.state(), connected=True)

    def get_cached_device(self, did: str) -> PerceptionDevice | None:
        setting = self._load_settings().get(did)
        if setting is None or not setting.enabled:
            return None
        return self._as_device(setting)

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            sessions = list(self._sessions.items())
            self._sessions.clear()
            self._session_settings.clear()
            self._callbacks.clear()
            for did, session in sessions:
                await self._stop_safely(did, session)

    def _remove_session(self, did: str) -> RtspSession | None:
        self._session_settings.pop(did, None)
        self._callbacks.pop(did, None)
        return self._sessions.pop(did, None)

    async def _stop_safely(self, did: str, session: RtspSession) -> bool:
        try:
            await session.stop()
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
