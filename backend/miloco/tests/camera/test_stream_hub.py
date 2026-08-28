from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from fractions import Fraction
from pathlib import Path
from typing import Literal

import av
import numpy as np
import pytest
from miloco.camera.service import CameraConflictError, CameraService
from miloco.camera.stream import (
    EncodedVideoPacket,
    LiveStreamHub,
    LiveStreamSource,
)


def _packet(value: int, *, keyframe: bool = False) -> EncodedVideoPacket:
    return EncodedVideoPacket(
        codec="h264",
        data=bytes([value]),
        pts=value,
        dts=value,
        is_keyframe=keyframe,
        time_base_num=1,
        time_base_den=90_000,
    )


class _PacketBackend:
    def __init__(self) -> None:
        self.listeners: list[Callable[[EncodedVideoPacket], None]] = []
        self.all_listeners: list[Callable[[EncodedVideoPacket], None]] = []
        self.close_listeners: list[Callable[[str | None], None]] = []
        self.frame_listeners: list[Callable[[np.ndarray, int | None], None]] = []
        self.detach_count = 0
        self.close_detach_count = 0
        self.active = True
        self.terminal = False

    def add_packet_listener(
        self, listener: Callable[[EncodedVideoPacket], None]
    ) -> Callable[[], None]:
        self.listeners.append(listener)
        self.all_listeners.append(listener)
        detached = False

        def detach() -> None:
            nonlocal detached
            if detached:
                return
            detached = True
            self.detach_count += 1
            self.listeners.remove(listener)

        return detach

    def add_close_listener(
        self, listener: Callable[[str | None], None]
    ) -> Callable[[], None]:
        self.close_listeners.append(listener)
        detached = False

        def detach() -> None:
            nonlocal detached
            if detached:
                return
            detached = True
            self.close_detach_count += 1
            if listener in self.close_listeners:
                self.close_listeners.remove(listener)

        return detach

    def stop(self, error_code: str | None = None) -> None:
        self.active = False
        self.terminal = error_code is not None
        for listener in tuple(self.close_listeners):
            listener(error_code)

    def is_active(self) -> bool:
        return self.active

    def is_terminal(self) -> bool:
        return self.terminal

    def emit(self, packet: EncodedVideoPacket) -> None:
        for listener in tuple(self.listeners):
            listener(packet)

    def add_video_frame_listener(
        self, listener: Callable[[np.ndarray, int | None], None]
    ) -> Callable[[], None]:
        self.frame_listeners.append(listener)

        def detach() -> None:
            if listener in self.frame_listeners:
                self.frame_listeners.remove(listener)

        return detach

    def emit_frame(self, value: int, pts: int = 0) -> None:
        frame = np.full((16, 16, 3), value, dtype=np.uint8)
        for listener in tuple(self.frame_listeners):
            listener(frame, pts)


def _source(
    backend: object,
    *,
    codec: str = "h264",
    source_type: Literal["miot", "rtsp"] = "miot",
) -> LiveStreamSource:
    return LiveStreamSource(
        camera_id="rtsp:camera",
        source_type=source_type,
        backend=backend,
        channel=0,
        input_codec=codec,
    )


async def _next(stream) -> bytes:
    return await asyncio.wait_for(anext(stream), timeout=0.5)


def _annexb_nals(data: bytes) -> list[bytes]:
    starts: list[tuple[int, int]] = []
    index = 0
    while index <= len(data) - 3:
        if data[index : index + 4] == b"\x00\x00\x00\x01":
            starts.append((index, 4))
            index += 4
        elif data[index : index + 3] == b"\x00\x00\x01":
            starts.append((index, 3))
            index += 3
        else:
            index += 1
    return [
        data[
            start + size : starts[position + 1][0]
            if position + 1 < len(starts)
            else None
        ]
        for position, (start, size) in enumerate(starts)
    ]


def _libx264_keyframe(profile: Literal["baseline", "high"], value: int) -> bytes:
    codec = av.CodecContext.create("libx264", "w")
    codec.width = 64
    codec.height = 64
    codec.pix_fmt = "yuv420p"
    codec.time_base = Fraction(1, 15)
    codec.framerate = Fraction(15, 1)
    codec.max_b_frames = 0
    codec.options = {
        "profile": profile,
        "preset": "medium",
        "tune": "zerolatency",
        "repeat-headers": "1",
        "annexb": "1",
    }
    codec.open()
    frame = av.VideoFrame.from_ndarray(
        np.full((64, 64, 3), value, dtype=np.uint8), format="bgr24"
    ).reformat(format="yuv420p")
    frame.pts = 0
    frame.time_base = codec.time_base
    return b"".join(bytes(packet) for packet in codec.encode(frame))


