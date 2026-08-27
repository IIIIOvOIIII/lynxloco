from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Literal, cast
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
    instances: list[_AdapterRtspSession] = []

    def __init__(self, source: RtspSourceSettings) -> None:
        self.source = source
        self.connected = False
        self.active = False
        self.stop_count = 0
        self.instances.append(self)

    async def start(self, video_cb, audio_cb) -> None:
        if "broken" in self.source.uri:
            raise RuntimeError("operator-secret private-path")
        self.active = True
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
        self.stop_count += 1
        self.active = False
        self.connected = False

    def state(self) -> CameraSourceState:
        return CameraSourceState(connected=self.connected)

    def is_active(self) -> bool:
        return self.active


@pytest.fixture(autouse=True)
def _reset_adapter_rtsp_sessions() -> None:
    _AdapterRtspSession.instances = []


class _PausingDiscoveryAdapter(CameraDeviceAdapter):
    def __init__(self, sources) -> None:
        super().__init__(sources=sources)
        self.snapshot_ready = asyncio.Event()
        self.release_snapshot = asyncio.Event()
        self._pause_next_discovery = False

    def arm_snapshot_pause(self) -> None:
        self.snapshot_ready.clear()
        self.release_snapshot.clear()
        self._pause_next_discovery = True

    async def discover_devices(self, *args, **kwargs):
        discovered = await super().discover_devices(*args, **kwargs)
        if self._pause_next_discovery:
            self._pause_next_discovery = False
            self.snapshot_ready.set()
            await self.release_snapshot.wait()
        return discovered


class _CountingRtspSource(RtspCameraSource):
    def __init__(self, settings_loader) -> None:
        super().__init__(settings_loader)
        self.connect_calls = 0

    async def connect_device(self, did, video_cb, audio_cb) -> None:
        self.connect_calls += 1
        await super().connect_device(did, video_cb, audio_cb)


async def _yield_until_done(task: asyncio.Task, *, turns: int = 20) -> bool:
    for _ in range(turns):
        if task.done():
            return True
        await asyncio.sleep(0)
    return task.done()


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
        async def apply_settings(self):
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            events.append("apply")
            await asyncio.sleep(0)
            return SimpleNamespace(success=True, reconcile_dids=frozenset())

    class _Adapter:
        async def reconcile_and_sync(
            self, disconnect_dids: frozenset[str], *, connect_enabled: bool
        ) -> bool:
            assert disconnect_dids == frozenset()
            assert connect_enabled is True
            nonlocal in_flight
            events.append("sync")
            in_flight -= 1
            await asyncio.sleep(0)
            return True

    source = _Source()
    adapter = _Adapter()
    runner = MagicMock()
    runner.is_running = True
    service = PerceptionService(
        collector=MagicMock(),
        pipeline=MagicMock(),
        perception_runner=runner,
        log_repo=MagicMock(),
        rtsp_camera_source=source,  # type: ignore[arg-type]
        camera_adapter=adapter,  # type: ignore[arg-type]
    )

    await asyncio.gather(
        service.sync_camera_sources(),
        service.sync_camera_sources(),
    )

    assert events == ["apply", "sync", "apply", "sync"]
    assert max_in_flight == 1


@pytest.mark.asyncio
async def test_restart_failure_has_no_phantom_and_later_sync_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "miloco.perception.collect.rtsp_camera_source.RtspSession",
        _AdapterRtspSession,
    )
    camera_id = "rtsp:00000000-0000-0000-0000-000000000011"
    settings = [
        RtspSourceSettings(
            id=camera_id,
            name="front door",
            uri="rtsp://healthy.example/stream",
            enabled=True,
        )
    ]
    rtsp = RtspCameraSource(lambda: settings)
    adapter = CameraDeviceAdapter(sources=[rtsp])
    runner = MagicMock()
    runner.is_running = True
    service = PerceptionService(
        collector=MagicMock(),
        pipeline=MagicMock(),
        perception_runner=runner,
        log_repo=MagicMock(),
        rtsp_camera_source=rtsp,
        camera_adapter=adapter,
    )
    await adapter.sync_devices()
    original = rtsp.get_session(camera_id)

    settings = [settings[0].model_copy(update={"uri": "rtsp://broken.example/new"})]
    applied = await service.sync_camera_sources()

    assert applied is False
    assert rtsp.get_state(camera_id).connected is False
    assert rtsp.get_session(camera_id) is None
    assert adapter.get_connected_devices() == {}
    failed_replacement = next(
        session
        for session in _AdapterRtspSession.instances
        if session.source.uri == "rtsp://broken.example/new"
    )
    assert failed_replacement.connected is False
    assert failed_replacement.stop_count == 1
    assert original is not failed_replacement

    settings = [
        settings[0].model_copy(update={"uri": "rtsp://recovered.example/stream"})
    ]
    assert await service.sync_camera_sources() is True
    assert set(adapter.get_connected_devices()) == {camera_id}
    assert rtsp.get_state(camera_id).connected is True


