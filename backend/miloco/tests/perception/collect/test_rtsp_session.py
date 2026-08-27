from __future__ import annotations

import asyncio
import gc
import importlib
import logging
import threading
import time as stdlib_time
import weakref
from collections.abc import Callable
from fractions import Fraction
from types import ModuleType, SimpleNamespace
from typing import Any

import av
import numpy as np
import pytest
from miloco.config.settings import RtspSourceSettings
from miloco.perception.collect.rtsp_probe import RtspSourceError


def _rtsp_session() -> ModuleType:
    """Load the wished-for module while keeping the initial RED readable."""
    try:
        return importlib.import_module("miloco.perception.collect.rtsp_session")
    except ModuleNotFoundError:
        pytest.fail("RTSP session implementation is missing", pytrace=False)


def _source(**overrides: object) -> RtspSourceSettings:
    values: dict[str, object] = {
        "id": "rtsp:00000000-0000-0000-0000-000000000011",
        "name": "session-camera",
        "uri": "rtsp://camera.example:8554/live",
        "username": "",
        "password": "",
        "transport": "tcp",
        "audio_enabled": True,
        "enabled": True,
    }
    values.update(overrides)
    return RtspSourceSettings.model_validate(values)


class _VideoFrame:
    def __init__(
        self,
        value: int,
        *,
        pts: int | None = None,
        time_base: Fraction | None = None,
    ) -> None:
        self.value = value
        self.pts = value if pts is None else pts
        self.time_base = time_base
        self.width = 4
        self.height = 3
        self.formats: list[str] = []

    def to_ndarray(self, *, format: str) -> np.ndarray:
        self.formats.append(format)
        return np.full((3, 4, 3), self.value, dtype=np.uint8)


class _TrackingVideoFrame(_VideoFrame):
    created: list[weakref.ReferenceType[np.ndarray]] = []

    def to_ndarray(self, *, format: str) -> np.ndarray:
        image = super().to_ndarray(format=format)
        self.created.append(weakref.ref(image))
        return image


def _audio_frame() -> av.AudioFrame:
    pcm = np.arange(160, dtype=np.int16).reshape(1, -1)
    frame = av.AudioFrame.from_ndarray(pcm, format="s16", layout="mono")
    frame.sample_rate = 8000
    frame.pts = 80
    frame.time_base = Fraction(1, 8000)
    return frame


def _stream(kind: str, codec: str) -> object:
    context = SimpleNamespace(name=codec, width=4, height=3, sample_rate=8000)
    return SimpleNamespace(
        type=kind,
        codec_context=context,
        average_rate=Fraction(10, 1) if kind == "video" else None,
        time_base=Fraction(1, 1000),
    )


class _Packet:
    def __init__(self, stream: object, frames: list[object]) -> None:
        self.stream = stream
        self._frames = frames
        self.decode_calls = 0

    def decode(self) -> list[object]:
        self.decode_calls += 1
        return self._frames


class _Container:
    def __init__(
        self,
        packets: list[_Packet],
        *,
        video_stream: object | None = None,
    ) -> None:
        self._packets = packets
        packet_video = [
            packet.stream
            for packet in packets
            if getattr(packet.stream, "type", None) == "video"
        ][:1]
        self.streams = SimpleNamespace(
            video=packet_video or ([video_stream] if video_stream is not None else []),
            audio=[
                packet.stream
                for packet in packets
                if getattr(packet.stream, "type", None) == "audio"
            ][:1],
        )
        self.closed = False
        self.close_calls = 0

    def demux(self) -> list[_Packet]:
        return self._packets

    def close(self) -> None:
        self.closed = True
        self.close_calls += 1


class _BlockingContainer(_Container):
    def __init__(self) -> None:
        super().__init__([], video_stream=_stream("video", "h264"))
        self.entered = threading.Event()
        self.released = threading.Event()
        self.exited = threading.Event()

    def demux(self) -> list[_Packet]:
        self.entered.set()
        self.released.wait(timeout=2.0)
        self.exited.set()
        return []

    def close(self) -> None:
        super().close()
        self.released.set()