@pytest.mark.asyncio
async def test_multiple_subscribers_share_one_source_attachment() -> None:
    backend = _PacketBackend()
    resolve_count = 0

    async def resolve(_camera_id: str) -> LiveStreamSource:
        nonlocal resolve_count
        resolve_count += 1
        return _source(backend)

    hub = LiveStreamHub(resolve)
    first = hub.subscribe("rtsp:camera")
    second = hub.subscribe("rtsp:camera")
    first_next = asyncio.create_task(_next(first))
    second_next = asyncio.create_task(_next(second))
    await asyncio.sleep(0)

    backend.emit(_packet(1, keyframe=True))

    assert await first_next == b"\x01"
    assert await second_next == b"\x01"
    assert resolve_count == 1
    assert len(backend.listeners) == 1
    assert hub.state("rtsp:camera").viewer_count == 2

    await first.aclose()
    assert len(backend.listeners) == 1
    await second.aclose()
    assert backend.detach_count == 1
    assert hub.state("rtsp:camera").viewer_count == 0


@pytest.mark.asyncio
async def test_slow_subscriber_is_bounded_and_drops_to_next_keyframe() -> None:
    backend = _PacketBackend()

    async def resolve(_camera_id: str) -> LiveStreamSource:
        return _source(backend)

    hub = LiveStreamHub(resolve, queue_size=3)
    stream = hub.subscribe("rtsp:camera")
    first_next = asyncio.create_task(_next(stream))
    await asyncio.sleep(0)
    backend.emit(_packet(1, keyframe=True))
    assert await first_next == b"\x01"

    for packet in (
        _packet(2),
        _packet(3),
        _packet(4),
        _packet(5, keyframe=True),
        _packet(6),
    ):
        backend.emit(packet)

    assert await _next(stream) == b"\x05"
    assert await _next(stream) == b"\x06"
    state = hub.state("rtsp:camera")
    assert state.queue_depth <= 3
    assert state.dropped_packets == 3
    await stream.aclose()


@pytest.mark.asyncio
async def test_overflow_without_keyframe_waits_for_fresh_keyframe() -> None:
    backend = _PacketBackend()

    async def resolve(_camera_id: str) -> LiveStreamSource:
        return _source(backend)

    hub = LiveStreamHub(resolve, queue_size=2)
    stream = hub.subscribe("rtsp:camera")
    initial = asyncio.create_task(_next(stream))
    await asyncio.sleep(0)
    backend.emit(_packet(1, keyframe=True))
    assert await initial == b"\x01"

    backend.emit(_packet(2))
    backend.emit(_packet(3))
    backend.emit(_packet(4))
    waiting = asyncio.create_task(_next(stream))
    await asyncio.sleep(0)
    assert not waiting.done()

    backend.emit(_packet(5))
    assert not waiting.done()
    backend.emit(_packet(6, keyframe=True))
    assert await waiting == b"\x06"
    await stream.aclose()


@pytest.mark.asyncio
async def test_close_camera_stops_subscribers_and_detaches_source() -> None:
    backend = _PacketBackend()

    async def resolve(_camera_id: str) -> LiveStreamSource:
        return _source(backend)

    hub = LiveStreamHub(resolve)
    stream = hub.subscribe("rtsp:camera")
    pending = asyncio.create_task(_next(stream))
    await asyncio.sleep(0)

    await hub.close_camera("rtsp:camera")

    with pytest.raises(StopAsyncIteration):
        await pending
    assert backend.detach_count == 1
    assert hub.state("rtsp:camera").mode == "idle"


@pytest.mark.asyncio
async def test_late_packet_from_closed_generation_cannot_reach_replacement() -> None:
    old_backend = _PacketBackend()
    replacement_backend = _PacketBackend()
    backends = iter((old_backend, replacement_backend))

    async def resolve(_camera_id: str) -> LiveStreamSource:
        return _source(next(backends))

    hub = LiveStreamHub(resolve)
    old_stream = hub.subscribe("rtsp:camera")
    old_pending = asyncio.create_task(_next(old_stream))
    await asyncio.sleep(0)
    stale_listener = old_backend.all_listeners[0]
    await hub.close_camera("rtsp:camera")
    with pytest.raises(StopAsyncIteration):
        await old_pending

    replacement = hub.subscribe("rtsp:camera")
    replacement_pending = asyncio.create_task(_next(replacement))
    await asyncio.sleep(0)
    stale_listener(_packet(90, keyframe=True))
    await asyncio.sleep(0)

    assert not replacement_pending.done()
    assert hub.state("rtsp:camera").queue_depth == 0
    replacement_backend.emit(_packet(91, keyframe=True))
    assert await replacement_pending == b"["
    await replacement.aclose()


