from __future__ import annotations

import gc
import math
import weakref
from typing import Literal

import numpy as np
import pytest
from miloco.config import get_settings
from miloco.perception.collect.camera_adapter import CameraDeviceAdapter
from miloco.perception.collect.camera_source import (
    AudioFrameCallback,
    CameraSourceState,
    VideoFrameCallback,
)
from miloco.perception.schema import DecodedVideoFrame
from miloco.perception.types import PerceptionDevice


class _FrameSource:
    def __init__(self, source_type: Literal["miot", "rtsp"], *dids: str) -> None:
        self.source_type = source_type
        self._devices = {did: _device(did) for did in dids}
        self._connected = {did: False for did in dids}
        self.video_callbacks: dict[str, VideoFrameCallback] = {}
        self.audio_callbacks: dict[str, AudioFrameCallback] = {}
        self.retain_pending = False
        self.fail_connect = False

    async def discover_devices(
        self, all_devices: dict | None = None, **filters: object
    ) -> dict[str, PerceptionDevice]:
        return self._devices

    async def connect_device(
        self,
        did: str,
        video_cb: VideoFrameCallback,
        audio_cb: AudioFrameCallback,
    ) -> None:
        self.video_callbacks[did] = video_cb
        self.audio_callbacks[did] = audio_cb
        if self.fail_connect:
            raise RuntimeError("test connect failure")
        self._connected[did] = True

    async def disconnect_device(self, did: str) -> None:
        self._connected[did] = False

    def get_state(self, did: str) -> CameraSourceState:
        return CameraSourceState(connected=self._connected[did])

    def retain_pending_connection(self, did: str) -> bool:
        return self.retain_pending

    async def shutdown(self) -> None:
        for did in self._connected:
            self._connected[did] = False

    async def emit_video(
        self,
        did: str,
        frame: np.ndarray,
        *,
        stream_ts: int = 0,
    ) -> None:
        await self.video_callbacks[did](did, frame, stream_ts, 0)


def _device(did: str) -> PerceptionDevice:
    return PerceptionDevice(
        did=did,
        name=did,
        device_type="camera",
        room_name="test-room",
    )


async def _connect(
    adapter: CameraDeviceAdapter, source: _FrameSource, did: str
) -> None:
    await adapter.connect_device(did, source=source._devices[did])


@pytest.mark.asyncio
async def test_rtsp_admits_three_host_monotonic_frames_from_twenty_five_fps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing adapter admission makes all 25 frames reach the real buffer."""
    did = "rtsp-one"
    source = _FrameSource("rtsp", did)
    adapter = CameraDeviceAdapter(sources=[source], perception_fps_provider=lambda: 3)
    wall_times = iter(range(0, 1_000, 40))
    monkeypatch.setattr(
        "miloco.perception.collect.camera_adapter._monotonic_ms",
        lambda: next(wall_times),
    )

    await _connect(adapter, source, did)
    for index in range(25):
        await source.emit_video(
            did,
            np.full((1, 1, 3), index, dtype=np.uint8),
            stream_ts=index * 40,
        )

    collected = adapter.collect(did, drain=False)
    assert collected is not None
    assert [frame.wall_ms for frame in collected.video] == [0, 360, 720]
    state = adapter._devices[did]
    assert state.rtsp_admitted_frames == 3
    assert state.rtsp_dropped_frames == 22


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fps", "wall_times", "admitted", "dropped"),
    [
        (1, [0, 999, 1_000], 2, 1),
        (10, [0, 99, 100, 199, 200], 3, 2),
    ],
)
async def test_rtsp_admission_uses_configured_fps_boundary(
    monkeypatch: pytest.MonkeyPatch,
    fps: int,
    wall_times: list[int],
    admitted: int,
    dropped: int,
) -> None:
    """A wrong interval boundary changes the number of buffered frames."""
    did = f"rtsp-{fps}-fps"
    source = _FrameSource("rtsp", did)
    adapter = CameraDeviceAdapter(sources=[source], perception_fps_provider=lambda: fps)
    clock = iter(wall_times)
    monkeypatch.setattr(
        "miloco.perception.collect.camera_adapter._monotonic_ms", lambda: next(clock)
    )

    await _connect(adapter, source, did)
    for index in range(len(wall_times)):
        await source.emit_video(did, np.zeros((1, 1, 3), dtype=np.uint8))

    state = adapter._devices[did]
    assert state.rtsp_admitted_frames == admitted
    assert state.rtsp_dropped_frames == dropped


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [0, -1, "invalid", None, float("inf")])
async def test_invalid_injected_rtsp_fps_fails_closed_to_one(raw: object) -> None:
    """Unsafe provider values must not leave RTSP admission disabled or raise."""
    did = "rtsp-invalid-fps"
    source = _FrameSource("rtsp", did)
    adapter = CameraDeviceAdapter(sources=[source], perception_fps_provider=lambda: raw)

    await _connect(adapter, source, did)

    assert adapter._devices[did].rtsp_target_fps == 1


@pytest.mark.asyncio
async def test_rtsp_admission_ignores_repeated_reversed_and_missing_equivalent_pts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Using camera PTS would admit a different set of otherwise identical frames."""
    did = "rtsp-pts-independent"
    source = _FrameSource("rtsp", did)
    adapter = CameraDeviceAdapter(sources=[source], perception_fps_provider=lambda: 3)
    clock = iter([0, 100, 334, 500])
    monkeypatch.setattr(
        "miloco.perception.collect.camera_adapter._monotonic_ms", lambda: next(clock)
    )

    await _connect(adapter, source, did)
    for stream_ts in [7_000, 7_000, -50, 0]:
        await source.emit_video(
            did, np.zeros((1, 1, 3), dtype=np.uint8), stream_ts=stream_ts
        )

    collected = adapter.collect(did, drain=False)
    assert collected is not None
    assert [frame.wall_ms for frame in collected.video] == [0, 334]
    assert adapter._devices[did].rtsp_dropped_frames == 2