class _BurstContainer(_Container):
    def __init__(self, packets: list[_Packet]) -> None:
        super().__init__(packets)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.finished = threading.Event()

    def demux(self):
        self.entered.set()
        self.release.wait(timeout=2.0)
        packets, self._packets = self._packets, []
        try:
            yield from packets
        finally:
            packets.clear()
            self.finished.set()


class _FailAfterPacketsContainer(_Container):
    def demux(self):
        yield from self._packets
        raise ConnectionResetError("raw connection detail")


class _NoisyContainer(_Container):
    def __init__(self, packet: _Packet, sensitive: str) -> None:
        super().__init__([packet])
        self._sensitive = sensitive

    def demux(self):
        av.logging.log(av.logging.ERROR, "rtsp", f"demux {self._sensitive}")
        yield from self._packets

    def close(self) -> None:
        av.logging.log(av.logging.ERROR, "rtsp", f"close {self._sensitive}")
        super().close()


class _SequenceOpener:
    def __init__(self, *results: object) -> None:
        self._results = list(results)
        self.calls = 0

    def __call__(self, *_args: object, **_kwargs: object) -> Any:
        self.calls += 1
        result = self._results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class _BlockingOpenSequence:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.second_started = threading.Event()
        self.late_container = _Container([], video_stream=_stream("video", "h264"))
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def __call__(self, *_args: object, **_kwargs: object) -> object:
        with self._lock:
            self.calls += 1
            call = self.calls
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if call == 1:
                self.started.set()
                self.release.wait(timeout=2.0)
                return self.late_container
            self.second_started.set()
            raise _terminal_error()
        finally:
            with self._lock:
                self.active -= 1


async def _wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    async def _poll() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(_poll(), timeout=timeout)


def _terminal_error() -> RtspSourceError:
    return RtspSourceError(
        "authentication_failed", "RTSP authentication failed", recoverable=False
    )


def test_reconnect_delay_uses_capped_exponential_base() -> None:
    session_module = _rtsp_session()

    assert [session_module.reconnect_delay(i, jitter=0.0) for i in range(8)] == [
        1.0,
        2.0,
        4.0,
        8.0,
        16.0,
        32.0,
        60.0,
        60.0,
    ]


def test_reconnect_delay_applies_deterministic_jitter_without_going_negative() -> None:
    session_module = _rtsp_session()

    assert session_module.reconnect_delay(3, jitter=0.25) == 10.0
    assert session_module.reconnect_delay(3, jitter=-0.25) == 6.0
    assert session_module.reconnect_delay(3, jitter=-2.0) == 0.0


def test_reconnect_delay_clamps_before_exponentiation_for_huge_attempts() -> None:
    session_module = _rtsp_session()

    assert session_module.reconnect_delay(1024, jitter=0.0) == 60.0
    assert session_module.reconnect_delay(100_000, jitter=0.25) == 75.0


@pytest.mark.asyncio
async def test_stop_cancels_backoff_without_waiting_for_delay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_module = _rtsp_session()
    opener = _SequenceOpener(ConnectionResetError("private rtsp://camera/path"))
    monkeypatch.setattr(session_module.av, "open", opener)
    session = session_module.RtspSession(_source())

    await session.start(_unused_video_cb, _unused_audio_cb)
    await _wait_until(lambda: session.state().reconnect_attempt == 1)
    await asyncio.wait_for(session.stop(), timeout=0.2)

    assert session.state().connected is False
    assert session.state().error_code == "connection_reset"
    assert "private" not in (session.state().error_message or "")


