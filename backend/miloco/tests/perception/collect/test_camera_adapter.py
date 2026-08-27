from __future__ import annotations

import json

import numpy as np
import pytest
from miloco.database.kv_repo import ScopeConfigKeys
from miloco.perception.collect.camera_adapter import CameraDeviceAdapter
from miot.types import MIoTCameraInfo


class _FakeKV:
    def __init__(self) -> None:
        self._values = {
            ScopeConfigKeys.HOME_WHITE_LIST_KEY: json.dumps(["home-1"]),
        }

    def get(self, key: str, default: str | None = None) -> str | None:
        return self._values.get(key, default)

    def set(self, key: str, value: str) -> bool:
        self._values[key] = value
        return True


def _camera(did: str, *, channel_count: int) -> MIoTCameraInfo:
    return MIoTCameraInfo.model_construct(
        did=did,
        home_id="home-1",
        name=f"camera-{did}",
        online=True,
        lan_online=True,
        room_name="living-room",
        channel_count=channel_count,
    )


class _RecordingMiotProxy:
    is_authenticated = True

    def __init__(self, cameras: dict[str, MIoTCameraInfo]) -> None:
        self._cameras = cameras
        self._kv_repo = _FakeKV()
        self._camera_awake_cache: dict = {}
        self.video_subscriptions: list[tuple[str, int, object]] = []
        self.audio_subscriptions: list[tuple[str, int, object]] = []
        self.video_unsubscriptions: list[tuple[str, int, int]] = []
        self.audio_unsubscriptions: list[tuple[str, int, int]] = []

    async def get_cameras(self) -> dict[str, MIoTCameraInfo]:
        return self._cameras

    def get_cached_camera(self, did: str) -> MIoTCameraInfo | None:
        return self._cameras.get(did)

    async def start_camera_decode_video_stream(
        self, did: str, channel: int, callback: object
    ) -> int:
        self.video_subscriptions.append((did, channel, callback))
        return 101

    async def start_camera_decode_audio_stream(
        self, did: str, channel: int, callback: object
    ) -> int:
        self.audio_subscriptions.append((did, channel, callback))
        return 202

    async def stop_camera_decode_video_stream(
        self, did: str, channel: int, registration_id: int
    ) -> None:
        self.video_unsubscriptions.append((did, channel, registration_id))

    async def stop_camera_decode_audio_stream(
        self, did: str, channel: int, registration_id: int
    ) -> None:
        self.audio_unsubscriptions.append((did, channel, registration_id))


@pytest.mark.asyncio
async def test_miot_discovery_preserves_single_and_multichannel_dids() -> None:
    proxy = _RecordingMiotProxy(
        {
            "dual": _camera("dual", channel_count=2),
            "single": _camera("single", channel_count=1),
        }
    )
    adapter = CameraDeviceAdapter(miot_proxy=proxy)  # type: ignore[arg-type]

    devices = await adapter.discover_devices()

    assert list(devices) == ["dual:ch0", "dual:ch1", "single"]
    assert devices["dual:ch0"].did == "dual:ch0"
    assert devices["dual:ch1"].did == "dual:ch1"
    assert devices["single"].did == "single"


@pytest.mark.asyncio
async def test_miot_callbacks_collection_unregistration_and_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy = _RecordingMiotProxy({"dual": _camera("dual", channel_count=2)})
    adapter = CameraDeviceAdapter(miot_proxy=proxy)  # type: ignore[arg-type]
    device = (await adapter.discover_devices())["dual:ch1"]
    monkeypatch.setattr(
        "miloco.perception.collect.camera_adapter._monotonic_ms", lambda: 50_000
    )
    monkeypatch.setattr(
        "miloco.perception.collect.camera_adapter._unix_ms",
        lambda: 1_700_000_050_000,
    )

    await adapter.connect_device("dual:ch1", source=device)

    assert [(did, channel) for did, channel, _ in proxy.video_subscriptions] == [
        ("dual", 1)
    ]
    assert [(did, channel) for did, channel, _ in proxy.audio_subscriptions] == [
        ("dual", 1)
    ]

    video_callback = proxy.video_subscriptions[0][2]
    audio_callback = proxy.audio_subscriptions[0][2]
    video_frame = np.zeros((2, 3, 3), dtype=np.uint8)
    audio_frame = np.array([11, -12], dtype=np.int16)
    await video_callback(  # type: ignore[operator]
        "dual", video_frame, 9_000, 1, 1_700_000_049_970, 1_700_000_049_990
    )
    await audio_callback(  # type: ignore[operator]
        "dual", audio_frame, 9_000, 1, 1_700_000_049_975, 1_700_000_049_990
    )

    collected = adapter.collect("dual:ch1", drain=False)
    assert collected is not None
    assert collected.meta.did == "dual:ch1"
    assert collected.meta.name == "camera-dual"
    assert collected.video[0].frame is video_frame
    assert collected.video[0].stream_ts == 9_000
    assert collected.video[0].decode_latency_ms == 20.0
    assert collected.audio[0].frame is audio_frame
    assert collected.audio[0].stream_ts == 9_000
    assert collected.audio[0].decode_latency_ms == 15.0

    await adapter.shutdown()

    assert proxy.video_unsubscriptions == [("dual", 1, 101)]
    assert proxy.audio_unsubscriptions == [("dual", 1, 202)]
    assert adapter.get_connected_devices() == {}
    assert adapter.collect("dual:ch1", drain=False) is None
