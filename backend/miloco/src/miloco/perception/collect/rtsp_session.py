"""Bounded decoded-media session for one configured RTSP source."""

from __future__ import annotations

import asyncio
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal, Protocol, cast

import av
import numpy as np
from numpy.typing import NDArray

from miloco.config.settings import RtspSourceSettings
from miloco.perception.collect.camera_source import (
    AudioFrameCallback,
    CameraSourceState,
    VideoFrameCallback,
)
from miloco.perception.collect.rtsp_probe import (
    RtspSourceError,
    _classify_failure,
    _open_container,
    _validate_uri,
)

_AUDIO_SAMPLE_RATE = 16_000
_OPEN_TIMEOUT_SEC = 8.0
_JITTER_BOUND = 0.2


def reconnect_delay(attempt: int, *, jitter: float) -> float:
    base = min(60.0, float(2 ** max(0, attempt)))
    return max(0.0, base * (1.0 + jitter))


class PacketListener(Protocol):
    """A dormant hook for consumers of packets from this same demux session."""

    def __call__(self, packet: object) -> None: ...


@dataclass(frozen=True)
class _DecodedEvent:
    kind: Literal["video", "audio"]
    frame: NDArray[np.uint8] | NDArray[np.int16]
    stream_ts: int
    recv_unix_ms: int
    decoded_unix_ms: int


