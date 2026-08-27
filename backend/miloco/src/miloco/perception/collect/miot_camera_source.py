"""MIoT discovery and decoded-frame subscription transport."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, cast

from miot.types import MIoTCameraInfo

from miloco.miot.client import MiotProxy
from miloco.miot.schema import CameraInfo
from miloco.perception.collect.camera_source import (
    AudioFrameCallback,
    CameraSourceState,
    VideoFrameCallback,
)
from miloco.perception.types import PerceptionDevice

logger = logging.getLogger(__name__)

_ONDEMAND_REFRESH_MIN_INTERVAL_MS = 10_000
DEFAULT_VIDEO_CHANNEL = 0
DEFAULT_AUDIO_CHANNEL = 0
_CHANNEL_SEP = ":ch"


def split_channel_did(did: str) -> tuple[str, int]:
    """Split a synthetic channel DID into its physical DID and channel."""
    if _CHANNEL_SEP in did:
        physical, channel = did.rsplit(_CHANNEL_SEP, 1)
        return physical, int(channel)
    return did, DEFAULT_VIDEO_CHANNEL


@dataclass
class _MiotSubscription:
    video_registration_id: int = -1
    audio_registration_id: int = -1


class MiotCameraSource:
    """MIoT camera transport implementing the common camera-source boundary."""

    source_type: Literal["miot", "rtsp"] = "miot"

    def __init__(self, miot_proxy: MiotProxy) -> None:
        self._miot_proxy = miot_proxy
        self._subscriptions: dict[str, _MiotSubscription] = {}
        self._last_ondemand_refresh_ms = 0

    async def discover_devices(
        self,
        all_devices: dict | None = None,
        online_only: bool = True,
        require_lan: bool = True,
        cap: bool = True,
        **_: object,
    ) -> dict[str, PerceptionDevice]:
        if not self._miot_proxy.is_authenticated:
            return {}
        return self._filter_cameras_from_all(
            all_devices if all_devices else await self._miot_proxy.get_cameras(),
            online_only=online_only,
            require_lan=require_lan,
            cap=cap,
        )

    def _filter_cameras_from_all(
        self,
        all_devices: dict,
        *,
        online_only: bool = True,
        require_lan: bool = True,
        cap: bool = True,
    ) -> dict[str, PerceptionDevice]:
        from miloco.miot.filter import select_active_camera_dids

        cameras = {
            did: info
            for did, info in all_devices.items()
            if isinstance(info, MIoTCameraInfo)
        }
        active = select_active_camera_dids(
            self._miot_proxy._kv_repo,
            cameras,
            online_only=online_only,
            require_lan=require_lan,
            cap=cap,
            awake_map=getattr(self._miot_proxy, "_camera_awake_cache", None),
        )
        result: dict[str, PerceptionDevice] = {}
        for synthetic_did in active:
            physical_did, _ = split_channel_did(synthetic_did)
            camera = CameraInfo.model_validate(cameras[physical_did].model_dump())
            result[synthetic_did] = PerceptionDevice(
                did=synthetic_did,
                name=camera.name,
                device_type="camera",
                room_id=camera.room_name,
                room_name=camera.room_name,
                online=cast(bool, camera.online and camera.lan_online),
            )
        return result

    async def refresh_if_needed(
        self, *, expected_count: int, connected_count: int, now_ms: int
    ) -> None:
        """Rebuild missing SDK camera managers with the existing throttle."""
        if not self._miot_proxy.is_authenticated:
            return
        if expected_count <= connected_count:
            return
        if (
            now_ms - self._last_ondemand_refresh_ms
            < _ONDEMAND_REFRESH_MIN_INTERVAL_MS
        ):
            return
        self._last_ondemand_refresh_ms = now_ms
        await self._miot_proxy.refresh_cameras()

    async def connect_device(
        self,
        did: str,
        video_cb: VideoFrameCallback,
        audio_cb: AudioFrameCallback,
    ) -> None:
        if did in self._subscriptions:
            return

        physical_did, channel = split_channel_did(did)
        subscription = _MiotSubscription()
        try:
            subscription.video_registration_id = (
                await self._miot_proxy.start_camera_decode_video_stream(
                    physical_did, channel, cast(Any, video_cb)
                )
            )
        except Exception as error:  # noqa: BLE001
            logger.error("Failed to subscribe decoded video for %s: %s", did, error)

        try:
            subscription.audio_registration_id = (
                await self._miot_proxy.start_camera_decode_audio_stream(
                    physical_did, channel, cast(Any, audio_cb)
                )
            )
        except Exception as error:  # noqa: BLE001
            logger.error("Failed to subscribe decoded audio for %s: %s", did, error)

        if (
            subscription.video_registration_id < 0
            and subscription.audio_registration_id < 0
        ):
            logger.warning(
                "Camera %s stream subscribe failed (manager missing?), "
                "will retry on next sync",
                did,
            )
            return
        self._subscriptions[did] = subscription

    async def disconnect_device(self, did: str) -> None:
        subscription = self._subscriptions.pop(did, None)
        if subscription is None:
            return

        physical_did, channel = split_channel_did(did)
        if subscription.video_registration_id >= 0:
            try:
                await self._miot_proxy.stop_camera_decode_video_stream(
                    physical_did, channel, subscription.video_registration_id
                )
            except Exception as error:  # noqa: BLE001
                logger.error(
                    "Failed to unsubscribe decoded video for %s: %s", did, error
                )
        if subscription.audio_registration_id >= 0:
            try:
                await self._miot_proxy.stop_camera_decode_audio_stream(
                    physical_did, channel, subscription.audio_registration_id
                )
            except Exception as error:  # noqa: BLE001
                logger.error(
                    "Failed to unsubscribe decoded audio for %s: %s", did, error
                )

    def get_state(self, did: str) -> CameraSourceState:
        return CameraSourceState(connected=did in self._subscriptions)

    def get_cached_device(self, did: str) -> PerceptionDevice | None:
        """Return current MIoT metadata while retaining a synthetic DID."""
        physical_did, _ = split_channel_did(did)
        get_cached_camera = getattr(self._miot_proxy, "get_cached_camera", None)
        camera_info = (
            get_cached_camera(physical_did) if get_cached_camera is not None else None
        )
        if camera_info is None:
            return None
        camera = CameraInfo.model_validate(camera_info.model_dump())
        return PerceptionDevice(
            did=did,
            name=camera.name,
            device_type="camera",
            room_id=camera.room_name,
            room_name=camera.room_name,
            online=cast(bool, camera.online and camera.lan_online),
        )

    async def shutdown(self) -> None:
        for did in list(self._subscriptions):
            await self.disconnect_device(did)