@pytest.mark.asyncio
async def test_video_only_decode_runs_callbacks_on_owner_loop_and_tracks_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_module = _rtsp_session()
    video_stream = _stream("video", "h264")
    frame = _VideoFrame(7, pts=123)
    container = _Container([_Packet(video_stream, [frame])])
    opener = _SequenceOpener(container, _terminal_error())
    monkeypatch.setattr(session_module.av, "open", opener)
    monkeypatch.setattr(session_module, "reconnect_delay", lambda *_a, **_kw: 0.0)
    owner_thread = threading.get_ident()
    received: list[tuple[np.ndarray, int, int]] = []
    done = asyncio.Event()

    async def video_cb(
        _did: str,
        image: np.ndarray,
        ts: int,
        _ch: int,
        _recv_ms: int,
        _decoded_ms: int,
    ) -> None:
        received.append((image, ts, threading.get_ident()))
        done.set()

    session = session_module.RtspSession(_source(audio_enabled=False))
    await session.start(video_cb, _unused_audio_cb)
    await asyncio.wait_for(done.wait(), timeout=1.0)
    await _wait_until(lambda: session.state().error_code == "authentication_failed")

    state = session.state()
    assert frame.formats == ["bgr24"]
    assert received[0][0].dtype == np.uint8
    assert received[0][0].shape == (3, 4, 3)
    assert received[0][1:] == (123, owner_thread)
    assert state.video_codec == "h264"
    assert state.audio_codec is None
    assert (state.width, state.height, state.fps) == (4, 3, 10.0)
    assert state.last_frame_unix_ms is not None
    assert state.connected is False
    assert container.closed is True
    await session.stop()


@pytest.mark.asyncio
async def test_audio_is_resampled_to_mono_s16_at_16khz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_module = _rtsp_session()
    video_packet = _Packet(_stream("video", "h264"), [_VideoFrame(1)])
    audio_packet = _Packet(_stream("audio", "aac"), [_audio_frame()])
    container = _Container([video_packet, audio_packet])
    monkeypatch.setattr(
        session_module.av,
        "open",
        _SequenceOpener(container, _terminal_error()),
    )
    monkeypatch.setattr(session_module, "reconnect_delay", lambda *_a, **_kw: 0.0)
    received: list[np.ndarray] = []
    done = asyncio.Event()

    async def audio_cb(
        _did: str,
        pcm: np.ndarray,
        _ts: int,
        _ch: int,
        _recv_ms: int,
        _decoded_ms: int,
    ) -> None:
        received.append(pcm)
        done.set()

    session = session_module.RtspSession(_source())
    await session.start(_unused_video_cb, audio_cb)
    await asyncio.wait_for(done.wait(), timeout=1.0)

    assert audio_packet.decode_calls == 1
    assert received
    assert received[0].dtype == np.int16
    assert received[0].ndim == 1
    assert received[0].size > 160
    assert session.state().audio_codec == "aac"
    await session.stop()


@pytest.mark.asyncio
async def test_audio_disabled_skips_audio_decode_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_module = _rtsp_session()
    audio_packet = _Packet(_stream("audio", "aac"), [_audio_frame()])
    video_packet = _Packet(_stream("video", "h264"), [_VideoFrame(1)])
    container = _Container([audio_packet, video_packet])
    monkeypatch.setattr(
        session_module.av,
        "open",
        _SequenceOpener(container, _terminal_error()),
    )
    monkeypatch.setattr(session_module, "reconnect_delay", lambda *_a, **_kw: 0.0)
    session = session_module.RtspSession(_source(audio_enabled=False))

    await session.start(_unused_video_cb, _unused_audio_cb)
    await _wait_until(lambda: session.state().error_code == "authentication_failed")

    assert audio_packet.decode_calls == 0
    assert session.state().audio_codec is None
    await session.stop()


@pytest.mark.asyncio
async def test_eof_reconnects_closes_container_and_success_resets_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_module = _rtsp_session()
    first = _Container([], video_stream=_stream("video", "h264"))
    second = _Container([_Packet(_stream("video", "hevc"), [_VideoFrame(4)])])
    opener = _SequenceOpener(first, second, _terminal_error())
    monkeypatch.setattr(session_module.av, "open", opener)
    monkeypatch.setattr(session_module, "reconnect_delay", lambda *_a, **_kw: 0.0)
    attempts_seen: list[int] = []
    done = asyncio.Event()
    session: Any

    async def video_cb(*_args: object) -> None:
        attempts_seen.append(session.state().reconnect_attempt)
        done.set()

    session = session_module.RtspSession(_source())
    await session.start(video_cb, _unused_audio_cb)
    await asyncio.wait_for(done.wait(), timeout=1.0)
    await _wait_until(lambda: session.state().error_code == "authentication_failed")

    assert opener.calls == 3
    assert first.closed is True
    assert second.closed is True
    assert attempts_seen == [0]
    await session.stop()