@pytest.mark.asyncio
async def test_real_source_adapter_apply_diff_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "miloco.perception.collect.rtsp_camera_source.RtspSession",
        _AdapterRtspSession,
    )
    camera_id = "rtsp:00000000-0000-0000-0000-000000000021"
    configured = RtspSourceSettings(
        id=camera_id,
        name="office camera",
        room_name="office",
        uri="rtsp://healthy.example/stream",
        enabled=False,
    )
    settings = [configured]
    rtsp = RtspCameraSource(lambda: settings)
    adapter = CameraDeviceAdapter(sources=[rtsp])
    runner = MagicMock()
    runner.is_running = True
    service = PerceptionService(
        collector=MagicMock(),
        pipeline=MagicMock(),
        perception_runner=runner,
        log_repo=MagicMock(),
        rtsp_camera_source=rtsp,
        camera_adapter=adapter,
    )

    assert await service.sync_camera_sources() is True
    assert adapter.get_connected_devices() == {}

    settings = [configured.model_copy(update={"enabled": True})]
    assert await service.sync_camera_sources() is True
    first_session = rtsp.get_session(camera_id)
    assert first_session is not None
    assert set(adapter.get_connected_devices()) == {camera_id}

    settings = [settings[0].model_copy(update={"name": "renamed", "room_name": "hall"})]
    assert await service.sync_camera_sources() is True
    assert rtsp.get_session(camera_id) is first_session
    assert adapter.get_connected_devices()[camera_id].name == "renamed"
    assert adapter.get_connected_devices()[camera_id].room_name == "hall"

    settings = [
        settings[0].model_copy(update={"uri": "rtsp://replacement.example/stream"})
    ]
    assert await service.sync_camera_sources() is True
    replacement = rtsp.get_session(camera_id)
    assert replacement is not first_session
    assert isinstance(first_session, _AdapterRtspSession)
    assert isinstance(replacement, _AdapterRtspSession)
    assert first_session.stop_count == 1
    assert replacement.stop_count == 0
    assert len(_AdapterRtspSession.instances) == 2

    settings = [settings[0].model_copy(update={"enabled": False})]
    assert await service.sync_camera_sources() is True
    assert adapter.get_connected_devices() == {}

    added_id = "rtsp:00000000-0000-0000-0000-000000000022"
    settings = [
        RtspSourceSettings(
            id=added_id,
            name="added",
            uri="rtsp://added.example/stream",
            enabled=True,
        )
    ]
    assert await service.sync_camera_sources() is True
    assert set(adapter.get_connected_devices()) == {added_id}

    settings = []
    assert await service.sync_camera_sources() is True
    assert adapter.get_connected_devices() == {}


@pytest.mark.asyncio
async def test_paused_engine_applies_settings_without_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "miloco.perception.collect.rtsp_camera_source.RtspSession",
        _AdapterRtspSession,
    )
    settings = [
        RtspSourceSettings(
            id="rtsp:00000000-0000-0000-0000-000000000031",
            name="paused camera",
            uri="rtsp://paused.example/stream",
            enabled=True,
        )
    ]
    rtsp = RtspCameraSource(lambda: settings)
    adapter = CameraDeviceAdapter(sources=[rtsp])
    runner = MagicMock()
    runner.is_running = False
    service = PerceptionService(
        collector=MagicMock(),
        pipeline=MagicMock(),
        perception_runner=runner,
        log_repo=MagicMock(),
        rtsp_camera_source=rtsp,
        camera_adapter=adapter,
    )

    assert await service.sync_camera_sources() is True

    assert adapter.get_connected_devices() == {}
    assert rtsp.get_session(settings[0].id) is None
    assert _AdapterRtspSession.instances == []


@pytest.mark.asyncio
async def test_stopped_engine_does_not_connect_new_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "miloco.perception.collect.rtsp_camera_source.RtspSession",
        _AdapterRtspSession,
    )
    first_id = "rtsp:00000000-0000-0000-0000-000000000041"
    settings = [
        RtspSourceSettings(
            id=first_id,
            name="running camera",
            uri="rtsp://running.example/stream",
            enabled=True,
        )
    ]
    rtsp = RtspCameraSource(lambda: settings)
    adapter = CameraDeviceAdapter(sources=[rtsp])
    runner = MagicMock()
    runner.is_running = True
    service = PerceptionService(
        collector=MagicMock(),
        pipeline=MagicMock(),
        perception_runner=runner,
        log_repo=MagicMock(),
        rtsp_camera_source=rtsp,
        camera_adapter=adapter,
    )
    assert await service.sync_camera_sources() is True
    assert set(adapter.get_connected_devices()) == {first_id}

    await adapter.shutdown()
    runner.is_running = False
    second_id = "rtsp:00000000-0000-0000-0000-000000000042"
    settings = [
        RtspSourceSettings(
            id=second_id,
            name="new while stopped",
            uri="rtsp://stopped.example/stream",
            enabled=True,
        )
    ]

    assert await service.sync_camera_sources() is True
    assert adapter.get_connected_devices() == {}
    assert rtsp.get_session(second_id) is None
    assert len(_AdapterRtspSession.instances) == 1


