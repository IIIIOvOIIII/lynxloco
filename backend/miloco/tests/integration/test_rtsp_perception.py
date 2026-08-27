from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import av
import numpy as np
import pytest
from miloco.config.settings import RtspSourceSettings
from miloco.perception.collect import rtsp_session as session_module
from miloco.perception.collect.camera_adapter import CameraDeviceAdapter
from miloco.perception.collect.collector import MultimodalCollector
from miloco.perception.collect.rtsp_camera_source import RtspCameraSource
from miloco.perception.schema import DeviceData

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "rtsp"


class _FixtureOpener:
    def __init__(
        self,
        fixture: Path,
        open_fixture: Callable[..., av.container.InputContainer],
    ) -> None:
        self._fixture = fixture
        self._open_fixture = open_fixture

    def __call__(self, _url: str, **_options: object) -> av.container.InputContainer:
        return self._open_fixture(str(self._fixture))


async def _wait_for_device_data(
    collector: MultimodalCollector,
    camera_id: str,
    *,
    timeout: float = 2.0,
) -> DeviceData:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        data = collector.collect(camera_id, drain=False)
        if data is not None and data.video:
            return data
        await asyncio.sleep(0.01)
    raise AssertionError(f"No decoded DeviceData for {camera_id}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fixture_name", "camera_id", "expected_codec", "expect_audio"),
    [
        (
            "h264_video_audio.mkv",
            "rtsp:00000000-0000-0000-0000-000000000801",
            "h264",
            True,
        ),
        (
            "h265_video_only.mkv",
            "rtsp:00000000-0000-0000-0000-000000000802",
            "hevc",
            False,
        ),
    ],
)
async def test_fixture_media_reaches_device_data_through_rtsp_perception_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    fixture_name: str,
    camera_id: str,
    expected_codec: str,
    expect_audio: bool,
) -> None:
    setting = RtspSourceSettings(
        id=camera_id,
        name=f"fixture-{expected_codec}",
        room_name="integration-room",
        uri="rtsp://fixture.invalid/stream",
        audio_enabled=True,
        enabled=True,
    )
    source = RtspCameraSource(lambda: [setting])
    adapter = CameraDeviceAdapter(sources=[source])
    collector = MultimodalCollector([adapter])
    original_open = session_module.av.open
    monkeypatch.setattr(
        session_module.av,
        "open",
        _FixtureOpener(_FIXTURES / fixture_name, original_open),
    )

    try:
        await collector.sync_all_devices()
        data = await _wait_for_device_data(collector, camera_id)
        state = source.get_state(camera_id)

        assert data.meta.did == camera_id
        assert data.meta.name == f"fixture-{expected_codec}"
        assert data.meta.room_id == "integration-room"
        assert data.meta.room_name == "integration-room"
        assert state.video_codec == expected_codec
        assert state.width == 64
        assert state.height == 48
        assert data.video
        assert data.video[0].frame.shape == (48, 64, 3)
        assert data.video[0].frame.dtype == np.uint8
        assert data.video[0].stream_ts >= 0
        assert data.video[0].unix_ms > 0

        if expect_audio:
            assert data.audio
            assert data.audio[0].frame.dtype == np.int16
            assert data.audio[0].frame.ndim == 1
            assert data.get_pcm_ndarray(sample_rate=16_000).size > 0
        else:
            assert data.audio == []
            assert data.get_pcm_ndarray(sample_rate=16_000).size == 0
    finally:
        await collector.shutdown()