class RtspSession:
    """Own one RTSP connection, reconnect loop, and bounded callback queue."""

    def __init__(self, source: RtspSourceSettings, *, queue_size: int = 3) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self._source = source
        self._queue_size = queue_size
        self._state = CameraSourceState(connected=False)
        self._task: asyncio.Task[None] | None = None
        self._events: asyncio.Queue[_DecodedEvent | None] | None = None
        self._stop_async: asyncio.Event | None = None
        self._stop_thread = threading.Event()
        self._connection_had_frame = threading.Event()
        self._active_container: av.container.InputContainer | None = None
        self._video_cb: VideoFrameCallback | None = None
        self._audio_cb: AudioFrameCallback | None = None
        self._listeners: tuple[PacketListener, ...] = ()

    async def start(
        self, video_cb: VideoFrameCallback, audio_cb: AudioFrameCallback
    ) -> None:
        if self._task is not None and not self._task.done():
            return
        if self._task is not None:
            await self._task

        self._video_cb = video_cb
        self._audio_cb = audio_cb
        self._events = asyncio.Queue(maxsize=self._queue_size)
        self._stop_async = asyncio.Event()
        self._stop_thread.clear()
        self._task = asyncio.create_task(self._main())

    async def stop(self) -> None:
        task = self._task
        if task is None:
            self._state = replace(self._state, connected=False)
            return

        self._stop_thread.set()
        if self._stop_async is not None:
            self._stop_async.set()
        await self._close_active_container()
        await task
        if self._task is task:
            self._task = None

    def state(self) -> CameraSourceState:
        return self._state

    def add_packet_listener(self, listener: PacketListener) -> Callable[[], None]:
        if listener not in self._listeners:
            self._listeners = (*self._listeners, listener)
        removed = False

        def _unsubscribe() -> None:
            nonlocal removed
            if removed:
                return
            removed = True
            self._listeners = tuple(
                registered
                for registered in self._listeners
                if registered is not listener
            )

        return _unsubscribe

    async def _main(self) -> None:
        assert self._events is not None
        consumer = asyncio.create_task(self._consume_events())
        try:
            await self._run_reconnect_loop()
        finally:
            self._state = replace(self._state, connected=False)
            if self._stop_thread.is_set():
                consumer.cancel()
                await asyncio.gather(consumer, return_exceptions=True)
            else:
                await self._events.put(None)
                await consumer

    async def _run_reconnect_loop(self) -> None:
        attempt = 0
        try:
            _validate_uri(self._source.uri)
        except RtspSourceError as error:
            self._record_error(error, reconnect_attempt=0)
            return

        while not self._stop_thread.is_set():
            container: av.container.InputContainer | None = None
            saw_frame = False
            error: RtspSourceError | None = None
            try:
                container = await self._open_until_stopped()
                if container is None:
                    return
                self._active_container = container
                self._connection_had_frame.clear()
                saw_frame = await asyncio.to_thread(
                    self._decode_container_sync,
                    container,
                    asyncio.get_running_loop(),
                )
                if self._stop_thread.is_set():
                    return
                error = RtspSourceError(
                    "end_of_stream",
                    "RTSP stream ended unexpectedly",
                    recoverable=True,
                )
            except asyncio.CancelledError:
                self._stop_thread.set()
                if self._stop_async is not None:
                    self._stop_async.set()
                await self._close_active_container()
                raise
            except BaseException as failure:
                saw_frame = self._connection_had_frame.is_set()
                error = _classify_failure(failure, phase="media")
            finally:
                if container is not None and self._active_container is container:
                    self._active_container = None
                    await asyncio.to_thread(self._close_container_sync, container)

            if self._stop_thread.is_set():
                return
            assert error is not None
            if saw_frame:
                attempt = 0
            if not error.recoverable:
                self._record_error(error, reconnect_attempt=attempt)
                return

            self._record_error(error, reconnect_attempt=attempt + 1)
            delay = reconnect_delay(
                attempt,
                jitter=random.uniform(-_JITTER_BOUND, _JITTER_BOUND),
            )
            attempt += 1
            if await self._wait_for_stop(delay):
                return

    async def _open_until_stopped(self) -> av.container.InputContainer | None:
        assert self._stop_async is not None
        loop = asyncio.get_running_loop()
        opener = asyncio.create_task(
            asyncio.to_thread(
                self._open_container_sync,
                self._source,
            )
        )
        stopper = asyncio.create_task(self._stop_async.wait())
        try:
            done, _ = await asyncio.wait(
                {opener, stopper}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:
            self._close_late_open(opener, loop)
            stopper.cancel()
            await asyncio.gather(stopper, return_exceptions=True)
            raise

        if stopper in done:
            self._close_late_open(opener, loop)
            return None

        stopper.cancel()
        await asyncio.gather(stopper, return_exceptions=True)
        return await opener

    def _close_late_open(
        self,
        opener: asyncio.Task[av.container.InputContainer],
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        def _close_when_ready(
            done: asyncio.Task[av.container.InputContainer],
        ) -> None:
            try:
                container = done.result()
            except BaseException:
                return
            loop.create_task(asyncio.to_thread(self._close_container_sync, container))

        opener.add_done_callback(_close_when_ready)

    async def _wait_for_stop(self, delay: float) -> bool:
        assert self._stop_async is not None
        if self._stop_async.is_set():
            return True
        try:
            await asyncio.wait_for(self._stop_async.wait(), timeout=delay)
        except TimeoutError:
            return False
        return True

    def _decode_container_sync(
        self,
        container: av.container.InputContainer,
        loop: asyncio.AbstractEventLoop,
    ) -> bool:
        with av.logging.Capture(local=True):
            return self._decode_container_captured_sync(container, loop)

    def _decode_container_captured_sync(
        self,
        container: av.container.InputContainer,
        loop: asyncio.AbstractEventLoop,
    ) -> bool:
        video_streams = container.streams.video
        if not video_streams:
            raise RtspSourceError(
                "no_video_stream", "RTSP source has no video stream", recoverable=False
            )

        video_stream = video_streams[0]
        video_context = video_stream.codec_context
        video_codec = str(video_context.name).lower()
        if video_codec == "h265":
            video_codec = "hevc"
        audio_streams = container.streams.audio if self._source.audio_enabled else []
        audio_codec = (
            str(audio_streams[0].codec_context.name) if audio_streams else None
        )
        average_rate = video_stream.average_rate
        fps = float(average_rate) if average_rate is not None else 0.0
        loop.call_soon_threadsafe(
            self._mark_connected,
            video_codec,
            audio_codec,
            int(video_context.width),
            int(video_context.height),
            fps,
        )

        resampler = (
            av.AudioResampler(format="s16", layout="mono", rate=_AUDIO_SAMPLE_RATE)
            if audio_streams
            else None
        )
        saw_frame = False
        for packet in container.demux():
            if self._stop_thread.is_set():
                break
            if self._listeners:
                loop.call_soon_threadsafe(self._notify_packet_listeners, packet)

            stream_type = getattr(packet.stream, "type", None)
            if stream_type == "audio" and not self._source.audio_enabled:
                continue
            if stream_type not in {"video", "audio"}:
                continue

            for frame in packet.decode():
                if self._stop_thread.is_set():
                    break
                recv_unix_ms = int(time.time() * 1000)
                if stream_type == "video":
                    video_frame = cast(av.VideoFrame, frame)
                    image = video_frame.to_ndarray(format="bgr24").astype(
                        np.uint8, copy=False
                    )
                    decoded_unix_ms = int(time.time() * 1000)
                    event = _DecodedEvent(
                        kind="video",
                        frame=image,
                        stream_ts=int(video_frame.pts or 0),
                        recv_unix_ms=recv_unix_ms,
                        decoded_unix_ms=decoded_unix_ms,
                    )
                    loop.call_soon_threadsafe(self._enqueue_event, event)
                    self._connection_had_frame.set()
                    saw_frame = True
                    continue

                assert resampler is not None
                audio_frame = cast(av.AudioFrame, frame)
                for resampled in resampler.resample(audio_frame):
                    pcm = (
                        resampled.to_ndarray().reshape(-1).astype(np.int16, copy=False)
                    )
                    decoded_unix_ms = int(time.time() * 1000)
                    event = _DecodedEvent(
                        kind="audio",
                        frame=pcm,
                        stream_ts=int(audio_frame.pts or 0),
                        recv_unix_ms=recv_unix_ms,
                        decoded_unix_ms=decoded_unix_ms,
                    )
                    loop.call_soon_threadsafe(self._enqueue_event, event)
                    self._connection_had_frame.set()
                    saw_frame = True
        return saw_frame

    def _mark_connected(
        self,
        video_codec: str,
        audio_codec: str | None,
        width: int,
        height: int,
        fps: float,
    ) -> None:
        self._state = replace(
            self._state,
            connected=True,
            video_codec=video_codec,
            audio_codec=audio_codec,
            width=width,
            height=height,
            fps=fps,
            error_code=None,
            error_message=None,
        )

    def _enqueue_event(self, event: _DecodedEvent) -> None:
        if self._stop_thread.is_set() or self._events is None:
            return
        dropped = self._state.dropped_frames
        if self._events.full():
            try:
                self._events.get_nowait()
                self._events.task_done()
                dropped += 1
            except asyncio.QueueEmpty:
                pass
        self._events.put_nowait(event)
        self._state = replace(
            self._state,
            connected=True,
            last_frame_unix_ms=event.decoded_unix_ms,
            reconnect_attempt=0,
            dropped_frames=dropped,
            error_code=None,
            error_message=None,
        )

    async def _consume_events(self) -> None:
        assert self._events is not None
        while True:
            event = await self._events.get()
            try:
                if event is None:
                    return
                try:
                    if event.kind == "video" and self._video_cb is not None:
                        await self._video_cb(
                            self._source.id,
                            cast(NDArray[np.uint8], event.frame),
                            event.stream_ts,
                            0,
                            event.recv_unix_ms,
                            event.decoded_unix_ms,
                        )
                    elif event.kind == "audio" and self._audio_cb is not None:
                        await self._audio_cb(
                            self._source.id,
                            cast(NDArray[np.int16], event.frame),
                            event.stream_ts,
                            0,
                            event.recv_unix_ms,
                            event.decoded_unix_ms,
                        )
                except Exception:
                    pass
            finally:
                self._events.task_done()

    def _notify_packet_listeners(self, packet: object) -> None:
        for listener in self._listeners:
            try:
                listener(packet)
            except Exception:
                pass

    def _record_error(self, error: RtspSourceError, *, reconnect_attempt: int) -> None:
        self._state = replace(
            self._state,
            connected=False,
            reconnect_attempt=reconnect_attempt,
            error_code=error.code,
            error_message=error.safe_message,
        )

    async def _close_active_container(self) -> None:
        container = self._active_container
        if container is None:
            return
        self._active_container = None
        await asyncio.to_thread(self._close_container_sync, container)

    @staticmethod
    def _close_container_sync(container: av.container.InputContainer) -> None:
        with av.logging.Capture(local=True):
            try:
                container.close()
            except Exception:
                pass

    @staticmethod
    def _open_container_sync(
        source: RtspSourceSettings,
    ) -> av.container.InputContainer:
        with av.logging.Capture(local=True):
            return _open_container(source, _OPEN_TIMEOUT_SEC, av.open)
