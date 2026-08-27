from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from miloco.config.settings import RtspSourceSettings
from miloco.database.kv_repo import ScopeConfigKeys
from miloco.perception.collect.camera_adapter import CameraDeviceAdapter
from miloco.perception.collect.camera_source import CameraSourceState
from miloco.perception.collect.miot_camera_source import MiotCameraSource
from miloco.perception.collect.rtsp_camera_source import RtspCameraSource
from miloco.perception.service import PerceptionService
from miloco.perception.types import PerceptionDevice
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


class _FrameProducingMiotSource:
    source_type: Literal["miot", "rtsp"] = "miot"

    def __init__(self) -> None:
        self.connected = False

    async def discover_devices(self, *args, **kwargs):
        return {
            "miot-stable-did": PerceptionDevice(
                did="miot-stable-did",
                name="miot camera",
                device_type="camera",
                room_name="living-room",
            )
        }

    async def connect_device(self, did, video_cb, audio_cb) -> None:
        self.connected = True
        await video_cb(
            did,
            np.zeros((2, 2, 3), dtype=np.uint8),
            100,
            0,
            1_000,
            1_001,
        )

    async def disconnect_device(self, did) -> None:
        self.connected = False

    def get_state(self, did) -> CameraSourceState:
        return CameraSourceState(connected=self.connected)

    async def shutdown(self) -> None:
        self.connected = False


class _AdapterRtspSession:
    def __init__(self, source: RtspSourceSettings) -> None:
        self.source = source
        self.connected = False

    async def start(self, video_cb, audio_cb) -> None:
        if "broken" in self.source.uri:
            raise RuntimeError("operator-secret private-path")
        self.connected = True
        await video_cb(
            self.source.id,
            np.ones((2, 2, 3), dtype=np.uint8),
            200,
            0,
            2_000,
            2_001,
        )

    async def stop(self) -> None:
        self.connected = False

    def state(self) -> CameraSourceState:
        return CameraSourceState(connected=self.connected)


@pytest.mark.asyncio
async def test_failing_rtsp_does_not_block_miot_or_another_rtsp_device_data(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        "miloco.perception.collect.rtsp_camera_source.RtspSession",
        _AdapterRtspSession,
    )
    bad_id = "rtsp:00000000-0000-0000-0000-000000000001"
    good_id = "rtsp:00000000-0000-0000-0000-000000000002"
    settings = [
        RtspSourceSettings(
            id=bad_id,
            name="broken",
            uri="rtsp://broken.example/stream",
            enabled=True,
        ),
        RtspSourceSettings(
            id=good_id,
            name="healthy",
            room_name="office",
            uri="rtsp://healthy.example/stream",
            enabled=True,
        ),
    ]
    miot = _FrameProducingMiotSource()
    rtsp = RtspCameraSource(lambda: settings)
    adapter = CameraDeviceAdapter(sources=[miot, rtsp])  # type: ignore[list-item]

    with caplog.at_level(logging.ERROR):
        await adapter.sync_devices()

    assert set(adapter.get_connected_devices()) == {"miot-stable-did", good_id}
    miot_data = adapter.collect("miot-stable-did", drain=False)
    rtsp_data = adapter.collect(good_id, drain=False)
    assert miot_data is not None
    assert miot_data.meta.did == "miot-stable-did"
    assert len(miot_data.video) == 1
    assert rtsp_data is not None
    assert rtsp_data.meta.did == good_id
    assert rtsp_data.meta.name == "healthy"
    assert rtsp_data.meta.room_name == "office"
    assert len(rtsp_data.video) == 1
    assert adapter.collect(bad_id, drain=False) is None
    assert "operator-secret" not in caplog.text
    assert "private-path" not in caplog.text


@pytest.mark.asyncio
async def test_sync_camera_sources_serializes_apply_then_adapter_sync() -> None:
    events: list[str] = []
    in_flight = 0
    max_in_flight = 0

    class _Source:
        async def apply_settings(self) -> None:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            events.append("apply")
            await __import__("asyncio").sleep(0)

    class _Adapter:
        async def sync_devices(self, all_devices: dict | None = None) -> None:
            del all_devices
            nonlocal in_flight
            events.append("sync")
            in_flight -= 1
            await __import__("asyncio").sleep(0)

    source = _Source()
    adapter = _Adapter()
    service = PerceptionService(
        collector=MagicMock(),
        pipeline=MagicMock(),
        perception_runner=MagicMock(),
        log_repo=MagicMock(),
        rtsp_camera_source=source,  # type: ignore[arg-type]
        camera_adapter=adapter,  # type: ignore[arg-type]
    )

    await __import__("asyncio").gather(
        service.sync_camera_sources(),
        service.sync_camera_sources(),
    )

    assert events == ["apply", "sync", "apply", "sync"]
    assert max_in_flight == 1


@pytest.mark.asyncio
async def test_init_builds_one_adapter_with_miot_and_rtsp_sources() -> None:
    from miloco.perception import init_perception_module

    miot_source = MagicMock(spec=MiotCameraSource)
    rtsp_source = MagicMock(spec=RtspCameraSource)
    camera_adapter = MagicMock()
    adapter_cls = MagicMock(return_value=camera_adapter)
    rtsp_cls = MagicMock(return_value=rtsp_source)
    settings = SimpleNamespace(
        camera=SimpleNamespace(rtsp_sources=["configured-rtsp-source"])
    )
    runner_cls = MagicMock()
    runner_cls.return_value.start = AsyncMock()
    service_cls = MagicMock()

    with (
        patch("miloco.perception.PerceptionLogRepo"),
        patch("miloco.perception.OnDemandLogRepo"),
        patch("miloco.perception.PerceptionEngineProxy"),
        patch("miloco.perception.MiotCameraSource", return_value=miot_source),
        patch("miloco.perception.RtspCameraSource", rtsp_cls),
        patch("miloco.perception.CameraDeviceAdapter", adapter_cls),
        patch("miloco.perception.MultimodalCollector"),
        patch("miloco.perception.PipelineProcessor"),
        patch("miloco.perception.get_settings", return_value=settings),
        patch("miloco.perception.runner.PerceptionRunner", runner_cls),
        patch("miloco.perception.service.PerceptionService", service_cls),
        patch(
            "miloco.perception.engine_state.is_perception_enabled", return_value=False
        ),
        patch("miloco.perception.engine.omni.circuit_breaker.get_omni_circuit_breaker"),
    ):
        await init_perception_module(MagicMock(), MagicMock())
        source_loader = rtsp_cls.call_args.args[0]
        loaded_sources = source_loader()

    assert loaded_sources == ["configured-rtsp-source"]
    assert adapter_cls.call_args.kwargs["sources"] == [miot_source, rtsp_source]
    assert service_cls.call_args.kwargs["rtsp_camera_source"] is rtsp_source
    assert service_cls.call_args.kwargs["camera_adapter"] is camera_adapter
