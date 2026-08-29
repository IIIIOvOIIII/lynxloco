"""Viewer-driven bounded H.264 software transcoding."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncGenerator, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from fractions import Fraction
from typing import cast

import av
import numpy as np
from av.video.frame import PictureType
from numpy.typing import NDArray


@dataclass(frozen=True)
class TranscodeConfig:
    max_width: int = 1280
    max_height: int = 720
    fps: int = 15
    bitrate: int = 2_000_000
    queue_size: int = 2

    def __post_init__(self) -> None:
        if (
            min(
                self.max_width,
                self.max_height,
                self.fps,
                self.bitrate,
                self.queue_size,
            )
            < 1
        ):
            raise ValueError("transcode configuration values must be positive")


@dataclass(frozen=True)
class _FrameInput:
    frame: NDArray[np.uint8]
    pts: int | None


_STOP = object()
_VIEWER_CLOSED = object()
_EMITTED_TIMESTAMP_HISTORY = 256


def _create_libx264_codec() -> av.VideoCodecContext:
    return av.CodecContext.create("libx264", "w")


class SharedH264Transcoder:
    """One bounded encoder shared by all current viewers of one source."""

    def __init__(
        self,
        config: TranscodeConfig | None = None,
        *,
        codec_factory: Callable[[], av.VideoCodecContext] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self._config = config or TranscodeConfig()
        self._codec_factory = codec_factory or _create_libx264_codec
        self._on_error = on_error
        self._lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._input: asyncio.Queue[_FrameInput | object] | None = None
        self._viewers: dict[int, asyncio.Queue[bytes | object]] = {}
        self._worker: asyncio.Task[None] | None = None
        self._executor: ThreadPoolExecutor | None = None
        self._next_viewer_id = 0
        self._generation = 0
        self._active_generation: int | None = None
        self._dropped_frames = 0
        self._force_keyframe = False
        self._error_code: str | None = None
        self._emitted_timestamps: deque[int] = deque(maxlen=_EMITTED_TIMESTAMP_HISTORY)

    def attach(self) -> AsyncGenerator[bytes, None]:
        async def stream() -> AsyncGenerator[bytes, None]:
            viewer_id, queue = await self._attach_viewer()
            try:
                while True:
                    item = await queue.get()
                    if item is _VIEWER_CLOSED:
                        return
                    yield cast(bytes, item)
            finally:
                await self._detach_viewer(viewer_id)

        return stream()

    async def attach_ready(self) -> AsyncGenerator[bytes, None]:
        """Register a viewer before returning its stream to a frame producer."""
        viewer_id, queue = await self._attach_viewer()
        return self._viewer_stream(viewer_id, queue)

    async def detach(self) -> None:
        """Detach all current viewers and synchronously release this generation."""
        await self.stop()

    async def push_frame(self, frame: NDArray[np.uint8], pts: int | None) -> None:
        if frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError("video frame must be a three-channel image")
        async with self._lock:
            queue = self._input
            if self._active_generation is None or queue is None:
                return
            if queue.full():
                try:
                    queue.get_nowait()
                    self._dropped_frames += 1
                except asyncio.QueueEmpty:
                    pass
            image = np.ascontiguousarray(frame, dtype=np.uint8).copy()
            queue.put_nowait(_FrameInput(image, pts))

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            async with self._lock:
                for queue in self._viewers.values():
                    self._close_viewer_queue(queue)
                self._viewers.clear()
                worker = self._begin_stop_locked()
            if worker is not None:
                await asyncio.gather(worker, return_exceptions=True)

    @property
    def viewer_count(self) -> int:
        return len(self._viewers)

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def running(self) -> bool:
        worker = self._worker
        return worker is not None and not worker.done()

    @property
    def queue_depth(self) -> int:
        return self._input.qsize() if self._input is not None else 0

    @property
    def dropped_frames(self) -> int:
        return self._dropped_frames

    @property
    def error_code(self) -> str | None:
        return self._error_code

    @property
    def emitted_timestamps(self) -> tuple[int, ...]:
        return tuple(self._emitted_timestamps)

    async def _attach_viewer(
        self,
    ) -> tuple[int, asyncio.Queue[bytes | object]]:
        async with self._lifecycle_lock:
            async with self._lock:
                if self._active_generation is None:
                    self._start_generation_locked()
                viewer_id = self._next_viewer_id
                self._next_viewer_id += 1
                queue: asyncio.Queue[bytes | object] = asyncio.Queue(maxsize=8)
                self._viewers[viewer_id] = queue
                return viewer_id, queue

    async def _viewer_stream(
        self,
        viewer_id: int,
        queue: asyncio.Queue[bytes | object],
    ) -> AsyncGenerator[bytes, None]:
        try:
            while True:
                item = await queue.get()
                if item is _VIEWER_CLOSED:
                    return
                yield cast(bytes, item)
        finally:
            await self._detach_viewer(viewer_id)

    async def _detach_viewer(self, viewer_id: int) -> None:
        async with self._lifecycle_lock:
            async with self._lock:
                queue = self._viewers.pop(viewer_id, None)
                if queue is not None:
                    self._close_viewer_queue(queue)
                worker = self._begin_stop_locked() if not self._viewers else None
            if worker is not None and worker is not asyncio.current_task():
                await asyncio.gather(worker, return_exceptions=True)

    def _start_generation_locked(self) -> None:
        self._generation += 1
        generation = self._generation
        self._active_generation = generation
        self._error_code = None
        self._force_keyframe = False
        self._input = asyncio.Queue(maxsize=self._config.queue_size)
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="miloco-h264-transcoder",
        )
        self._worker = asyncio.create_task(self._run_generation(generation))

    def _begin_stop_locked(self) -> asyncio.Task[None] | None:
        worker = self._worker
        queue = self._input
        self._active_generation = None
        if queue is not None:
            while not queue.empty():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            queue.put_nowait(_STOP)
        return worker

    async def _run_generation(self, generation: int) -> None:
        codec: av.VideoCodecContext | None = None
        output_width = 0
        output_height = 0
        next_pts = 0
        source_origin: int | None = None
        output_origin = 0
        last_source_pts: int | None = None
        try:
            while True:
                queue = self._input
                if queue is None:
                    return
                item = await queue.get()
                if item is _STOP:
                    break
                frame_input = cast(_FrameInput, item)
                if codec is None:
                    output_width, output_height = self._fit_dimensions(
                        frame_input.frame.shape[1], frame_input.frame.shape[0]
                    )
                    codec = await self._call_worker(
                        self._initialize_codec, output_width, output_height
                    )
                if frame_input.pts is None:
                    candidate_pts = next_pts
                else:
                    if (
                        source_origin is None
                        or last_source_pts is not None
                        and frame_input.pts < last_source_pts
                    ):
                        source_origin = frame_input.pts
                        output_origin = next_pts
                    last_source_pts = frame_input.pts
                    candidate_pts = output_origin + round(
                        (frame_input.pts - source_origin) * self._config.fps / 1000
                    )
                    if candidate_pts < next_pts:
                        self._dropped_frames += 1
                        continue
                encoded_pts = candidate_pts
                next_pts = encoded_pts + 1
                force_keyframe, self._force_keyframe = self._force_keyframe, False
                packets = await self._call_worker(
                    self._encode,
                    codec,
                    frame_input.frame,
                    output_width,
                    output_height,
                    encoded_pts,
                    force_keyframe,
                )
                self._publish(generation, packets)

            if codec is not None:
                packets = await self._call_worker(self._flush, codec)
                self._publish(generation, packets)
        except Exception:
            await self._fail_generation(generation)
        finally:
            executor = self._executor
            if self._active_generation == generation:
                self._active_generation = None
            if self._worker is asyncio.current_task():
                self._worker = None
            self._input = None
            self._executor = None
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=False)

    async def _call_worker(self, operation, *args):
        executor = self._executor
        if executor is None:
            raise RuntimeError("transcoder worker is unavailable")
        return await asyncio.get_running_loop().run_in_executor(
            executor, operation, *args
        )

    def _initialize_codec(self, width: int, height: int) -> av.VideoCodecContext:
        codec = self._codec_factory()
        codec.width = width
        codec.height = height
        codec.pix_fmt = "yuv420p"
        codec.time_base = Fraction(1, self._config.fps)
        codec.framerate = Fraction(self._config.fps, 1)
        codec.bit_rate = self._config.bitrate
        codec.gop_size = self._config.fps
        codec.max_b_frames = 0
        codec.options = {
            "preset": "veryfast",
            "tune": "zerolatency",
            "sc_threshold": "0",
            "keyint_min": str(self._config.fps),
            "repeat-headers": "1",
            "annexb": "1",
        }
        codec.open()
        return codec

    @staticmethod
    def _encode(
        codec: av.VideoCodecContext,
        image: NDArray[np.uint8],
        width: int,
        height: int,
        pts: int,
        force_keyframe: bool,
    ) -> tuple[tuple[bytes, int], ...]:
        frame = av.VideoFrame.from_ndarray(image, format="bgr24")
        frame = frame.reformat(width=width, height=height, format="yuv420p")
        frame.pts = pts
        frame.time_base = codec.time_base
        if force_keyframe:
            frame.pict_type = PictureType.I
        return tuple(
            (bytes(packet), int(packet.pts or pts)) for packet in codec.encode(frame)
        )

    @staticmethod
    def _flush(codec: av.VideoCodecContext) -> tuple[tuple[bytes, int], ...]:
        return tuple(
            (bytes(packet), int(packet.pts or 0)) for packet in codec.encode(None)
        )

    async def _fail_generation(self, generation: int) -> None:
        async with self._lock:
            if self._active_generation != generation:
                return
            self._active_generation = None
            self._error_code = "transcode_failed"
            for queue in self._viewers.values():
                self._close_viewer_queue(queue)
        if self._on_error is not None:
            try:
                self._on_error("transcode_failed")
            except Exception:
                pass

    def _publish(self, generation: int, packets: tuple[tuple[bytes, int], ...]) -> None:
        if self._active_generation != generation:
            return
        for data, pts in packets:
            self._emitted_timestamps.append(pts)
            for queue in tuple(self._viewers.values()):
                if queue.full():
                    try:
                        queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    self._force_keyframe = True
                queue.put_nowait(data)

    def _fit_dimensions(self, width: int, height: int) -> tuple[int, int]:
        if width < 1 or height < 1:
            raise ValueError("video frame dimensions must be positive")
        scale = min(
            1.0,
            self._config.max_width / width,
            self._config.max_height / height,
        )
        output_width = max(2, int(width * scale) // 2 * 2)
        output_height = max(2, int(height * scale) // 2 * 2)
        return output_width, output_height

    @staticmethod
    def _close_viewer_queue(queue: asyncio.Queue[bytes | object]) -> None:
        while not queue.empty():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        queue.put_nowait(_VIEWER_CLOSED)
