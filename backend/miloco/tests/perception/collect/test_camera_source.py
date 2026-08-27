from __future__ import annotations

import logging
from typing import Literal

import numpy as np
import pytest
from miloco.perception.collect.camera_adapter import CameraDeviceAdapter
from miloco.perception.collect.camera_source import (
    AudioFrameCallback,
    CameraSourceState,
    VideoFrameCallback,
)
from miloco.perception.types import PerceptionDevice


def _device(did: str) -> PerceptionDevice:
    return PerceptionDevice(
        did=did,
        name=did,
        device_type="camera",
        room_name="test-room",
    )


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

    async def connect_device(
        self,
        did: str,
        video_cb: VideoFrameCallback,
        audio_cb: AudioFrameCallback,
    ) -> None:
        self.connected.append(did)

    async def disconnect_device(self, did: str) -> None:
        self.disconnected.append(did)

    def get_state(self, did: str) -> CameraSourceState:
        return CameraSourceState(
            connected=did in self.connected and did not in self.disconnected
        )

    async def shutdown(self) -> None:
        self.shutdown_count += 1


class _FailOnceConnectSource(_RecordingSource):
    def __init__(self, devices: dict[str, PerceptionDevice]) -> None:
        super().__init__("rtsp", devices)
        self.connect_attempts = 0

    async def connect_device(
        self,
        did: str,
        video_cb: VideoFrameCallback,
        audio_cb: AudioFrameCallback,
    ) -> None:
        self.connect_attempts += 1
        if self.connect_attempts == 1:
            raise RuntimeError("temporary connect failure")
        await super().connect_device(did, video_cb, audio_cb)


class _FailingShutdownSource(_RecordingSource):
    async def shutdown(self) -> None:
        self.shutdown_count += 1
        raise RuntimeError("sensitive shutdown details")


class _AsyncPendingSource(_RecordingSource):
    def __init__(self, devices: dict[str, PerceptionDevice]) -> None:
        super().__init__("rtsp", devices)
        self._registered: set[str] = set()
        self._video_callbacks: dict[str, VideoFrameCallback] = {}

    async def connect_device(
        self,
        did: str,
        video_cb: VideoFrameCallback,
        audio_cb: AudioFrameCallback,
    ) -> None:
        self._registered.add(did)
        self._video_callbacks[did] = video_cb

    def get_state(self, did: str) -> CameraSourceState:
        return CameraSourceState(connected=False)

    def retain_pending_connection(self, did: str) -> bool:
        return did in self._registered

    async def emit_video(self, did: str) -> None:
        callback = self._video_callbacks[did]
        await callback(
            did,
            np.ones((2, 2, 3), dtype=np.uint8),
            100,
            0,
            1_000,
            1_001,
        )


class _SynchronousFailedSource(_RecordingSource):
    async def connect_device(
        self,
        did: str,
        video_cb: VideoFrameCallback,
        audio_cb: AudioFrameCallback,
    ) -> None:
        return None

    def get_state(self, did: str) -> CameraSourceState:
        return CameraSourceState(connected=False)


class _PendingDecisionSource(_RecordingSource):
    def __init__(
        self,
        devices: dict[str, PerceptionDevice],
        *,
        decision: str = "active",
        connected: bool = False,
    ) -> None:
        super().__init__("rtsp", devices)
        self.decision = decision
        self.network_connected = connected
        self.registered: set[str] = set()
        self.connect_attempts = 0
        self.capability_calls = 0

    async def connect_device(
        self,
        did: str,
        video_cb: VideoFrameCallback,
        audio_cb: AudioFrameCallback,
    ) -> None:
        self.connect_attempts += 1
        self.registered.add(did)

    async def disconnect_device(self, did: str) -> None:
        self.registered.discard(did)
        await super().disconnect_device(did)

    def get_state(self, did: str) -> CameraSourceState:
        return CameraSourceState(connected=self.network_connected)

    def retain_pending_connection(self, did: str) -> object:
        self.capability_calls += 1
        if self.decision == "raise":
            raise RuntimeError("operator-secret capability failure")
        if self.decision == "text":
            return "yes"
        if self.decision == "coroutine":

            async def _invalid_async_decision() -> bool:
                return True

            return _invalid_async_decision()
        return did in self.registered and self.decision == "active"


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
    adapter = CameraDeviceAdapter(sources=[miot, rtsp])

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
    adapter = CameraDeviceAdapter(sources=[miot, rtsp])

    with pytest.raises(
        RuntimeError,
        match="Duplicate camera DID 'shared'.*miot.*rtsp",
    ):
        await adapter.discover_devices()