@pytest.mark.asyncio
async def test_source_shutdown_ends_waiting_viewers_without_hanging() -> None:
    backend = _PacketBackend()

    async def resolve(_camera_id: str) -> LiveStreamSource:
        return _source(backend)

    hub = LiveStreamHub(resolve)
    stream = hub.subscribe("rtsp:camera")
    pending = asyncio.create_task(_next(stream))
    await asyncio.sleep(0)

    backend.stop()

    with pytest.raises(StopAsyncIteration):
        await pending
    assert hub.state("rtsp:camera").mode == "idle"
    assert backend.detach_count == 1
    assert backend.close_detach_count == 1


@pytest.mark.asyncio
async def test_terminal_source_ends_viewers_with_safe_error_state() -> None:
    backend = _PacketBackend()

    async def resolve(_camera_id: str) -> LiveStreamSource:
        return _source(backend)

    hub = LiveStreamHub(resolve)
    stream = hub.subscribe("rtsp:camera")
    pending = asyncio.create_task(_next(stream))
    await asyncio.sleep(0)

    backend.stop("authentication_failed")

    with pytest.raises(StopAsyncIteration):
        await pending
    state = hub.state("rtsp:camera")
    assert state.mode == "error"
    assert state.error_code == "authentication_failed"


class _StopsDuringLifecycleAttach(_PacketBackend):
    def add_close_listener(
        self, listener: Callable[[str | None], None]
    ) -> Callable[[], None]:
        detach = super().add_close_listener(listener)
        self.stop("resource_not_found")
        return detach


@pytest.mark.asyncio
async def test_source_stop_during_attach_closes_new_subscription() -> None:
    backend = _StopsDuringLifecycleAttach()

    async def resolve(_camera_id: str) -> LiveStreamSource:
        return _source(backend)

    hub = LiveStreamHub(resolve)
    stream = hub.subscribe("rtsp:camera")

    with pytest.raises(StopAsyncIteration):
        await _next(stream)
    assert hub.state("rtsp:camera").error_code == "resource_not_found"


@pytest.mark.asyncio
async def test_broken_subscriber_cleanup_does_not_affect_other_viewers() -> None:
    backend = _PacketBackend()

    async def resolve(_camera_id: str) -> LiveStreamSource:
        return _source(backend)

    hub = LiveStreamHub(resolve)
    broken = hub.subscribe("rtsp:camera")
    healthy = hub.subscribe("rtsp:camera")
    broken_next = asyncio.create_task(_next(broken))
    healthy_next = asyncio.create_task(_next(healthy))
    await asyncio.sleep(0)
    broken_next.cancel()
    await asyncio.gather(broken_next, return_exceptions=True)
    await broken.aclose()

    backend.emit(_packet(7, keyframe=True))

    assert await healthy_next == b"\x07"
    assert hub.state("rtsp:camera").viewer_count == 1
    await healthy.aclose()


class _FakeTranscoder:
    def __init__(
        self,
        *,
        fail: bool = False,
        stop_entered: asyncio.Event | None = None,
        stop_release: asyncio.Event | None = None,
    ) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=2)
        self.pushes = 0
        self.stop_count = 0
        self.viewer_count = 0
        self.fail = fail
        self.on_error: Callable[[str], None] | None = None
        self.stop_entered = stop_entered
        self.stop_release = stop_release

    def attach(self) -> AsyncGenerator[bytes, None]:
        async def stream() -> AsyncGenerator[bytes, None]:
            self.viewer_count += 1
            try:
                while (item := await self.queue.get()) is not None:
                    yield item
            finally:
                self.viewer_count -= 1

        return stream()

    async def push_frame(self, frame: np.ndarray, pts: int | None) -> None:
        del pts
        self.pushes += 1
        if self.fail:
            assert self.on_error is not None
            self.on_error("transcode_failed")
            await self.queue.put(None)
            return
        await self.queue.put(b"encoded:" + bytes([int(frame[0, 0, 0])]))

    async def stop(self) -> None:
        self.stop_count += 1
        if self.stop_entered is not None:
            self.stop_entered.set()
        if self.stop_release is not None:
            await self.stop_release.wait()
        while not self.queue.empty():
            self.queue.get_nowait()
        await self.queue.put(None)


