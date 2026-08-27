from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import pytest
from miloco.perception.collect.camera_adapter import CameraDeviceAdapter
from miloco.perception.collect.camera_source import CameraSourceState
from miloco.perception.types import PerceptionDevice


def _device(did: str) -> PerceptionDevice:
    return PerceptionDevice(
        did=did,
        name=did,
        device_type="camera",
        room_name="test-room",
    )


@dataclass
class _State:
    connected: bool


class _RecordingSource:
    def __init__(
        self,
        source_type: Literal["miot", "rtsp"],
        devices: dict[str, PerceptionDevice],
    ) -> None:
        self.source_type = source_type
        self._devices = devices
        self.connected: list[str] = []
        self.disconnected: list[str] = []
        self.shutdown_count = 0

    async def discover_devices(
        self, all_devices: dict | None = None, **filters: object
    ) -> dict[str, PerceptionDevice]:
        return self._devices

    async def connect_device(self, did: str, video_cb, audio_cb) -> None:
        self.connected.append(did)

    async def disconnect_device(self, did: str) -> None:
        self.disconnected.append(did)

    def get_state(self, did: str) -> _State:
        return _State(connected=did in self.connected and did not in self.disconnected)

    async def shutdown(self) -> None:
        self.shutdown_count += 1


def test_camera_source_state_has_safe_disconnected_defaults() -> None:
    assert CameraSourceState(connected=False) == CameraSourceState(
        connected=False,
        video_codec=None,
        audio_codec=None,
        width=None,
        height=None,
        fps=None,
        last_frame_unix_ms=None,
        reconnect_attempt=0,
        dropped_frames=0,
        error_code=None,
        error_message=None,
    )


@pytest.mark.asyncio
async def test_adapter_merges_sources_and_routes_each_did_to_its_owner() -> None:
    miot = _RecordingSource(
        "miot",
        {
            "miot-b": _device("miot-b"),
            "miot-a": _device("miot-a"),
        },
    )
    rtsp = _RecordingSource("rtsp", {"rtsp-1": _device("rtsp-1")})
    adapter = CameraDeviceAdapter(sources=[miot, rtsp])  # type: ignore[arg-type]

    discovered = await adapter.discover_devices()

    assert list(discovered) == ["miot-a", "miot-b", "rtsp-1"]
    assert adapter._did_source_types == {
        "miot-a": "miot",
        "miot-b": "miot",
        "rtsp-1": "rtsp",
    }

    await adapter.connect_device("miot-a", source=discovered["miot-a"])
    await adapter.connect_device("rtsp-1", source=discovered["rtsp-1"])
    await adapter.disconnect_device("miot-a")

    assert miot.connected == ["miot-a"]
    assert miot.disconnected == ["miot-a"]
    assert rtsp.connected == ["rtsp-1"]
    assert rtsp.disconnected == []

    await adapter.shutdown()

    assert rtsp.disconnected == ["rtsp-1"]
    assert miot.shutdown_count == 1
    assert rtsp.shutdown_count == 1


@pytest.mark.asyncio
async def test_adapter_rejects_duplicate_dids_across_sources() -> None:
    miot = _RecordingSource("miot", {"shared": _device("shared")})
    rtsp = _RecordingSource("rtsp", {"shared": _device("shared")})
    adapter = CameraDeviceAdapter(sources=[miot, rtsp])  # type: ignore[arg-type]

    with pytest.raises(
        RuntimeError,
        match="Duplicate camera DID 'shared'.*miot.*rtsp",
    ):
        await adapter.discover_devices()