@pytest.mark.asyncio
async def test_stop_race_cannot_reconnect_after_shutdown() -> None:
    events: list[str] = []
    sync_entered = asyncio.Event()
    release_sync = asyncio.Event()

    class _Source:
        async def apply_settings(self):
            return SimpleNamespace(success=True, reconcile_dids=frozenset())

    class _Adapter:
        async def reconcile_and_sync(
            self, disconnect_dids: frozenset[str], *, connect_enabled: bool
        ) -> bool:
            assert disconnect_dids == frozenset()
            assert connect_enabled is True
            sync_entered.set()
            await release_sync.wait()
            events.append("sync")
            return True

        async def shutdown(self) -> None:
            events.append("shutdown")

    class _Runner:
        def __init__(self, adapter: _Adapter) -> None:
            self.is_running = True
            self._adapter = adapter

        async def stop(self) -> None:
            self.is_running = False
            await self._adapter.shutdown()

    adapter = _Adapter()
    runner = _Runner(adapter)
    from miloco.perception.runner import PerceptionRunner

    service = PerceptionService(
        collector=MagicMock(),
        pipeline=MagicMock(),
        perception_runner=cast(PerceptionRunner, runner),
        log_repo=MagicMock(),
        rtsp_camera_source=_Source(),  # type: ignore[arg-type]
        camera_adapter=adapter,  # type: ignore[arg-type]
    )

    sync_task = asyncio.create_task(service.sync_camera_sources())
    await sync_entered.wait()
    stop_task = asyncio.create_task(service.stop_engine())
    await asyncio.sleep(0)
    assert events == []

    release_sync.set()
    await asyncio.gather(sync_task, stop_task)

    assert events == ["sync", "shutdown"]


@pytest.mark.asyncio
async def test_periodic_stale_disabled_snapshot_cannot_undo_management_enable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "miloco.perception.collect.rtsp_camera_source.RtspSession",
        _AdapterRtspSession,
    )
    camera_id = "rtsp:00000000-0000-0000-0000-000000000051"
    configured = RtspSourceSettings(
        id=camera_id,
        name="race camera",
        uri="rtsp://race.example/stream",
        enabled=False,
    )
    settings = [configured]
    rtsp = RtspCameraSource(lambda: settings)
    adapter = _PausingDiscoveryAdapter([rtsp])
    runner = MagicMock()
    runner.is_running = True
    service = PerceptionService(
        collector=MagicMock(),
        pipeline=MagicMock(),
        perception_runner=runner,
        log_repo=MagicMock(),
        rtsp_camera_source=rtsp,
        camera_adapter=adapter,
    )

    adapter.arm_snapshot_pause()
    periodic = asyncio.create_task(adapter.sync_devices())
    await adapter.snapshot_ready.wait()
    settings = [configured.model_copy(update={"enabled": True})]
    management = asyncio.create_task(service.sync_camera_sources())
    management_finished_while_periodic_owned_sync = await _yield_until_done(management)

    adapter.release_snapshot.set()
    await asyncio.gather(periodic, management)

    assert management_finished_while_periodic_owned_sync is False
    assert set(adapter.get_connected_devices()) == {camera_id}
    assert rtsp.get_session(camera_id) is not None


@pytest.mark.asyncio
async def test_periodic_stale_enabled_snapshot_cannot_race_management_disable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "miloco.perception.collect.rtsp_camera_source.RtspSession",
        _AdapterRtspSession,
    )
    camera_id = "rtsp:00000000-0000-0000-0000-000000000052"
    configured = RtspSourceSettings(
        id=camera_id,
        name="reverse race camera",
        uri="rtsp://reverse-race.example/stream",
        enabled=True,
    )
    settings = [configured]
    rtsp = _CountingRtspSource(lambda: settings)
    adapter = _PausingDiscoveryAdapter([rtsp])
    runner = MagicMock()
    runner.is_running = True
    service = PerceptionService(
        collector=MagicMock(),
        pipeline=MagicMock(),
        perception_runner=runner,
        log_repo=MagicMock(),
        rtsp_camera_source=rtsp,
        camera_adapter=adapter,
    )
    await adapter.sync_devices()
    rtsp.connect_calls = 0

    adapter.arm_snapshot_pause()
    periodic = asyncio.create_task(adapter.sync_devices())
    await adapter.snapshot_ready.wait()
    settings = [configured.model_copy(update={"enabled": False})]
    management = asyncio.create_task(service.sync_camera_sources())
    management_finished_while_periodic_owned_sync = await _yield_until_done(management)

    adapter.release_snapshot.set()
    await asyncio.gather(periodic, management)

    assert management_finished_while_periodic_owned_sync is False
    assert adapter.get_connected_devices() == {}
    assert rtsp.get_session(camera_id) is None
    assert rtsp.connect_calls == 0


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