class _ReadyBoundaryTranscoder:
    """Expose whether the hub registers output before accepting a frame."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.lazy_attach_release = asyncio.Event()
        self.attached = False
        self.lost_frames = 0

    async def _stream(self) -> AsyncGenerator[bytes, None]:
        try:
            while (item := await self.queue.get()) is not None:
                yield item
        finally:
            self.attached = False

    def attach(self) -> AsyncGenerator[bytes, None]:
        async def lazy_stream() -> AsyncGenerator[bytes, None]:
            await self.lazy_attach_release.wait()
            self.attached = True
            async for chunk in self._stream():
                yield chunk

        return lazy_stream()

    async def attach_ready(self) -> AsyncGenerator[bytes, None]:
        self.attached = True
        return self._stream()

    async def push_frame(self, frame: np.ndarray, pts: int | None) -> None:
        del pts
        if not self.attached:
            self.lost_frames += 1
            return
        await self.queue.put(b"encoded:" + bytes([int(frame[0, 0, 0])]))

    async def stop(self) -> None:
        self.lazy_attach_release.set()
        await self.queue.put(None)


@pytest.mark.asyncio
async def test_transcoder_output_is_attached_before_first_decoded_frame() -> None:
    backend = _PacketBackend()
    transcoder = _ReadyBoundaryTranscoder()

    async def resolve(_camera_id: str) -> LiveStreamSource:
        return _source(backend, codec="hevc", source_type="rtsp")

    hub = LiveStreamHub(
        resolve,
        transcoder_factory=lambda _on_error: transcoder,
    )
    viewer = hub.subscribe("rtsp:camera")
    pending = asyncio.create_task(_next(viewer))
    await asyncio.sleep(0)

    backend.emit_frame(23, 100)

    assert await asyncio.wait_for(pending, 0.5) == b"encoded:\x17"
    assert transcoder.lost_frames == 0
    await viewer.aclose()


@pytest.mark.asyncio
async def test_new_viewer_waits_for_previous_transcoder_shutdown() -> None:
    first_backend = _PacketBackend()
    second_backend = _PacketBackend()
    backends = iter((first_backend, second_backend))
    resolve_count = 0
    stop_entered = asyncio.Event()
    stop_release = asyncio.Event()
    created: list[_FakeTranscoder] = []

    async def resolve(_camera_id: str) -> LiveStreamSource:
        nonlocal resolve_count
        resolve_count += 1
        return _source(next(backends), codec="hevc", source_type="rtsp")

    def factory(on_error: Callable[[str], None]) -> _FakeTranscoder:
        transcoder = _FakeTranscoder(
            stop_entered=stop_entered if not created else None,
            stop_release=stop_release if not created else None,
        )
        transcoder.on_error = on_error
        created.append(transcoder)
        return transcoder

    hub = LiveStreamHub(resolve, transcoder_factory=factory)
    first = hub.subscribe("rtsp:camera")
    first_pending = asyncio.create_task(_next(first))
    await asyncio.sleep(0)
    first_backend.emit_frame(1)
    assert await first_pending == b"encoded:\x01"

    closing = asyncio.create_task(first.aclose())
    await asyncio.wait_for(stop_entered.wait(), 1)
    replacement = hub.subscribe("rtsp:camera")
    replacement_pending = asyncio.create_task(_next(replacement))
    await asyncio.sleep(0)

    assert len(created) == 1
    assert resolve_count == 1
    assert not replacement_pending.done()

    stop_release.set()
    await asyncio.wait_for(closing, 1)
    for _ in range(20):
        if len(created) == 2:
            break
        await asyncio.sleep(0)
    assert len(created) == 2
    assert resolve_count == 2
    second_backend.emit_frame(2)
    assert await replacement_pending == b"encoded:\x02"
    await replacement.aclose()


@pytest.mark.asyncio
async def test_one_camera_shutdown_does_not_block_another_camera() -> None:
    first_backend = _PacketBackend()
    other_backend = _PacketBackend()
    stop_entered = asyncio.Event()
    stop_release = asyncio.Event()
    created: list[_FakeTranscoder] = []

    async def resolve(camera_id: str) -> LiveStreamSource:
        backend = first_backend if camera_id == "rtsp:first" else other_backend
        return _source(backend, codec="hevc", source_type="rtsp")

    def factory(on_error: Callable[[str], None]) -> _FakeTranscoder:
        transcoder = _FakeTranscoder(
            stop_entered=stop_entered if not created else None,
            stop_release=stop_release if not created else None,
        )
        transcoder.on_error = on_error
        created.append(transcoder)
        return transcoder

    hub = LiveStreamHub(resolve, transcoder_factory=factory)
    first = hub.subscribe("rtsp:first")
    first_pending = asyncio.create_task(_next(first))
    await asyncio.sleep(0)
    first_backend.emit_frame(1)
    await first_pending
    closing = asyncio.create_task(first.aclose())
    await asyncio.wait_for(stop_entered.wait(), 1)

    other = hub.subscribe("rtsp:other")
    other_pending = asyncio.create_task(_next(other))
    await asyncio.sleep(0)
    other_backend.emit_frame(2)
    assert await other_pending == b"encoded:\x02"
    assert len(created) == 2

    stop_release.set()
    await asyncio.wait_for(closing, 1)
    await other.aclose()


@pytest.mark.asyncio
async def test_cancelled_waiter_and_close_camera_share_one_shutdown() -> None:
    backend = _PacketBackend()
    stop_entered = asyncio.Event()
    stop_release = asyncio.Event()
    created: list[_FakeTranscoder] = []

    async def resolve(_camera_id: str) -> LiveStreamSource:
        return _source(backend, codec="hevc", source_type="rtsp")

    def factory(on_error: Callable[[str], None]) -> _FakeTranscoder:
        transcoder = _FakeTranscoder(
            stop_entered=stop_entered,
            stop_release=stop_release,
        )
        transcoder.on_error = on_error
        created.append(transcoder)
        return transcoder

    hub = LiveStreamHub(resolve, transcoder_factory=factory)
    viewer = hub.subscribe("rtsp:camera")
    initial = asyncio.create_task(_next(viewer))
    await asyncio.sleep(0)
    backend.emit_frame(1)
    await initial
    closing = asyncio.create_task(viewer.aclose())
    await asyncio.wait_for(stop_entered.wait(), 1)

    waiter = hub.subscribe("rtsp:camera")
    waiter_pending = asyncio.create_task(_next(waiter))
    closing_camera = asyncio.create_task(hub.close_camera("rtsp:camera"))
    await asyncio.sleep(0)
    waiter_pending.cancel()
    await asyncio.gather(waiter_pending, return_exceptions=True)
    stop_release.set()

    await asyncio.wait_for(closing, 1)
    await asyncio.wait_for(closing_camera, 1)
    assert len(created) == 1
    assert created[0].stop_count == 1
    assert hub.state("rtsp:camera").mode == "idle"


@pytest.mark.asyncio
@pytest.mark.parametrize("codec", ["hevc", "h264"])
async def test_hevc_and_incompatible_h264_select_shared_transcoder(codec: str) -> None:
    backend = _PacketBackend()
    created: list[_FakeTranscoder] = []

    def factory(on_error: Callable[[str], None]) -> _FakeTranscoder:
        transcoder = _FakeTranscoder()
        transcoder.on_error = on_error
        created.append(transcoder)
        return transcoder

    async def resolve(_camera_id: str) -> LiveStreamSource:
        return _source(backend, codec=codec, source_type="rtsp")

    hub = LiveStreamHub(resolve, transcoder_factory=factory)
    first = hub.subscribe("rtsp:camera")
    second = hub.subscribe("rtsp:camera")
    first_pending = asyncio.create_task(_next(first))
    second_pending = asyncio.create_task(_next(second))
    await asyncio.sleep(0)
    if codec == "h264":
        backend.emit(_packet(1, keyframe=True))
        await asyncio.sleep(0)
    backend.emit_frame(23, 100)

    assert await first_pending == b"encoded:\x17"
    assert await second_pending == b"encoded:\x17"
    assert len(created) == 1
    assert created[0].pushes == 1
    assert hub.state("rtsp:camera").mode == "transcoding"

    await first.aclose()
    assert created[0].stop_count == 0
    await second.aclose()
    assert created[0].stop_count == 1
    assert backend.frame_listeners == []


@pytest.mark.asyncio
async def test_compatible_h264_uses_normalizer_without_transcoder() -> None:
    backend = _PacketBackend()
    created = 0

    def factory(on_error: Callable[[str], None]) -> _FakeTranscoder:
        del on_error
        nonlocal created
        created += 1
        return _FakeTranscoder()

    async def resolve(_camera_id: str) -> LiveStreamSource:
        return _source(backend, source_type="rtsp")

    hub = LiveStreamHub(resolve, transcoder_factory=factory)
    viewer = hub.subscribe("rtsp:camera")
    pending = asyncio.create_task(_next(viewer))
    await asyncio.sleep(0)
    annexb = (
        b"\x00\x00\x00\x01\x67\x42\xc0\x1e\xda\x11\xec\x04\x40\x00\x00\x03"
        b"\x00\x40\x00\x00\x05\x23\xc5\x8b\xa8"
        b"\x00\x00\x00\x01\x68\xce\x0f\xc8"
        b"\x00\x00\x00\x01\x65\x88\x84"
    )
    backend.emit(EncodedVideoPacket("h264", annexb, 0, 0, True, 1, 90_000))

    output = await pending
    assert output.startswith(b"\x00\x00\x00\x01\x67")
    assert created == 0
    assert hub.state("rtsp:camera").mode == "passthrough"
    await viewer.aclose()


@pytest.mark.asyncio
async def test_late_h264_viewer_waits_for_next_idr_without_forcing_transcode() -> None:
    backend = _PacketBackend()
    created = 0

    def factory(on_error: Callable[[str], None]) -> _FakeTranscoder:
        del on_error
        nonlocal created
        created += 1
        return _FakeTranscoder()

    async def resolve(_camera_id: str) -> LiveStreamSource:
        return _source(backend, source_type="rtsp")

    hub = LiveStreamHub(resolve, transcoder_factory=factory)
    first = hub.subscribe("rtsp:camera")
    first_pending = asyncio.create_task(_next(first))
    await asyncio.sleep(0)
    decoder_config = (
        b"\x00\x00\x00\x01\x67\x42\xc0\x1e\xda\x11\xec\x04\x40\x00\x00\x03"
        b"\x00\x40\x00\x00\x05\x23\xc5\x8b\xa8"
        b"\x00\x00\x00\x01\x68\xce\x0f\xc8"
    )
    backend.emit(
        EncodedVideoPacket(
            "h264",
            decoder_config + b"\x00\x00\x00\x01\x65\x88\x84",
            0,
            0,
            True,
            1,
            90_000,
        )
    )
    await first_pending

    late = hub.subscribe("rtsp:camera")
    late_pending = asyncio.create_task(_next(late))
    await asyncio.sleep(0)
    backend.emit(
        EncodedVideoPacket(
            "h264", b"\x00\x00\x00\x01\x41\x9a\x22", 1, 1, False, 1, 90_000
        )
    )
    assert await _next(first) == b"\x00\x00\x00\x01\x41\x9a\x22"
    assert not late_pending.done()
    assert created == 0

    backend.emit(
        EncodedVideoPacket(
            "h264", b"\x00\x00\x00\x01\x65\x88\x84", 2, 2, True, 1, 90_000
        )
    )
    assert (await late_pending).startswith(decoder_config)
    assert created == 0
    await first.aclose()
    await late.aclose()


@pytest.mark.asyncio
async def test_h264_overflow_rearms_decoder_config_before_next_idr() -> None:
    backend = _PacketBackend()

    async def resolve(_camera_id: str) -> LiveStreamSource:
        return _source(backend, source_type="rtsp")

    fixture = (
        Path(__file__).parents[1] / "fixtures" / "rtsp" / "h264_annexb_packets.bin"
    ).read_bytes()
    nals = _annexb_nals(fixture)
    idr = next(nal for nal in nals if nal[0] & 0x1F == 5)
    decoder_config = b"".join(
        b"\x00\x00\x00\x01" + nal for nal in nals if nal[0] & 0x1F in {7, 8}
    )

    hub = LiveStreamHub(resolve, queue_size=2)
    viewer = hub.subscribe("rtsp:camera")
    initial = asyncio.create_task(_next(viewer))
    await asyncio.sleep(0)
    backend.emit(EncodedVideoPacket("h264", fixture, 0, 0, True, 1, 90_000))
    assert (await initial).startswith(decoder_config)

    for pts in (1, 2, 3):
        backend.emit(
            EncodedVideoPacket(
                "h264",
                b"\x00\x00\x00\x01\x41\x9a\x22",
                pts,
                pts,
                False,
                1,
                90_000,
            )
        )
    await asyncio.sleep(0)
    assert hub.state("rtsp:camera").dropped_packets == 3

    recovered = asyncio.create_task(_next(viewer))
    await asyncio.sleep(0)
    backend.emit(
        EncodedVideoPacket(
            "h264",
            b"\x00\x00\x00\x01" + idr,
            4,
            4,
            True,
            1,
            90_000,
        )
    )
    recovered_chunk = await recovered
    assert recovered_chunk.startswith(decoder_config)
    decoder = av.CodecContext.create("h264", "r")
    assert decoder.decode(av.Packet(recovered_chunk))
    assert hub.state("rtsp:camera").dropped_packets == 3
    await viewer.aclose()


@pytest.mark.asyncio
async def test_h264_config_change_overflow_rearms_with_current_config() -> None:
    backend = _PacketBackend()

    async def resolve(_camera_id: str) -> LiveStreamSource:
        return _source(backend, source_type="rtsp")

    baseline = _libx264_keyframe("baseline", 32)
    high = _libx264_keyframe("high", 192)
    baseline_nals = _annexb_nals(baseline)
    high_nals = _annexb_nals(high)
    baseline_prefix = b"".join(
        b"\x00\x00\x00\x01" + nal for nal in baseline_nals if nal[0] & 0x1F in {7, 8}
    )
    high_prefix = b"".join(
        b"\x00\x00\x00\x01" + nal for nal in high_nals if nal[0] & 0x1F in {7, 8}
    )
    bare_high = b"".join(
        b"\x00\x00\x00\x01" + nal for nal in high_nals if nal[0] & 0x1F not in {7, 8}
    )
    assert baseline_prefix != high_prefix

    hub = LiveStreamHub(resolve, queue_size=2)
    viewer = hub.subscribe("rtsp:camera")
    initial = asyncio.create_task(_next(viewer))
    await asyncio.sleep(0)
    backend.emit(EncodedVideoPacket("h264", baseline, 0, 0, True, 1, 90_000))
    assert (await initial).startswith(baseline_prefix)

    for pts in (1, 2):
        backend.emit(
            EncodedVideoPacket(
                "h264",
                b"\x00\x00\x00\x01\x41\x9a\x22",
                pts,
                pts,
                False,
                1,
                90_000,
            )
        )
    backend.emit(EncodedVideoPacket("h264", high, 3, 3, True, 1, 90_000))
    await asyncio.sleep(0)
    assert hub.state("rtsp:camera").dropped_packets == 3

    recovered = asyncio.create_task(_next(viewer))
    await asyncio.sleep(0)
    backend.emit(EncodedVideoPacket("h264", bare_high, 4, 4, True, 1, 90_000))
    recovered_chunk = await recovered
    assert recovered_chunk.startswith(high_prefix)
    assert not recovered_chunk.startswith(baseline_prefix)
    decoder = av.CodecContext.create("h264", "r")
    assert decoder.decode(av.Packet(recovered_chunk))
    await viewer.aclose()


@pytest.mark.asyncio
async def test_malformed_h264_config_does_not_replace_cached_config() -> None:
    backend = _PacketBackend()

    async def resolve(_camera_id: str) -> LiveStreamSource:
        return _source(backend, source_type="rtsp")

    baseline = _libx264_keyframe("baseline", 64)
    baseline_prefix = b"".join(
        b"\x00\x00\x00\x01" + nal
        for nal in _annexb_nals(baseline)
        if nal[0] & 0x1F in {7, 8}
    )
    hub = LiveStreamHub(resolve)
    viewer = hub.subscribe("rtsp:camera")
    initial = asyncio.create_task(_next(viewer))
    await asyncio.sleep(0)
    backend.emit(EncodedVideoPacket("h264", baseline, 0, 0, True, 1, 90_000))
    await initial

    backend.emit(
        EncodedVideoPacket(
            "h264",
            b"\x00\x00\x00\x01\x67\x64\x00\x00\x00\x00\x00\x01\x65\x88\x84",
            1,
            1,
            True,
            1,
            90_000,
        )
    )
    await asyncio.sleep(0)

    assert hub._feeds["rtsp:camera"].h264_decoder_config == baseline_prefix
    await viewer.aclose()


@pytest.mark.asyncio
async def test_transcoder_error_is_safe_and_does_not_break_frame_producer() -> None:
    backend = _PacketBackend()
    perception_values: list[int] = []

    def factory(on_error: Callable[[str], None]) -> _FakeTranscoder:
        transcoder = _FakeTranscoder(fail=True)
        transcoder.on_error = on_error
        return transcoder

    async def resolve(_camera_id: str) -> LiveStreamSource:
        return _source(backend, codec="hevc", source_type="rtsp")

    hub = LiveStreamHub(resolve, transcoder_factory=factory)
    viewer = hub.subscribe("rtsp:camera")
    pending = asyncio.create_task(_next(viewer))
    await asyncio.sleep(0)
    perception_values.append(1)
    backend.emit_frame(1)

    with pytest.raises(StopAsyncIteration):
        await pending
    assert perception_values == [1]
    state = hub.state("rtsp:camera")
    assert state.mode == "error"
    assert state.error_code == "transcode_failed"
    await viewer.aclose()


class _Miot:
    async def list_cameras_with_state(self) -> list[dict]:
        return [
            {
                "did": "miot-camera",
                "channel": 1,
                "channel_count": 2,
                "in_use": True,
            }
        ]


class _RtspRegistry:
    def __init__(self, session: object) -> None:
        self.session = session

    def get_session(self, camera_id: str) -> object | None:
        return self.session if camera_id == "rtsp:camera" else None

    def get_state(self, _camera_id: str):
        return type("State", (), {"video_codec": "h264"})()


class _Perception:
    def __init__(self, registry: _RtspRegistry) -> None:
        self._rtsp_camera_source = registry

    async def sync_camera_sources(self) -> bool:
        return True

    async def retry_camera_source(self, camera_id: str) -> bool:
        del camera_id
        return True


def _settings():
    source = type(
        "Source",
        (),
        {"id": "rtsp:camera", "enabled": True},
    )()
    return type(
        "Settings",
        (),
        {"camera": type("Camera", (), {"rtsp_sources": [source]})()},
    )()


@pytest.mark.asyncio
async def test_camera_service_resolves_existing_backends_without_connecting() -> None:
    rtsp_backend = _PacketBackend()
    miot = _Miot()
    service = CameraService(
        miot,
        _Perception(_RtspRegistry(rtsp_backend)),
        settings_loader=_settings,
    )

    rtsp = await service.resolve_live_stream("rtsp:camera")
    miot_stream = await service.resolve_live_stream("miot-camera:ch1")

    assert rtsp.backend is rtsp_backend
    assert rtsp.source_type == "rtsp"
    assert rtsp.input_codec == "h264"
    assert miot_stream.backend is miot
    assert miot_stream.source_type == "miot"
    assert miot_stream.camera_id == "miot-camera"
    assert miot_stream.channel == 1


@pytest.mark.asyncio
async def test_camera_service_rejects_missing_disabled_or_inactive_sources() -> None:
    rtsp_backend = _PacketBackend()

    def disabled_settings():
        settings = _settings()
        settings.camera.rtsp_sources[0].enabled = False
        return settings

    service = CameraService(
        _Miot(),
        _Perception(_RtspRegistry(rtsp_backend)),
        settings_loader=disabled_settings,
    )
    with pytest.raises(CameraConflictError) as disabled:
        await service.resolve_live_stream("rtsp:camera")
    assert disabled.value.code == "camera_disabled"

    inactive = CameraService(
        _Miot(),
        _Perception(_RtspRegistry(None)),
        settings_loader=_settings,
    )
    with pytest.raises(CameraConflictError) as unavailable:
        await inactive.resolve_live_stream("rtsp:camera")
    assert unavailable.value.code == "camera_unavailable"

    stopped_backend = _PacketBackend()
    stopped_backend.stop()
    stopped = CameraService(
        _Miot(),
        _Perception(_RtspRegistry(stopped_backend)),
        settings_loader=_settings,
    )
    with pytest.raises(CameraConflictError) as stopped_error:
        await stopped.resolve_live_stream("rtsp:camera")
    assert stopped_error.value.code == "camera_unavailable"

    terminal_backend = _PacketBackend()
    terminal_backend.stop("authentication_failed")
    terminal = CameraService(
        _Miot(),
        _Perception(_RtspRegistry(terminal_backend)),
        settings_loader=_settings,
    )
    with pytest.raises(CameraConflictError) as terminal_error:
        await terminal.resolve_live_stream("rtsp:camera")
    assert terminal_error.value.code == "camera_unavailable"
