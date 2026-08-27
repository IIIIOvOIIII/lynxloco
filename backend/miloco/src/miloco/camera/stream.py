"""Bounded fan-out primitives for source-neutral camera live streams."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol


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
    packets: deque[EncodedVideoPacket]
    ready: asyncio.Event
    closed: bool = False
    waiting_for_keyframe: bool = True


@dataclass
class _CameraFeed:
    source: LiveStreamSource
    subscribers: dict[int, _Subscriber]
    packet_detach: Callable[[], None] = lambda: None
    lifecycle_detach: Callable[[], None] = lambda: None
    dropped_packets: int = 0


class LiveStreamHub:
    """Fan out one existing encoded source to isolated bounded viewers."""

    def __init__(self, resolver: LiveStreamResolver, *, queue_size: int = 8) -> None:
        if queue_size < 1:
            raise ValueError("queue_size must be positive")
        self._resolver = resolver
        self._queue_size = queue_size
        self._feeds: dict[str, _CameraFeed] = {}
        self._states: dict[str, LiveStreamState] = {}
        self._lock = asyncio.Lock()
        self._next_subscriber_id = 0

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
        async with self._lock:
            feed = self._feeds.pop(camera_id, None)
            if feed is None:
                self._states[camera_id] = self._idle_state()
                return
            self._detach_source(feed)
            for subscriber in feed.subscribers.values():
                subscriber.closed = True
                subscriber.packets.clear()
                subscriber.ready.set()
            self._states[camera_id] = self._idle_state(
                dropped_packets=feed.dropped_packets
            )

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
            mode="passthrough",
            input_codec=feed.source.input_codec,
            output_codec=feed.source.input_codec,
            queue_depth=queue_depth,
            dropped_packets=feed.dropped_packets,
            error_code=None,
        )

    async def _attach(self, camera_id: str) -> tuple[int, _Subscriber]:
        async with self._lock:
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

                def source_closed(error_code: str | None) -> None:
                    loop.call_soon_threadsafe(
                        self._source_closed, camera_id, feed, error_code
                    )

                try:
                    feed.packet_detach = listener_adder(receive)
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

            subscriber_id = self._next_subscriber_id
            self._next_subscriber_id += 1
            subscriber = _Subscriber(deque(), asyncio.Event())
            feed.subscribers[subscriber_id] = subscriber
            return subscriber_id, subscriber

    async def _detach(self, camera_id: str, subscriber_id: int) -> None:
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
            self._detach_source(feed)
            self._states[camera_id] = self._idle_state(
                dropped_packets=feed.dropped_packets
            )

    def _publish(
        self,
        camera_id: str,
        source_feed: _CameraFeed,
        packet: EncodedVideoPacket,
    ) -> None:
        feed = self._feeds.get(camera_id)
        if feed is not source_feed or not isinstance(packet, EncodedVideoPacket):
            return
        for subscriber in tuple(feed.subscribers.values()):
            try:
                self._enqueue(feed, subscriber, packet)
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
        feed = self._feeds.get(camera_id)
        if feed is not source_feed:
            return
        self._feeds.pop(camera_id, None)
        self._detach_source(feed)
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

    @staticmethod
    def _detach_source(feed: _CameraFeed) -> None:
        packet_detach, feed.packet_detach = feed.packet_detach, lambda: None
        lifecycle_detach, feed.lifecycle_detach = (
            feed.lifecycle_detach,
            lambda: None,
        )
        for detach in (packet_detach, lifecycle_detach):
            try:
                detach()
            except Exception:
                pass

    def _enqueue(
        self,
        feed: _CameraFeed,
        subscriber: _Subscriber,
        packet: EncodedVideoPacket,
    ) -> None:
        if subscriber.closed:
            return
        if subscriber.waiting_for_keyframe:
            if not packet.is_keyframe:
                feed.dropped_packets += 1
                return
            subscriber.waiting_for_keyframe = False

        subscriber.packets.append(packet)
        if len(subscriber.packets) > self._queue_size:
            subscriber.packets.popleft()
            feed.dropped_packets += 1
            while subscriber.packets and not subscriber.packets[0].is_keyframe:
                subscriber.packets.popleft()
                feed.dropped_packets += 1
            if not subscriber.packets:
                subscriber.waiting_for_keyframe = True
        if subscriber.packets:
            subscriber.ready.set()

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
