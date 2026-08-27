"""Bounded fan-out primitives for source-neutral camera live streams."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol

import numpy as np
from numpy.typing import NDArray

from miloco.camera.transcoder import SharedH264Transcoder


@dataclass(frozen=True)
class EncodedVideoPacket:
    codec: Literal["h264", "hevc"]
    data: bytes
    pts: int | None
    dts: int | None
    is_keyframe: bool
    time_base_num: int
    time_base_den: int
    extradata: bytes = b""


@dataclass(frozen=True)
class LiveStreamState:
    viewer_count: int
    mode: Literal["idle", "passthrough", "transcoding", "error"]
    input_codec: str | None
    output_codec: str | None
    queue_depth: int
    dropped_packets: int
    error_code: str | None


class PacketSource(Protocol):
    def add_packet_listener(
        self, listener: Callable[[EncodedVideoPacket], None]
    ) -> Callable[[], None]: ...


class SourceLifecycle(Protocol):
    def add_close_listener(
        self, listener: Callable[[str | None], None]
    ) -> Callable[[], None]: ...


class DecodedVideoSource(Protocol):
    def add_video_frame_listener(
        self,
        listener: Callable[[NDArray[np.uint8], int | None], None],
    ) -> Callable[[], None]: ...


class H264Transcoder(Protocol):
    def attach(self) -> AsyncGenerator[bytes, None]: ...

    async def push_frame(self, frame: NDArray[np.uint8], pts: int | None) -> None: ...

    async def stop(self) -> None: ...


TranscoderFactory = Callable[[Callable[[str], None]], H264Transcoder]


@dataclass(frozen=True)
class LiveStreamSource:
    """An already-owned source backend resolved without connection material."""

    camera_id: str
    source_type: Literal["miot", "rtsp"]
    backend: object
    channel: int
    input_codec: str | None


LiveStreamResolver = Callable[[str], Awaitable[LiveStreamSource]]


@dataclass
class _Subscriber:
    packets: deque[_OutputChunk]
    ready: asyncio.Event
    closed: bool = False
    waiting_for_keyframe: bool = True
    normalizer: object | None = None


@dataclass(frozen=True)
class _OutputChunk:
    data: bytes
    is_keyframe: bool


@dataclass
class _CameraFeed:
    source: LiveStreamSource
    subscribers: dict[int, _Subscriber]
    packet_detach: Callable[[], None] = lambda: None
    lifecycle_detach: Callable[[], None] = lambda: None
    frame_detach: Callable[[], None] = lambda: None
    mode: Literal["passthrough", "transcoding", "error"] = "passthrough"
    transcoder: H264Transcoder | None = None
    transcode_stream: AsyncGenerator[bytes, None] | None = None
    transcode_task: asyncio.Task[None] | None = None
    h264_decoder_config: bytes = b""
    dropped_packets: int = 0


class LiveStreamHub:
    """Fan out one existing encoded source to isolated bounded viewers."""

    def __init__(
        self,
        resolver: LiveStreamResolver,
        *,
        queue_size: int = 8,
        transcoder_factory: TranscoderFactory | None = None,
    ) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self._resolver = resolver
        self._queue_size = queue_size
        self._feeds: dict[str, _CameraFeed] = {}
        self._stopping: dict[str, asyncio.Task[None]] = {}
        self._states: dict[str, LiveStreamState] = {}
        self._lock = asyncio.Lock()
        self._next_subscriber_id = 0
        self._transcoder_factory = transcoder_factory or (
            lambda on_error: SharedH264Transcoder(on_error=on_error)
        )

    async def subscribe(self, camera_id: str) -> AsyncGenerator[bytes, None]:
        subscriber_id, subscriber = await self._attach(camera_id)
        try:
            while True:
                await subscriber.ready.wait()
                subscriber.ready.clear()
                while subscriber.packets:
                    yield subscriber.packets.popleft().data
                if subscriber.closed:
                    return
        finally:
            await self._detach(camera_id, subscriber_id)

    async def close_camera(self, camera_id: str) -> None:
        while True:
            async with self._lock:
                stopping = self._stopping.get(camera_id)
                if stopping is None:
                    feed = self._feeds.pop(camera_id, None)
                    if feed is None:
                        self._states[camera_id] = self._idle_state()
                        return
                    for subscriber in feed.subscribers.values():
                        subscriber.closed = True
                        subscriber.packets.clear()
                        subscriber.ready.set()
                    self._states[camera_id] = self._idle_state(
                        dropped_packets=feed.dropped_packets
                    )
                    stopping = self._start_shutdown_locked(camera_id, feed)
            await asyncio.shield(stopping)

    def state(self, camera_id: str) -> LiveStreamState:
        feed = self._feeds.get(camera_id)
        if feed is None:
            return self._states.get(camera_id, self._idle_state())
        queue_depth = max(
            (len(subscriber.packets) for subscriber in feed.subscribers.values()),
            default=0,
        )
        return LiveStreamState(
            viewer_count=len(feed.subscribers),
            mode=feed.mode,
            input_codec=feed.source.input_codec,
            output_codec=(
                "h264" if feed.mode == "transcoding" else feed.source.input_codec
            ),
            queue_depth=queue_depth,
            dropped_packets=feed.dropped_packets,
            error_code=None,
        )

    async def _attach(self, camera_id: str) -> tuple[int, _Subscriber]:
        while True:
            await self._wait_for_shutdown(camera_id)
            await self._lock.acquire()
            if camera_id in self._stopping:
                self._lock.release()
                continue
            break
        try:
            feed = self._feeds.get(camera_id)
            if feed is None:
                source = await self._resolver(camera_id)
                listener_adder = getattr(source.backend, "add_packet_listener", None)
                if not callable(listener_adder):
                    self._states[camera_id] = LiveStreamState(
                        viewer_count=0,
                        mode="error",
                        input_codec=source.input_codec,
                        output_codec=None,
                        queue_depth=0,
                        dropped_packets=0,
                        error_code="stream_unavailable",
                    )
                    raise RuntimeError("Camera stream backend is unavailable")
                loop = asyncio.get_running_loop()
                feed = _CameraFeed(source, {})
                self._feeds[camera_id] = feed

                def receive(packet: EncodedVideoPacket) -> None:
                    loop.call_soon_threadsafe(self._publish, camera_id, feed, packet)

                def receive_frame(frame: NDArray[np.uint8], pts: int | None) -> None:
                    loop.call_soon_threadsafe(
                        self._schedule_frame, camera_id, feed, frame, pts
                    )

                def source_closed(error_code: str | None) -> None:
                    loop.call_soon_threadsafe(
                        self._source_closed, camera_id, feed, error_code
                    )

                try:
                    feed.packet_detach = listener_adder(receive)
                    frame_adder = getattr(
                        source.backend, "add_video_frame_listener", None
                    )
                    if source.source_type == "rtsp":
                        if not callable(frame_adder):
                            raise RuntimeError("Decoded video fan-out is unavailable")
                        feed.frame_detach = frame_adder(receive_frame)
                    close_adder = getattr(source.backend, "add_close_listener", None)
                    if callable(close_adder):
                        feed.lifecycle_detach = close_adder(source_closed)
                except Exception:
                    if self._feeds.get(camera_id) is feed:
                        self._feeds.pop(camera_id, None)
                    self._detach_source(feed)
                    self._states[camera_id] = LiveStreamState(
                        viewer_count=0,
                        mode="error",
                        input_codec=source.input_codec,
                        output_codec=None,
                        queue_depth=0,
                        dropped_packets=0,
                        error_code="stream_unavailable",
                    )
                    raise

                if source.source_type == "rtsp" and source.input_codec == "hevc":
                    await self._activate_transcoder(camera_id, feed)

            subscriber_id = self._next_subscriber_id
            self._next_subscriber_id += 1
            normalizer: object | None = None
            if feed.source.source_type == "rtsp" and feed.source.input_codec == "h264":
                from miloco.camera.h264 import H264AnnexBNormalizer

                normalizer = H264AnnexBNormalizer()
                if feed.h264_decoder_config:
                    normalizer.push(
                        EncodedVideoPacket(
                            codec="h264",
                            data=feed.h264_decoder_config,
                            pts=None,
                            dts=None,
                            is_keyframe=False,
                            time_base_num=1,
                            time_base_den=1,
                        )
                    )
            subscriber = _Subscriber(deque(), asyncio.Event(), normalizer=normalizer)
            feed.subscribers[subscriber_id] = subscriber
            return subscriber_id, subscriber
        finally:
            self._lock.release()

    async def _detach(self, camera_id: str, subscriber_id: int) -> None:
        stopping: asyncio.Task[None] | None = None
        async with self._lock:
            feed = self._feeds.get(camera_id)
            if feed is None:
                return
            subscriber = feed.subscribers.pop(subscriber_id, None)
            if subscriber is not None:
                subscriber.closed = True
                subscriber.packets.clear()
                subscriber.ready.set()
            if feed.subscribers:
                return
            self._feeds.pop(camera_id, None)
            self._states[camera_id] = self._idle_state(
                dropped_packets=feed.dropped_packets
            )
            stopping = self._start_shutdown_locked(camera_id, feed)
        if stopping is not None:
            await asyncio.shield(stopping)

    def _publish(
        self,
        camera_id: str,
        source_feed: _CameraFeed,
        packet: EncodedVideoPacket,
    ) -> None:
        feed = self._feeds.get(camera_id)
        if feed is not source_feed or not isinstance(packet, EncodedVideoPacket):
            return
        if feed.source.source_type == "rtsp":
            if feed.mode == "transcoding":
                return
            if packet.codec != "h264":
                asyncio.create_task(self._activate_transcoder(camera_id, feed))
                return
            compatibility_checked = False
            compatible = True
            for subscriber in tuple(feed.subscribers.values()):
                normalizer = subscriber.normalizer
                try:
                    inspect = getattr(normalizer, "inspect")
                    compatibility = inspect(packet)
                    compatibility_checked = True
                    if not compatibility.passthrough:
                        compatible = False
                        break
                except Exception:
                    compatible = False
                    break
            if not compatibility_checked or not compatible:
                asyncio.create_task(self._activate_transcoder(camera_id, feed))
                return
            for subscriber in tuple(feed.subscribers.values()):
                try:
                    push = getattr(subscriber.normalizer, "push")
                    output = tuple(push(packet))
                    decoder_config = getattr(subscriber.normalizer, "decoder_config")()
                    for data in output:
                        self._enqueue_chunk(
                            feed,
                            subscriber,
                            _OutputChunk(data, packet.is_keyframe),
                            decoder_config=decoder_config,
                        )
                    if decoder_config:
                        feed.h264_decoder_config = decoder_config
                except Exception:
                    subscriber.closed = True
                    subscriber.packets.clear()
                    subscriber.ready.set()
            return
        for subscriber in tuple(feed.subscribers.values()):
            try:
                self._enqueue_chunk(
                    feed,
                    subscriber,
                    _OutputChunk(packet.data, packet.is_keyframe),
                )
            except Exception:
                # A corrupt subscriber must not interrupt any other viewer.
                subscriber.closed = True
                subscriber.packets.clear()
                subscriber.ready.set()

    def _source_closed(
        self,
        camera_id: str,
        source_feed: _CameraFeed,
        error_code: str | None,
    ) -> None:
        asyncio.create_task(
            self._handle_source_closed(camera_id, source_feed, error_code)
        )

    async def _handle_source_closed(
        self,
        camera_id: str,
        source_feed: _CameraFeed,
        error_code: str | None,
    ) -> None:
        async with self._lock:
            feed = self._feeds.get(camera_id)
            if feed is not source_feed:
                return
            self._feeds.pop(camera_id, None)
            for subscriber in feed.subscribers.values():
                subscriber.closed = True
                subscriber.packets.clear()
                subscriber.ready.set()
            if error_code is None:
                self._states[camera_id] = self._idle_state(
                    dropped_packets=feed.dropped_packets
                )
            else:
                self._states[camera_id] = LiveStreamState(
                    viewer_count=0,
                    mode="error",
                    input_codec=feed.source.input_codec,
                    output_codec=None,
                    queue_depth=0,
                    dropped_packets=feed.dropped_packets,
                    error_code=error_code,
                )
            stopping = self._start_shutdown_locked(camera_id, feed)
        await asyncio.shield(stopping)

    @staticmethod
    def _detach_source(feed: _CameraFeed) -> None:
        packet_detach, feed.packet_detach = feed.packet_detach, lambda: None
        lifecycle_detach, feed.lifecycle_detach = (
            feed.lifecycle_detach,
            lambda: None,
        )
        frame_detach, feed.frame_detach = feed.frame_detach, lambda: None
        for detach in (packet_detach, lifecycle_detach, frame_detach):
            try:
                detach()
            except Exception:
                pass

    def _enqueue_chunk(
        self,
        feed: _CameraFeed,
        subscriber: _Subscriber,
        chunk: _OutputChunk,
        *,
        decoder_config: bytes | None = None,
    ) -> None:
        if subscriber.closed:
            return
        if subscriber.waiting_for_keyframe:
            if not chunk.is_keyframe:
                feed.dropped_packets += 1
                return
            subscriber.waiting_for_keyframe = False

        subscriber.packets.append(chunk)
        if len(subscriber.packets) > self._queue_size:
            subscriber.packets.popleft()
            feed.dropped_packets += 1
            if subscriber.normalizer is not None and feed.mode == "passthrough":
                while subscriber.packets:
                    subscriber.packets.popleft()
                    feed.dropped_packets += 1
                subscriber.waiting_for_keyframe = True
                self._rearm_h264_normalizer(
                    feed, subscriber, decoder_config=decoder_config
                )
                return
            while subscriber.packets and not subscriber.packets[0].is_keyframe:
                subscriber.packets.popleft()
                feed.dropped_packets += 1
            if not subscriber.packets:
                subscriber.waiting_for_keyframe = True
        if subscriber.packets:
            subscriber.ready.set()

    async def _activate_transcoder(
        self, camera_id: str, source_feed: _CameraFeed
    ) -> None:
        feed = self._feeds.get(camera_id)
        if feed is not source_feed or feed.mode == "error":
            return
        if feed.transcoder is not None:
            feed.mode = "transcoding"
            return
        loop = asyncio.get_running_loop()

        def on_error(error_code: str) -> None:
            safe_code = (
                error_code if error_code == "transcode_failed" else "transcode_failed"
            )
            loop.call_soon_threadsafe(
                self._transcoder_failed, camera_id, feed, safe_code
            )

        transcoder = self._transcoder_factory(on_error)
        feed.transcoder = transcoder
        feed.mode = "transcoding"
        stream = transcoder.attach()
        feed.transcode_stream = stream
        feed.transcode_task = asyncio.create_task(
            self._consume_transcoder(camera_id, feed, transcoder, stream)
        )

    def _schedule_frame(
        self,
        camera_id: str,
        source_feed: _CameraFeed,
        frame: NDArray[np.uint8],
        pts: int | None,
    ) -> None:
        feed = self._feeds.get(camera_id)
        if feed is not source_feed or feed.mode != "transcoding":
            return
        transcoder = feed.transcoder
        if transcoder is not None:
            asyncio.create_task(
                self._push_transcode_frame(camera_id, feed, transcoder, frame, pts)
            )

    async def _push_transcode_frame(
        self,
        camera_id: str,
        source_feed: _CameraFeed,
        transcoder: H264Transcoder,
        frame: NDArray[np.uint8],
        pts: int | None,
    ) -> None:
        if (
            self._feeds.get(camera_id) is not source_feed
            or source_feed.transcoder is not transcoder
        ):
            return
        try:
            await transcoder.push_frame(frame, pts)
        except Exception:
            self._transcoder_failed(camera_id, source_feed, "transcode_failed")

    async def _consume_transcoder(
        self,
        camera_id: str,
        source_feed: _CameraFeed,
        transcoder: H264Transcoder,
        stream: AsyncGenerator[bytes, None],
    ) -> None:
        try:
            async for data in stream:
                if (
                    self._feeds.get(camera_id) is not source_feed
                    or source_feed.transcoder is not transcoder
                ):
                    return
                keyframe = self._annexb_contains_idr(data)
                for subscriber in tuple(source_feed.subscribers.values()):
                    self._enqueue_chunk(
                        source_feed, subscriber, _OutputChunk(data, keyframe)
                    )
        except Exception:
            self._transcoder_failed(camera_id, source_feed, "transcode_failed")

    def _transcoder_failed(
        self,
        camera_id: str,
        source_feed: _CameraFeed,
        error_code: str,
    ) -> None:
        asyncio.create_task(
            self._handle_transcoder_failed(camera_id, source_feed, error_code)
        )

    async def _handle_transcoder_failed(
        self,
        camera_id: str,
        source_feed: _CameraFeed,
        error_code: str,
    ) -> None:
        async with self._lock:
            feed = self._feeds.get(camera_id)
            if feed is not source_feed:
                return
            self._feeds.pop(camera_id, None)
            feed.mode = "error"
            for subscriber in feed.subscribers.values():
                subscriber.closed = True
                subscriber.packets.clear()
                subscriber.ready.set()
            self._states[camera_id] = LiveStreamState(
                viewer_count=0,
                mode="error",
                input_codec=feed.source.input_codec,
                output_codec=None,
                queue_depth=0,
                dropped_packets=feed.dropped_packets,
                error_code=(
                    error_code
                    if error_code == "transcode_failed"
                    else "transcode_failed"
                ),
            )
            stopping = self._start_shutdown_locked(camera_id, feed)
        await asyncio.shield(stopping)

    async def _shutdown_feed(self, feed: _CameraFeed) -> None:
        self._detach_source(feed)
        await self._stop_transcoder(feed)

    async def _stop_transcoder(self, feed: _CameraFeed) -> None:
        transcoder, feed.transcoder = feed.transcoder, None
        task, feed.transcode_task = feed.transcode_task, None
        stream, feed.transcode_stream = feed.transcode_stream, None
        if transcoder is not None:
            try:
                await transcoder.stop()
            except Exception:
                pass
        current = asyncio.current_task()
        if task is not None and task is not current:
            await asyncio.gather(task, return_exceptions=True)
        if stream is not None and task is None:
            try:
                await stream.aclose()
            except Exception:
                pass

    async def _wait_for_shutdown(self, camera_id: str) -> None:
        while True:
            async with self._lock:
                stopping = self._stopping.get(camera_id)
            if stopping is None:
                return
            await asyncio.shield(stopping)

    def _start_shutdown_locked(
        self, camera_id: str, feed: _CameraFeed
    ) -> asyncio.Task[None]:
        stopping = self._stopping.get(camera_id)
        if stopping is not None:
            return stopping
        stopping = asyncio.create_task(self._shutdown_registered(camera_id, feed))
        self._stopping[camera_id] = stopping
        return stopping

    async def _shutdown_registered(self, camera_id: str, feed: _CameraFeed) -> None:
        current = asyncio.current_task()
        try:
            await self._shutdown_feed(feed)
        finally:
            async with self._lock:
                if self._stopping.get(camera_id) is current:
                    self._stopping.pop(camera_id, None)

    @staticmethod
    def _rearm_h264_normalizer(
        feed: _CameraFeed,
        subscriber: _Subscriber,
        *,
        decoder_config: bytes | None = None,
    ) -> None:
        from miloco.camera.h264 import H264AnnexBNormalizer

        normalizer = H264AnnexBNormalizer()
        seed = decoder_config or feed.h264_decoder_config
        if seed:
            normalizer.push(
                EncodedVideoPacket(
                    codec="h264",
                    data=seed,
                    pts=None,
                    dts=None,
                    is_keyframe=False,
                    time_base_num=1,
                    time_base_den=1,
                )
            )
        subscriber.normalizer = normalizer

    @staticmethod
    def _annexb_contains_idr(data: bytes) -> bool:
        normalized = data.replace(b"\x00\x00\x01", b"\x00\x00\x00\x01")
        return any(
            nal and nal[0] & 0x1F == 5 for nal in normalized.split(b"\x00\x00\x00\x01")
        )

    @staticmethod
    def _idle_state(*, dropped_packets: int = 0) -> LiveStreamState:
        return LiveStreamState(
            viewer_count=0,
            mode="idle",
            input_codec=None,
            output_codec=None,
            queue_depth=0,
            dropped_packets=dropped_packets,
            error_code=None,
        )