@pytest.mark.asyncio
async def test_frame_before_transport_failure_resets_backoff_sequence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_module = _rtsp_session()
    recovered = _FailAfterPacketsContainer(
        [_Packet(_stream("video", "h264"), [_VideoFrame(4)])]
    )
    opener = _SequenceOpener(
        ConnectionResetError("first failure"),
        recovered,
        _terminal_error(),
    )
    attempts: list[int] = []

    def record_delay(attempt: int, *, jitter: float) -> float:
        attempts.append(attempt)
        return 0.0

    monkeypatch.setattr(session_module.av, "open", opener)
    monkeypatch.setattr(session_module, "reconnect_delay", record_delay)
    session = session_module.RtspSession(_source())

    await session.start(_unused_video_cb, _unused_audio_cb)
    await _wait_until(lambda: session.state().error_code == "authentication_failed")

    assert attempts == [0, 0]
    await session.stop()


@pytest.mark.asyncio
async def test_terminal_source_error_stops_without_reconnect_and_redacts_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_module = _rtsp_session()
    terminal = RtspSourceError(
        "resource_not_found", "RTSP resource was not found", recoverable=False
    )
    opener = _SequenceOpener(terminal)
    monkeypatch.setattr(session_module.av, "open", opener)
    session = session_module.RtspSession(
        _source(password="do-not-store", uri="rtsp://private.example/secret")
    )

    await session.start(_unused_video_cb, _unused_audio_cb)
    await _wait_until(lambda: session.state().error_code == "resource_not_found")
    await asyncio.sleep(0)

    state = session.state()
    assert opener.calls == 1
    assert state.connected is False
    assert state.error_message == "RTSP resource was not found"
    assert "private" not in repr(state)
    assert "secret" not in repr(state)
    assert "do-not-store" not in repr(state)
    await session.stop()


@pytest.mark.asyncio
async def test_queue_size_three_drops_oldest_while_callback_is_slow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_module = _rtsp_session()
    packets = [
        _Packet(_stream("video", "h264"), [_VideoFrame(value)]) for value in range(10)
    ]
    monkeypatch.setattr(
        session_module.av,
        "open",
        _SequenceOpener(_Container(packets), _terminal_error()),
    )
    monkeypatch.setattr(session_module, "reconnect_delay", lambda *_a, **_kw: 0.0)
    release = asyncio.Event()
    received: list[int] = []

    async def slow_video_cb(
        _did: str,
        image: np.ndarray,
        *_rest: object,
    ) -> None:
        received.append(int(image[0, 0, 0]))
        if len(received) == 1:
            await release.wait()

    session = session_module.RtspSession(_source(), queue_size=3)
    await session.start(slow_video_cb, _unused_audio_cb)
    await _wait_until(lambda: session.state().dropped_frames >= 6)
    release.set()
    await _wait_until(lambda: received[-3:] == [7, 8, 9])

    assert session.state().dropped_frames >= 6
    assert received[-3:] == [7, 8, 9]
    await session.stop()


@pytest.mark.asyncio
async def test_thread_ingress_stays_bounded_while_owner_loop_is_frozen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_module = _rtsp_session()
    _TrackingVideoFrame.created = []
    packets = [
        _Packet(_stream("video", "h264"), [_TrackingVideoFrame(value)])
        for value in range(50)
    ]
    packet_refs = [weakref.ref(packet) for packet in packets]
    container = _BurstContainer(packets)
    del packets
    monkeypatch.setattr(
        session_module.av,
        "open",
        _SequenceOpener(container, _terminal_error()),
    )
    monkeypatch.setattr(session_module, "reconnect_delay", lambda *_a, **_kw: 0.0)
    session = session_module.RtspSession(_source(), queue_size=3)
    session.add_packet_listener(lambda _packet: None)
    loop = asyncio.get_running_loop()
    real_call_soon_threadsafe = loop.call_soon_threadsafe
    wake_calls = {"media": 0, "packet": 0}

    def counting_call_soon_threadsafe(callback, *args, **kwargs):
        if callback == session._wake_media_consumer:
            wake_calls["media"] += 1
        elif callback == session._wake_packet_consumer:
            wake_calls["packet"] += 1
        return real_call_soon_threadsafe(callback, *args, **kwargs)

    monkeypatch.setattr(loop, "call_soon_threadsafe", counting_call_soon_threadsafe)

    await session.start(_unused_video_cb, _unused_audio_cb)
    assert await asyncio.to_thread(container.entered.wait, 0.5)
    container.release.set()
    assert container.finished.wait(timeout=0.5)
    gc.collect()

    live_images = sum(
        reference() is not None for reference in _TrackingVideoFrame.created
    )
    live_packets = sum(reference() is not None for reference in packet_refs)
    assert live_images <= 3
    assert live_packets <= 3
    assert session.state().dropped_frames == 47
    assert session._dropped_packets == 47
    assert wake_calls == {"media": 1, "packet": 1}
    await session.stop()


