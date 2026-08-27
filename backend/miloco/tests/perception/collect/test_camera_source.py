from __future__ import annotations

import logging
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


class _FailOnceConnectSource(_RecordingSource):
    def __init__(self, devices: dict[str, PerceptionDevice]) -> None:
        super().__init__("rtsp", devices)
        self.connect_attempts = 0

    async def connect_device(self, did: str, video_cb, audio_cb) -> None:
        self.connect_attempts += 1
        if self.connect_attempts == 1:
            raise RuntimeError("temporary connect failure")
        await super().connect_device(did, video_cb, audio_cb)


class _FailingShutdownSource(_RecordingSource):
    async def shutdown(self) -> None:
        self.shutdown_count += 1
        raise RuntimeError("sensitive shutdown details")


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


@pytest.mark.asyncio
async def test_failed_connect_removes_state_and_next_sync_retries() -> None:
    source = _FailOnceConnectSource({"flaky": _device("flaky")})
    adapter = CameraDeviceAdapter(sources=[source])  # type: ignore[arg-type]

    await adapter.sync_devices()

    assert adapter.get_connected_devices() == {}
    assert source.connect_attempts == 1

    await adapter.sync_devices()

    assert set(adapter.get_connected_devices()) == {"flaky"}
    assert source.connect_attempts == 2


@pytest.mark.asyncio
async def test_shutdown_logs_source_failure_and_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failing = _FailingShutdownSource("miot", {})
    healthy = _RecordingSource("rtsp", {})
    adapter = CameraDeviceAdapter(
        sources=[failing, healthy]  # type: ignore[list-item]
    )

    with caplog.at_level(
        logging.ERROR, logger="miloco.perception.collect.camera_adapter"
    ):
        await adapter.shutdown()

    assert failing.shutdown_count == 1
    assert healthy.shutdown_count == 1
    assert "Failed to shutdown camera source miot (RuntimeError)" in caplog.text
    assert "sensitive shutdown details" not in caplog.text
