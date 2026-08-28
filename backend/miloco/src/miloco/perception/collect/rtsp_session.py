"""Bounded decoded-media session for one configured RTSP source."""

from __future__ import annotations

import asyncio
import random
import threading
import time
from collections import deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Literal, Protocol, SupportsBytes, TypeVar, cast

import av
import numpy as np
from numpy.typing import NDArray

from miloco.camera.stream import EncodedVideoPacket
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
# PyAV applies both values to open and subsequent socket reads. This bounds a
# stop that is waiting for the demux owner to leave native code to eight seconds.
_IO_TIMEOUT_SEC = 8.0
_JITTER_BOUND = 0.2
_TaskResult = TypeVar("_TaskResult")


def reconnect_delay(attempt: int, *, jitter: float) -> float:
    bounded_attempt = min(6, max(0, attempt))
    base = min(60.0, float(2**bounded_attempt))
    return max(0.0, base * (1.0 + jitter))


class PacketListener(Protocol):
    """A dormant hook for consumers of packets from this same demux session."""

    def __call__(self, packet: EncodedVideoPacket) -> None: ...


class CloseListener(Protocol):
    """A source lifecycle hook carrying only a stable terminal error code."""

    def __call__(self, error_code: str | None) -> None: ...


class VideoFrameListener(Protocol):
    """A consumer of the same decoded BGR frame used by perception."""

    def __call__(self, frame: NDArray[np.uint8], pts: int | None) -> None: ...


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
        self._terminal = False
        self._task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._stop_async: asyncio.Event | None = None
        self._stop_thread = threading.Event()
        self._connection_had_frame = threading.Event()
        self._active_container: av.container.InputContainer | None = None
        self._open_worker: asyncio.Task[av.container.InputContainer] | None = None
        self._decode_worker: asyncio.Task[bool] | None = None
        self._video_cb: VideoFrameCallback | None = None
        self._audio_cb: AudioFrameCallback | None = None
        self._listeners: tuple[PacketListener, ...] = ()
        self._video_frame_listeners: tuple[VideoFrameListener, ...] = ()
        self._close_listeners: tuple[CloseListener, ...] = ()
        self._close_listener_lock = threading.Lock()
        self._ingress_lock = threading.Lock()
        self._media_ingress: deque[_DecodedEvent] = deque()
        self._packet_ingress: deque[EncodedVideoPacket] = deque()
        self._media_drain_scheduled = False
        self._packet_drain_scheduled = False
        self._dropped_frames = 0
        self._dropped_packets = 0
        self._media_ready: asyncio.Event | None = None
        self._packet_ready: asyncio.Event | None = None
        self._producer_done = True
        self._listener_executor: ThreadPoolExecutor | None = None
        self._listener_future: asyncio.Future[None] | None = None

    async def start(
        self, video_cb: VideoFrameCallback, audio_cb: AudioFrameCallback
    ) -> None:
        async with self._lifecycle_lock:
            existing = self._task
            if existing is not None and not existing.done():
                if not self._stop_thread.is_set():
                    return
                await asyncio.gather(existing, return_exceptions=True)
            elif existing is not None:
                await asyncio.gather(existing, return_exceptions=True)

            self._video_cb = video_cb
            self._audio_cb = audio_cb
            self._media_ready = asyncio.Event()
            self._packet_ready = asyncio.Event()
            self._stop_async = asyncio.Event()
            self._stop_thread.clear()
            self._terminal = False
            with self._close_listener_lock:
                self._producer_done = False
            self._clear_ingress()
            self._listener_executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="miloco-rtsp-listener",
            )
            self._task = asyncio.create_task(self._main())

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            task = self._task
            if task is None:
                self._state = replace(self._state, connected=False)
                self._clear_ingress()
                return

            self._stop_thread.set()
            if self._stop_async is not None:
                self._stop_async.set()
            _, _, cancellation = await self._await_task_uninterruptibly(task)
            if self._task is task:
                self._task = None
            self._clear_ingress()
            if cancellation is not None:
                raise cancellation

    def state(self) -> CameraSourceState:
        with self._ingress_lock:
            dropped_frames = self._dropped_frames
        if self._state.dropped_frames == dropped_frames:
            return self._state
        return replace(self._state, dropped_frames=dropped_frames)

    def is_active(self) -> bool:
        """Return whether the background producer task is still alive."""
        task = self._task
        return task is not None and not task.done()

    def is_terminal(self) -> bool:
        """Return whether the producer stopped on a non-recoverable failure."""
        return self._terminal

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

    def add_close_listener(self, listener: CloseListener) -> Callable[[], None]:
        """Notify once when this active producer stops or becomes terminal."""
        notified = False
        notify_lock = threading.Lock()

        def notify_once(error_code: str | None) -> None:
            nonlocal notified
            with notify_lock:
                if notified:
                    return
                notified = True
            try:
                listener(error_code)
            except Exception:
                pass

        with self._close_listener_lock:
            already_closed = self._producer_done or not self.is_active()
            error_code = self._state.error_code if self._terminal else None
            if not already_closed:
                self._close_listeners = (*self._close_listeners, notify_once)

        def unsubscribe() -> None:
            with self._close_listener_lock:
                self._close_listeners = tuple(
                    registered
                    for registered in self._close_listeners
                    if registered is not notify_once
                )

        if already_closed:
            notify_once(error_code)
        return unsubscribe

    def add_video_frame_listener(
        self, listener: VideoFrameListener
    ) -> Callable[[], None]:
        """Attach to decoded perception frames without opening another source."""
        if listener not in self._video_frame_listeners:
            self._video_frame_listeners = (*self._video_frame_listeners, listener)
        removed = False

        def unsubscribe() -> None:
            nonlocal removed
            if removed:
                return
            removed = True
            self._video_frame_listeners = tuple(
                registered
                for registered in self._video_frame_listeners
                if registered is not listener
            )

        return unsubscribe

    async def _main(self) -> None:
        media_consumer = asyncio.create_task(self._consume_media_ingress())
        packet_consumer = asyncio.create_task(self._consume_packet_ingress())
        try:
            await self._run_reconnect_loop()
        finally:
            self._state = replace(self._state, connected=False)
            self._notify_close_listeners()
            if self._stop_thread.is_set():
                media_consumer.cancel()
                packet_consumer.cancel()
            else:
                assert self._media_ready is not None
                assert self._packet_ready is not None
                self._media_ready.set()
                self._packet_ready.set()
            await asyncio.gather(
                media_consumer,
                packet_consumer,
                return_exceptions=True,
            )
            self._clear_ingress()
            self._shutdown_listener_executor()

    def _notify_close_listeners(self) -> None:
        with self._close_listener_lock:
            self._producer_done = True
            listeners, self._close_listeners = self._close_listeners, ()
            error_code = self._state.error_code if self._terminal else None
        for listener in listeners:
            try:
                listener(error_code)
            except Exception:
                pass

    async def _run_reconnect_loop(self) -> None:
        attempt = 0
        try:
            _validate_uri(self._source.uri)
        except RtspSourceError as error:
            self._terminal = not error.recoverable
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
                saw_frame = await self._decode_until_stopped(
                    container, asyncio.get_running_loop()
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
                raise
            except BaseException as failure:
                saw_frame = self._connection_had_frame.is_set()
                error = _classify_failure(failure, phase="media")
            finally:
                if container is not None and self._active_container is container:
                    self._active_container = None

            if self._stop_thread.is_set():
                return
            assert error is not None
            if saw_frame:
                attempt = 0
            if not error.recoverable:
                self._terminal = True
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
        opener = asyncio.create_task(
            asyncio.to_thread(
                self._open_container_sync,
                self._source,
            )
        )
        self._open_worker = opener
        stopper = asyncio.create_task(self._stop_async.wait())
        try:
            done, _ = await asyncio.wait(
                {opener, stopper}, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError as cancellation:
            stopper.cancel()
            _, _, cancellation = await self._await_task_uninterruptibly(
                stopper,
                pending_cancel=cancellation,
            )
            await self._reap_stopped_opener(
                opener,
                pending_cancel=cancellation,
            )
            raise AssertionError("cancelled opener cleanup must re-raise")
        try:
            if stopper in done:
                await self._reap_stopped_opener(opener)
                return None

            stopper.cancel()
            _, _, cancellation = await self._await_task_uninterruptibly(stopper)
            if cancellation is not None:
                await self._reap_stopped_opener(
                    opener,
                    pending_cancel=cancellation,
                )
                raise AssertionError("cancelled opener cleanup must re-raise")
            return opener.result()
        finally:
            if self._open_worker is opener:
                self._open_worker = None

    async def _reap_stopped_opener(
        self,
        opener: asyncio.Task[av.container.InputContainer],
        *,
        pending_cancel: asyncio.CancelledError | None = None,
    ) -> None:
        container, _failure, cancellation = await self._await_task_uninterruptibly(
            opener,
            pending_cancel=pending_cancel,
        )
        try:
            if container is not None:
                closer = asyncio.create_task(
                    asyncio.to_thread(self._close_container_sync, container)
                )
                (
                    _,
                    _close_failure,
                    cancellation,
                ) = await self._await_task_uninterruptibly(
                    closer,
                    pending_cancel=cancellation,
                )
        finally:
            if self._open_worker is opener:
                self._open_worker = None
        if cancellation is not None:
            raise cancellation

    async def _decode_until_stopped(
        self,
        container: av.container.InputContainer,
        loop: asyncio.AbstractEventLoop,
    ) -> bool:
        worker = asyncio.create_task(
            asyncio.to_thread(self._decode_container_sync, container, loop)
        )
        self._decode_worker = worker
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError as cancellation:
            self._stop_thread.set()
            if self._stop_async is not None:
                self._stop_async.set()
            _, _failure, cancellation = await self._await_task_uninterruptibly(
                worker,
                pending_cancel=cancellation,
            )
            assert cancellation is not None
            raise cancellation
        finally:
            if self._decode_worker is worker:
                self._decode_worker = None

    @staticmethod
    async def _await_task_uninterruptibly(
        task: asyncio.Task[_TaskResult],
        *,
        pending_cancel: asyncio.CancelledError | None = None,
    ) -> tuple[_TaskResult | None, BaseException | None, asyncio.CancelledError | None]:
        """Finish one owned task, deferring caller cancellation until cleanup."""
        cancellation = pending_cancel
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as error:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    cancellation = cancellation or error
                if task.done():
                    break
            except BaseException:
                break
        try:
            return task.result(), None, cancellation
        except BaseException as failure:
            return None, failure, cancellation

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
        try:
            with av.logging.Capture(local=True):
                return self._decode_container_captured_sync(container, loop)
        finally:
            self._close_container_sync(container)

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

            stream_type = getattr(packet.stream, "type", None)
            if self._listeners and stream_type == "video":
                self._enqueue_packet_from_thread(
                    self._snapshot_video_packet(packet), loop
                )
            if stream_type == "audio" and not self._source.audio_enabled:
                continue
            if stream_type not in {"video", "audio"}:
                continue

            recv_unix_ms = int(time.time() * 1000)
            for frame in packet.decode():
                if self._stop_thread.is_set():
                    break
                if stream_type == "video":
                    video_frame = cast(av.VideoFrame, frame)
                    image = video_frame.to_ndarray(format="bgr24").astype(
                        np.uint8, copy=False
                    )
                    decoded_unix_ms = int(time.time() * 1000)
                    event = _DecodedEvent(
                        kind="video",
                        frame=image,
                        stream_ts=self._timestamp_ms(
                            video_frame,
                            fallback_time_base=getattr(
                                packet.stream, "time_base", None
                            ),
                        ),
                        recv_unix_ms=recv_unix_ms,
                        decoded_unix_ms=decoded_unix_ms,
                    )
                    self._enqueue_media_from_thread(event, loop)
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
                        stream_ts=self._timestamp_ms(
                            resampled,
                            fallback_frame=audio_frame,
                            fallback_stream=packet,
                        ),
                        recv_unix_ms=recv_unix_ms,
                        decoded_unix_ms=decoded_unix_ms,
                    )
                    self._enqueue_media_from_thread(event, loop)
                    self._connection_had_frame.set()
                    saw_frame = True
        return saw_frame

    @staticmethod
    def _timestamp_ms(
        frame: object,
        *,
        fallback_frame: object | None = None,
        fallback_time_base: object | None = None,
        fallback_stream: object | None = None,
    ) -> int:
        if fallback_frame is not None:
            for candidate in (frame, fallback_frame, fallback_stream):
                if candidate is None:
                    continue
                candidate_pts = getattr(candidate, "pts", None)
                candidate_time_base = getattr(candidate, "time_base", None)
                if candidate_pts is not None and candidate_time_base is not None:
                    return int(candidate_pts * candidate_time_base * 1000)
            return 0

        pts = getattr(frame, "pts", None)
        time_base = getattr(frame, "time_base", None)
        if time_base is None:
            time_base = fallback_time_base
        if pts is None:
            return 0
        if time_base is None:
            return int(pts)
        return int(pts * time_base * 1000)

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

    def _enqueue_media_from_thread(
        self, event: _DecodedEvent, loop: asyncio.AbstractEventLoop
    ) -> None:
        if self._stop_thread.is_set():
            return
        schedule_drain = False
        with self._ingress_lock:
            if len(self._media_ingress) >= self._queue_size:
                self._media_ingress.popleft()
                self._dropped_frames += 1
            self._media_ingress.append(event)
            if not self._media_drain_scheduled:
                self._media_drain_scheduled = True
                schedule_drain = True
        if schedule_drain:
            loop.call_soon_threadsafe(self._wake_media_consumer)

    def _enqueue_packet_from_thread(
        self, packet: EncodedVideoPacket, loop: asyncio.AbstractEventLoop
    ) -> None:
        if self._stop_thread.is_set():
            return
        schedule_drain = False
        with self._ingress_lock:
            if len(self._packet_ingress) >= self._queue_size:
                self._packet_ingress.popleft()
                self._dropped_packets += 1
            self._packet_ingress.append(packet)
            if not self._packet_drain_scheduled:
                self._packet_drain_scheduled = True
                schedule_drain = True
        if schedule_drain:
            loop.call_soon_threadsafe(self._wake_packet_consumer)

    def _wake_media_consumer(self) -> None:
        if self._media_ready is not None:
            self._media_ready.set()

    def _wake_packet_consumer(self) -> None:
        if self._packet_ready is not None:
            self._packet_ready.set()

    def _mark_frame(self, event: _DecodedEvent) -> None:
        with self._ingress_lock:
            dropped_frames = self._dropped_frames
        self._state = replace(
            self._state,
            connected=True,
            last_frame_unix_ms=event.decoded_unix_ms,
            reconnect_attempt=0,
            dropped_frames=dropped_frames,
            error_code=None,
            error_message=None,
        )

    async def _consume_media_ingress(self) -> None:
        assert self._media_ready is not None
        while True:
            await self._media_ready.wait()
            self._media_ready.clear()
            while True:
                with self._ingress_lock:
                    if self._media_ingress:
                        event = self._media_ingress.popleft()
                    else:
                        self._media_drain_scheduled = False
                        event = None
                if event is None:
                    if self._producer_done:
                        return
                    break
                self._mark_frame(event)
                if event.kind == "video":
                    try:
                        if self._video_cb is not None:
                            await self._video_cb(
                                self._source.id,
                                cast(NDArray[np.uint8], event.frame),
                                event.stream_ts,
                                0,
                                event.recv_unix_ms,
                                event.decoded_unix_ms,
                            )
                    except Exception:
                        pass
                    for listener in self._video_frame_listeners:
                        try:
                            listener(
                                cast(NDArray[np.uint8], event.frame),
                                event.stream_ts,
                            )
                        except Exception:
                            pass
                elif event.kind == "audio" and self._audio_cb is not None:
                    try:
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

    async def _consume_packet_ingress(self) -> None:
        assert self._packet_ready is not None
        while True:
            await self._packet_ready.wait()
            self._packet_ready.clear()
            while True:
                with self._ingress_lock:
                    if self._packet_ingress:
                        packet = self._packet_ingress.popleft()
                    else:
                        self._packet_drain_scheduled = False
                        packet = None
                if packet is None:
                    if self._producer_done:
                        return
                    break
                listeners = self._listeners
                if listeners:
                    await self._run_packet_listeners(packet, listeners)
                if self._stop_thread.is_set():
                    return

    async def _run_packet_listeners(
        self,
        packet: EncodedVideoPacket,
        listeners: tuple[PacketListener, ...],
    ) -> None:
        executor = self._listener_executor
        if executor is None:
            return
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            executor,
            self._notify_packet_listeners_sync,
            packet,
            listeners,
        )
        self._listener_future = future
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(future)
            except Exception:
                pass
            raise
        finally:
            if self._listener_future is future:
                self._listener_future = None

    @staticmethod
    def _notify_packet_listeners_sync(
        packet: EncodedVideoPacket,
        listeners: tuple[PacketListener, ...],
    ) -> None:
        for listener in listeners:
            try:
                listener(packet)
            except Exception:
                pass

    @staticmethod
    def _snapshot_video_packet(packet: object) -> EncodedVideoPacket:
        stream = getattr(packet, "stream", None)
        context = getattr(stream, "codec_context", None)
        codec_name = str(getattr(context, "name", "")).lower()
        if codec_name == "h265":
            codec_name = "hevc"
        if codec_name not in {"h264", "hevc"}:
            raise RtspSourceError(
                "unsupported_video_codec",
                "RTSP video codec could not be decoded",
                recoverable=False,
            )
        time_base = getattr(packet, "time_base", None) or getattr(
            stream, "time_base", None
        )
        numerator = int(getattr(time_base, "numerator", 1))
        denominator = int(getattr(time_base, "denominator", 1))
        if denominator == 0:
            numerator, denominator = 1, 1
        extradata = getattr(context, "extradata", None)
        return EncodedVideoPacket(
            codec=cast(Literal["h264", "hevc"], codec_name),
            data=bytes(cast(SupportsBytes, packet)),
            pts=getattr(packet, "pts", None),
            dts=getattr(packet, "dts", None),
            is_keyframe=bool(getattr(packet, "is_keyframe", False)),
            time_base_num=numerator,
            time_base_den=denominator,
            extradata=bytes(extradata) if extradata else b"",
        )

    def _shutdown_listener_executor(self) -> None:
        executor = self._listener_executor
        self._listener_executor = None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=False)

    def _clear_ingress(self) -> None:
        with self._ingress_lock:
            self._media_ingress.clear()
            self._packet_ingress.clear()
            self._media_drain_scheduled = False
            self._packet_drain_scheduled = False

    def _record_error(self, error: RtspSourceError, *, reconnect_attempt: int) -> None:
        self._state = replace(
            self._state,
            connected=False,
            reconnect_attempt=reconnect_attempt,
            error_code=error.code,
            error_message=error.safe_message,
        )

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
            return _open_container(source, _IO_TIMEOUT_SEC, av.open)