@pytest.mark.asyncio
async def test_slow_packet_listener_never_blocks_owner_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_module = _rtsp_session()
    packet = _Packet(_stream("video", "h264"), [_VideoFrame(1)])
    monkeypatch.setattr(
        session_module.av,
        "open",
        _SequenceOpener(_Container([packet]), _terminal_error()),
    )
    monkeypatch.setattr(session_module, "reconnect_delay", lambda *_a, **_kw: 0.0)
    listener_started = threading.Event()
    listener_release = threading.Event()

    def slow_listener(_packet: object) -> None:
        listener_started.set()
        listener_release.wait(timeout=1.0)

    session = session_module.RtspSession(_source())
    session.add_packet_listener(slow_listener)
    release_timer = threading.Timer(0.4, listener_release.set)
    release_timer.start()
    started_at = stdlib_time.monotonic()
    try:
        await session.start(_unused_video_cb, _unused_audio_cb)
        assert await asyncio.to_thread(listener_started.wait, 0.5)
        elapsed = stdlib_time.monotonic() - started_at
        assert elapsed < 0.2
    finally:
        listener_release.set()
        release_timer.cancel()
        await session.stop()


@pytest.mark.asyncio
async def test_callback_failure_isolated_from_later_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_module = _rtsp_session()
    packets = [
        _Packet(_stream("video", "h264"), [_VideoFrame(1)]),
        _Packet(_stream("video", "h264"), [_VideoFrame(2)]),
    ]
    monkeypatch.setattr(
        session_module.av,
        "open",
        _SequenceOpener(_Container(packets), _terminal_error()),
    )
    monkeypatch.setattr(session_module, "reconnect_delay", lambda *_a, **_kw: 0.0)
    received: list[int] = []
    done = asyncio.Event()

    async def flaky_cb(_did: str, image: np.ndarray, *_rest: object) -> None:
        value = int(image[0, 0, 0])
        received.append(value)
        if value == 1:
            raise RuntimeError("callback private detail")
        done.set()

    session = session_module.RtspSession(_source())
    await session.start(flaky_cb, _unused_audio_cb)
    await asyncio.wait_for(done.wait(), timeout=1.0)

    assert received == [1, 2]
    assert session.state().error_code != "callback private detail"
    await session.stop()


@pytest.mark.asyncio
async def test_stop_is_idempotent_and_closes_active_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_module = _rtsp_session()
    container = _BlockingContainer()
    monkeypatch.setattr(session_module.av, "open", _SequenceOpener(container))
    session = session_module.RtspSession(_source())

    await session.start(_unused_video_cb, _unused_audio_cb)
    assert await asyncio.to_thread(container.entered.wait, 0.5)
    await asyncio.wait_for(session.stop(), timeout=0.5)
    await asyncio.wait_for(session.stop(), timeout=0.5)

    assert container.closed is True
    assert container.close_calls == 1
    assert session.state().connected is False


@pytest.mark.asyncio
async def test_external_session_cancellation_closes_and_joins_decode_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_module = _rtsp_session()
    container = _BlockingContainer()
    monkeypatch.setattr(session_module.av, "open", _SequenceOpener(container))
    session = session_module.RtspSession(_source())

    await session.start(_unused_video_cb, _unused_audio_cb)
    assert await asyncio.to_thread(container.entered.wait, 0.5)
    task = session._task
    assert task is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert container.closed is True
    assert container.exited.is_set()
    assert session.state().connected is False
    await session.stop()
    assert session._task is None