@pytest.mark.asyncio
async def test_failed_connect_removes_state_and_next_sync_retries() -> None:
    source = _FailOnceConnectSource({"flaky": _device("flaky")})
    adapter = CameraDeviceAdapter(sources=[source])

    await adapter.sync_devices()

    assert adapter.get_connected_devices() == {}
    assert source.connect_attempts == 1

    await adapter.sync_devices()

    assert set(adapter.get_connected_devices()) == {"flaky"}
    assert source.connect_attempts == 2


@pytest.mark.asyncio
async def test_registered_async_source_retains_buffer_until_late_callback() -> None:
    did = "rtsp:pending"
    source = _AsyncPendingSource({did: _device(did)})
    adapter = CameraDeviceAdapter(sources=[source])

    await adapter.sync_devices()

    assert set(adapter.get_connected_devices()) == {did}
    assert source.get_state(did).connected is False

    await source.emit_video(did)
    collected = adapter.collect(did, drain=False)
    assert collected is not None
    assert collected.meta.did == did
    assert len(collected.video) == 1


@pytest.mark.asyncio
async def test_synchronous_false_source_still_clears_precreated_buffer() -> None:
    did = "miot-failed"
    source = _SynchronousFailedSource("miot", {did: _device(did)})
    adapter = CameraDeviceAdapter(sources=[source])

    await adapter.sync_devices()

    assert adapter.get_connected_devices() == {}
    assert adapter.collect(did, drain=False) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    ["raise", "text", "coroutine"],
)
async def test_invalid_pending_capability_fails_closed_without_leaking_registration(
    decision: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    did = f"rtsp:invalid-{decision}"
    source = _PendingDecisionSource({did: _device(did)}, decision=decision)
    adapter = CameraDeviceAdapter(sources=[source])

    with caplog.at_level(
        logging.WARNING, logger="miloco.perception.collect.camera_adapter"
    ):
        await adapter.sync_devices()

    assert adapter.get_connected_devices() == {}
    assert adapter.collect(did, drain=False) is None
    assert source.registered == set()
    assert source.disconnected == [did]
    assert "operator-secret" not in caplog.text
    assert "capability failure" not in caplog.text
    assert "Failed to connect device" not in caplog.text


@pytest.mark.asyncio
async def test_connected_source_does_not_evaluate_broken_pending_capability() -> None:
    did = "rtsp:connected"
    source = _PendingDecisionSource(
        {did: _device(did)}, decision="raise", connected=True
    )
    adapter = CameraDeviceAdapter(sources=[source])

    await adapter.sync_devices()

    assert set(adapter.get_connected_devices()) == {did}
    assert source.capability_calls == 0
    assert source.registered == {did}
    assert source.disconnected == []


@pytest.mark.asyncio
async def test_terminal_pending_registration_is_pruned_without_same_sync_restart() -> (
    None
):
    did = "rtsp:terminal"
    source = _PendingDecisionSource({did: _device(did)})
    adapter = CameraDeviceAdapter(sources=[source])
    await adapter.sync_devices()
    assert source.connect_attempts == 1

    source.decision = "terminal"
    await adapter.sync_devices()

    assert adapter.get_connected_devices() == {}
    assert source.registered == set()
    assert source.disconnected == [did]
    assert source.connect_attempts == 1

    source.decision = "active"
    await adapter.sync_devices()

    assert set(adapter.get_connected_devices()) == {did}
    assert source.registered == {did}
    assert source.connect_attempts == 2


@pytest.mark.asyncio
async def test_active_disconnected_registration_survives_periodic_reconciliation() -> (
    None
):
    did = "rtsp:reconnecting"
    source = _PendingDecisionSource({did: _device(did)})
    adapter = CameraDeviceAdapter(sources=[source])

    await adapter.sync_devices()
    await adapter.sync_devices()

    assert set(adapter.get_connected_devices()) == {did}
    assert source.connect_attempts == 1
    assert source.disconnected == []


@pytest.mark.asyncio
async def test_shutdown_logs_source_failure_and_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failing = _FailingShutdownSource("miot", {})
    healthy = _RecordingSource("rtsp", {})
    adapter = CameraDeviceAdapter(sources=[failing, healthy])

    with caplog.at_level(
        logging.ERROR, logger="miloco.perception.collect.camera_adapter"
    ):
        await adapter.shutdown()

    assert failing.shutdown_count == 1
    assert healthy.shutdown_count == 1
    assert "Failed to shutdown camera source miot (RuntimeError)" in caplog.text
    assert "sensitive shutdown details" not in caplog.text