@pytest.mark.asyncio
async def test_rtsp_devices_keep_independent_admission_clocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sharing a deadline between DIDs incorrectly discards the second camera's first frame."""
    first, second = "rtsp-first", "rtsp-second"
    source = _FrameSource("rtsp", first, second)
    adapter = CameraDeviceAdapter(sources=[source], perception_fps_provider=lambda: 3)
    clock = iter([0, 100, 100, 400, 300, 450])
    monkeypatch.setattr(
        "miloco.perception.collect.camera_adapter._monotonic_ms", lambda: next(clock)
    )

    await _connect(adapter, source, first)
    await _connect(adapter, source, second)
    for did in [first, first, second, first, second, second]:
        await source.emit_video(did, np.zeros((1, 1, 3), dtype=np.uint8))

    assert (
        adapter._devices[first].rtsp_admitted_frames,
        adapter._devices[first].rtsp_dropped_frames,
    ) == (2, 1)
    assert (
        adapter._devices[second].rtsp_admitted_frames,
        adapter._devices[second].rtsp_dropped_frames,
    ) == (2, 1)


@pytest.mark.asyncio
async def test_miot_video_callbacks_remain_unthrottled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Applying RTSP admission to MIoT would drop real MIoT perception frames."""
    did = "miot-unthrottled"
    source = _FrameSource("miot", did)
    adapter = CameraDeviceAdapter(sources=[source], perception_fps_provider=lambda: 1)
    clock = iter([0, 40, 80, 120, 160, 200])
    monkeypatch.setattr(
        "miloco.perception.collect.camera_adapter._monotonic_ms", lambda: next(clock)
    )

    await _connect(adapter, source, did)
    for index in range(6):
        await source.emit_video(
            did, np.zeros((1, 1, 3), dtype=np.uint8), stream_ts=index * 40
        )

    collected = adapter.collect(did, drain=False)
    assert collected is not None
    assert len(collected.video) == 6


@pytest.mark.asyncio
async def test_rtsp_policy_uses_decoded_array_bytes_and_leaves_miot_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wiring policy to all sources or metadata-sized payloads breaks retention bounds."""
    rtsp_did, miot_did = "rtsp-policy", "miot-policy"
    rtsp = _FrameSource("rtsp", rtsp_did)
    miot = _FrameSource("miot", miot_did)
    adapter = CameraDeviceAdapter(
        sources=[rtsp, miot], perception_fps_provider=lambda: 3
    )
    clock = iter([0, 0])
    monkeypatch.setattr(
        "miloco.perception.collect.camera_adapter._monotonic_ms", lambda: next(clock)
    )

    await adapter.discover_devices()
    await _connect(adapter, rtsp, rtsp_did)
    await _connect(adapter, miot, miot_did)
    rtsp_state = adapter._devices[rtsp_did]
    miot_state = adapter._devices[miot_did]
    policy = rtsp_state.sync_buffer._retention_policy
    assert policy is not None
    assert policy.track == "decoded_video"
    assert (
        policy.max_items_per_window
        == math.ceil(3 * get_settings().perception.collect.window_size) + 1
    )
    assert policy.max_payload_bytes == 128 * 1024 * 1024
    assert miot_state.sync_buffer._retention_policy is None

    frame = np.zeros((1, 2, 3), dtype=np.uint8)
    await rtsp.emit_video(rtsp_did, frame)
    assert rtsp_state.sync_buffer.retained_payload_bytes == frame.nbytes
    decoded = DecodedVideoFrame(
        frame=frame,
        stream_ts=0,
        wall_ms=0,
        unix_ms=0,
    )
    assert policy.payload_size(decoded) == frame.nbytes