@pytest.mark.asyncio
async def test_stop_blocks_restart_until_late_open_returns_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_module = _rtsp_session()
    opener = _BlockingOpenSequence()
    monkeypatch.setattr(session_module.av, "open", opener)
    session = session_module.RtspSession(_source())

    await session.start(_unused_video_cb, _unused_audio_cb)
    assert await asyncio.to_thread(opener.started.wait, 0.5)
    stopping = asyncio.create_task(session.stop())
    await asyncio.sleep(0)
    restarting = asyncio.create_task(session.start(_unused_video_cb, _unused_audio_cb))
    await asyncio.sleep(0.05)

    assert opener.second_started.is_set() is False
    assert stopping.done() is False
    opener.release.set()
    await asyncio.wait_for(stopping, timeout=0.5)
    await asyncio.wait_for(restarting, timeout=0.5)
    await _wait_until(lambda: opener.second_started.is_set())

    assert opener.late_container.closed is True
    assert opener.max_active == 1
    await session.stop()


@pytest.mark.asyncio
async def test_cancel_then_stop_blocks_restart_until_open_worker_is_reaped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_module = _rtsp_session()
    opener = _BlockingOpenSequence()
    monkeypatch.setattr(session_module.av, "open", opener)
    session = session_module.RtspSession(_source())

    await session.start(_unused_video_cb, _unused_audio_cb)
    assert await asyncio.to_thread(opener.started.wait, 0.5)
    task = session._task
    assert task is not None
    task.cancel()
    stopping = asyncio.create_task(session.stop())
    restarting = asyncio.create_task(session.start(_unused_video_cb, _unused_audio_cb))
    await asyncio.sleep(0.05)

    assert opener.second_started.is_set() is False
    opener.release.set()
    await asyncio.wait_for(stopping, timeout=0.5)
    await asyncio.wait_for(restarting, timeout=0.5)
    await _wait_until(lambda: opener.second_started.is_set())

    assert opener.late_container.closed is True
    assert opener.max_active == 1
    await session.stop()


@pytest.mark.asyncio
async def test_frame_timestamps_are_milliseconds_and_recv_precedes_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_module = _rtsp_session()
    clock_values = iter([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    decode_started_ms: list[int] = []

    class TimingPacket(_Packet):
        def decode(self) -> list[object]:
            decode_started_ms.append(int(session_module.time.time() * 1000))
            return super().decode()

    video = _VideoFrame(1, pts=3, time_base=Fraction(1, 25))
    video_packet = TimingPacket(_stream("video", "h264"), [video])
    audio = _audio_frame()
    audio_packet = _Packet(_stream("audio", "aac"), [audio])
    container = _Container([video_packet, audio_packet])

    class Resampled:
        pts = 1600
        time_base = Fraction(1, 16_000)

        @staticmethod
        def to_ndarray() -> np.ndarray:
            return np.ones((1, 320), dtype=np.int16)

    class Resampler:
        def resample(self, _frame: object) -> list[Resampled]:
            return [Resampled()]

    monkeypatch.setattr(session_module.time, "time", lambda: next(clock_values))
    monkeypatch.setattr(session_module.av, "AudioResampler", lambda **_kw: Resampler())
    monkeypatch.setattr(
        session_module.av,
        "open",
        _SequenceOpener(container, _terminal_error()),
    )
    monkeypatch.setattr(session_module, "reconnect_delay", lambda *_a, **_kw: 0.0)
    video_result: list[tuple[int, int, int]] = []
    audio_result: list[tuple[int, int, int]] = []
    done = asyncio.Event()

    async def video_cb(
        _did: str,
        _frame: np.ndarray,
        ts: int,
        _ch: int,
        recv_ms: int,
        decoded_ms: int,
    ) -> None:
        video_result.append((ts, recv_ms, decoded_ms))
        if audio_result:
            done.set()

    async def audio_cb(
        _did: str,
        _frame: np.ndarray,
        ts: int,
        _ch: int,
        recv_ms: int,
        decoded_ms: int,
    ) -> None:
        audio_result.append((ts, recv_ms, decoded_ms))
        if video_result:
            done.set()

    session = session_module.RtspSession(_source())
    await session.start(video_cb, audio_cb)
    await asyncio.wait_for(done.wait(), timeout=1.0)

    assert video_result == [(120, 1000, 3000)]
    assert decode_started_ms == [2000]
    assert audio_result == [(100, 4000, 5000)]
    assert video_result[0][1] < decode_started_ms[0] < video_result[0][2]
    await session.stop()


@pytest.mark.asyncio
async def test_stop_releases_all_queued_ndarrays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_module = _rtsp_session()
    _TrackingVideoFrame.created = []
    packets = [
        _Packet(_stream("video", "h264"), [_TrackingVideoFrame(value)])
        for value in range(20)
    ]
    callback_started = asyncio.Event()
    callback_release = asyncio.Event()

    async def blocked_callback(*_args: object) -> None:
        callback_started.set()
        await callback_release.wait()

    monkeypatch.setattr(
        session_module.av,
        "open",
        _SequenceOpener(_Container(packets), _terminal_error()),
    )
    monkeypatch.setattr(session_module, "reconnect_delay", lambda *_a, **_kw: 0.0)
    session = session_module.RtspSession(_source(), queue_size=3)
    await session.start(blocked_callback, _unused_audio_cb)
    await asyncio.wait_for(callback_started.wait(), timeout=0.5)
    await session.stop()
    packets.clear()
    gc.collect()

    assert all(reference() is None for reference in _TrackingVideoFrame.created)


@pytest.mark.asyncio
async def test_session_locally_captures_libav_logs_with_connection_material(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session_module = _rtsp_session()
    username = "capture-user"
    password = "capture-password"
    sensitive = f"rtsp://{username}:{password}@private.example/secret"
    packet = _Packet(_stream("video", "h264"), [_VideoFrame(1)])
    container = _NoisyContainer(packet, sensitive)
    open_calls = 0

    def noisy_open(url: str, **_kwargs: object) -> _NoisyContainer:
        nonlocal open_calls
        open_calls += 1
        if open_calls > 1:
            raise _terminal_error()
        av.logging.log(av.logging.ERROR, "rtsp", f"open {url}")
        return container

    monkeypatch.setattr(session_module.av, "open", noisy_open)
    monkeypatch.setattr(session_module, "reconnect_delay", lambda *_a, **_kw: 0.0)
    session = session_module.RtspSession(_source(username=username, password=password))

    previous_level = av.logging.get_level()
    av.logging.set_level(av.logging.DEBUG)
    try:
        with caplog.at_level(logging.DEBUG, logger="libav.rtsp"):
            await session.start(_unused_video_cb, _unused_audio_cb)
            await _wait_until(lambda: container.closed)
            await session.stop()
    finally:
        av.logging.set_level(previous_level)

    assert username not in caplog.text
    assert password not in caplog.text
    assert sensitive not in caplog.text


@pytest.mark.asyncio
async def test_packet_listener_is_dormant_unsubscribable_and_failure_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_module = _rtsp_session()
    packet = _Packet(_stream("video", "h264"), [_VideoFrame(5)])
    opener = _SequenceOpener(_Container([packet]), _terminal_error())
    monkeypatch.setattr(session_module.av, "open", opener)
    monkeypatch.setattr(session_module, "reconnect_delay", lambda *_a, **_kw: 0.0)
    session = session_module.RtspSession(_source())
    seen: list[object] = []
    video_done = asyncio.Event()

    def broken_listener(received: object) -> None:
        seen.append(received)
        raise RuntimeError("listener private detail")

    unsubscribe = session.add_packet_listener(broken_listener)
    unsubscribe()
    unsubscribe()
    assert opener.calls == 0
    active_unsubscribe = session.add_packet_listener(broken_listener)

    async def video_cb(*_args: object) -> None:
        video_done.set()

    await session.start(video_cb, _unused_audio_cb)
    await asyncio.wait_for(video_done.wait(), timeout=1.0)
    await _wait_until(lambda: session.state().error_code == "authentication_failed")
    active_unsubscribe()

    assert seen == [packet]
    assert opener.calls == 2
    await session.stop()


async def _unused_video_cb(*_args: object) -> None:
    return None


async def _unused_audio_cb(*_args: object) -> None:
    return None