@pytest.mark.asyncio
async def test_clear_buffers_releases_frames_without_resetting_rtsp_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clearing buffers must free payloads but must not restart a live source's cadence."""
    did = "rtsp-clear"
    source = _FrameSource("rtsp", did)
    adapter = CameraDeviceAdapter(sources=[source], perception_fps_provider=lambda: 1)
    clock = iter([0, 500])
    monkeypatch.setattr(
        "miloco.perception.collect.camera_adapter._monotonic_ms", lambda: next(clock)
    )

    await _connect(adapter, source, did)
    frame = np.zeros((2, 2, 3), dtype=np.uint8)
    frame_ref = weakref.ref(frame)
    await source.emit_video(did, frame)
    del frame
    adapter.clear_buffers()
    gc.collect()

    state = adapter._devices[did]
    assert frame_ref() is None
    assert state.sync_buffer.retained_payload_bytes == 0
    await source.emit_video(did, np.zeros((1, 1, 3), dtype=np.uint8))
    assert state.rtsp_admitted_frames == 1
    assert state.rtsp_dropped_frames == 1


@pytest.mark.asyncio
async def test_rtsp_reconnect_prune_and_shutdown_start_fresh_epochs_and_release_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retaining a removed state leaks frames and causes a reconnected source to drop first frame."""
    did = "rtsp-lifecycle"
    source = _FrameSource("rtsp", did)
    adapter = CameraDeviceAdapter(sources=[source], perception_fps_provider=lambda: 1)
    clock = iter([0, 100, 200, 300])
    monkeypatch.setattr(
        "miloco.perception.collect.camera_adapter._monotonic_ms", lambda: next(clock)
    )

    await _connect(adapter, source, did)
    first_frame = np.zeros((2, 2, 3), dtype=np.uint8)
    first_ref = weakref.ref(first_frame)
    await source.emit_video(did, first_frame)
    del first_frame
    await adapter.disconnect_device(did)
    gc.collect()
    assert first_ref() is None
    assert did not in adapter.get_connected_devices()

    await _connect(adapter, source, did)
    await source.emit_video(did, np.zeros((1, 1, 3), dtype=np.uint8))
    assert adapter._devices[did].rtsp_admitted_frames == 1

    source._connected[did] = False
    await adapter.reconcile_and_sync(frozenset(), connect_enabled=False)
    assert did not in adapter.get_connected_devices()

    await _connect(adapter, source, did)
    last_frame = np.zeros((2, 2, 3), dtype=np.uint8)
    last_ref = weakref.ref(last_frame)
    await source.emit_video(did, last_frame)
    del last_frame
    await adapter.shutdown()
    gc.collect()
    assert last_ref() is None
    assert adapter.get_connected_devices() == {}


@pytest.mark.asyncio
async def test_failed_rtsp_connect_cleans_up_unregistered_state() -> None:
    """A failed connection must not leave a state that carries a future deadline."""
    did = "rtsp-failed-connect"
    source = _FrameSource("rtsp", did)
    source.fail_connect = True
    adapter = CameraDeviceAdapter(sources=[source], perception_fps_provider=lambda: 3)

    with pytest.raises(RuntimeError, match="test connect failure"):
        await _connect(adapter, source, did)

    assert did not in adapter.get_connected_devices()
