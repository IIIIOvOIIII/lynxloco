from __future__ import annotations

import asyncio
import threading
import weakref
from collections.abc import AsyncGenerator

import av
import miloco.camera.transcoder as transcoder_module
import numpy as np
import pytest
from miloco.camera.transcoder import SharedH264Transcoder, TranscodeConfig


async def _start_viewer(
    transcoder: SharedH264Transcoder,
) -> tuple[AsyncGenerator[bytes, None], asyncio.Task[bytes]]:
    stream = transcoder.attach()
    pending = asyncio.create_task(anext(stream))
    await asyncio.sleep(0)
    return stream, pending


def _frame(width: int = 96, height: int = 64, value: int = 64) -> np.ndarray:
    return np.full((height, width, 3), value, dtype=np.uint8)


def _contains_idr(chunk: bytes) -> bool:
    normalized = chunk.replace(b"\x00\x00\x01", b"\x00\x00\x00\x01")
    return any(
        nal and nal[0] & 0x1F == 5 for nal in normalized.split(b"\x00\x00\x00\x01")
    )


def _blocking_codec_factory(entered: threading.Event, release: threading.Event):
    def create() -> av.VideoCodecContext:
        entered.set()
        assert release.wait(timeout=2)
        return av.CodecContext.create("libx264", "w")

    return create


@pytest.mark.asyncio
async def test_first_viewer_starts_and_second_reuses_one_encoder() -> None:
    transcoder = SharedH264Transcoder(TranscodeConfig(fps=10))

    first, first_pending = await _start_viewer(transcoder)
    second, second_pending = await _start_viewer(transcoder)
    assert transcoder.viewer_count == 2
    assert transcoder.generation == 1

    await transcoder.push_frame(_frame(), 100)
    assert await asyncio.wait_for(first_pending, 2) != b""
    assert await asyncio.wait_for(second_pending, 2) != b""
    assert transcoder.generation == 1

    await first.aclose()
    assert transcoder.viewer_count == 1
    await second.aclose()
    assert transcoder.viewer_count == 0
    assert transcoder.queue_depth == 0


@pytest.mark.asyncio
async def test_last_detach_stops_and_later_attach_starts_new_generation() -> None:
    transcoder = SharedH264Transcoder()
    first, first_pending = await _start_viewer(transcoder)
    await transcoder.push_frame(_frame(), 1)
    await asyncio.wait_for(first_pending, 2)
    await first.aclose()

    assert transcoder.viewer_count == 0
    assert not transcoder.running
    first_generation = transcoder.generation

    replacement, replacement_pending = await _start_viewer(transcoder)
    assert transcoder.generation == first_generation + 1
    await transcoder.push_frame(_frame(value=128), 2)
    assert await asyncio.wait_for(replacement_pending, 2)
    await replacement.aclose()


@pytest.mark.asyncio
async def test_input_queue_is_bounded_and_drops_stale_frames() -> None:
    entered = threading.Event()
    release = threading.Event()
    transcoder = SharedH264Transcoder(
        TranscodeConfig(queue_size=2),
        codec_factory=_blocking_codec_factory(entered, release),
    )
    viewer, pending = await _start_viewer(transcoder)

    await transcoder.push_frame(_frame(value=1), 1)
    assert await asyncio.to_thread(entered.wait, 1)
    for value in range(2, 8):
        await transcoder.push_frame(_frame(value=value), value)

    assert transcoder.queue_depth == 2
    assert transcoder.dropped_frames == 4
    release.set()
    await asyncio.wait_for(pending, 2)
    await viewer.aclose()


@pytest.mark.asyncio
async def test_push_frame_rejects_invalid_shapes_without_copying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copy_calls = 0
    original_ascontiguousarray = transcoder_module.np.ascontiguousarray

    def count_copy(*args, **kwargs):
        nonlocal copy_calls
        copy_calls += 1
        return original_ascontiguousarray(*args, **kwargs)

    monkeypatch.setattr(transcoder_module.np, "ascontiguousarray", count_copy)
    transcoder = SharedH264Transcoder()

    with pytest.raises(ValueError, match="three-channel"):
        await transcoder.push_frame(np.zeros((64, 96), dtype=np.uint8), 1)

    assert copy_calls == 0


@pytest.mark.asyncio
async def test_push_frame_rejects_inactive_frames_without_copying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copy_calls = 0
    original_ascontiguousarray = transcoder_module.np.ascontiguousarray

    def count_copy(*args, **kwargs):
        nonlocal copy_calls
        copy_calls += 1
        return original_ascontiguousarray(*args, **kwargs)

    monkeypatch.setattr(transcoder_module.np, "ascontiguousarray", count_copy)
    transcoder = SharedH264Transcoder()

    await transcoder.push_frame(_frame(), 1)

    assert copy_calls == 0


@pytest.mark.asyncio
async def test_push_frame_copies_active_generation_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    copy_calls = 0
    original_ascontiguousarray = transcoder_module.np.ascontiguousarray

    def count_copy(*args, **kwargs):
        nonlocal copy_calls
        copy_calls += 1
        return original_ascontiguousarray(*args, **kwargs)

    monkeypatch.setattr(transcoder_module.np, "ascontiguousarray", count_copy)
    transcoder = SharedH264Transcoder()
    viewer, pending = await _start_viewer(transcoder)

    await transcoder.push_frame(_frame(), 1)

    assert copy_calls == 1
    await asyncio.wait_for(pending, 2)
    await viewer.aclose()


@pytest.mark.asyncio
async def test_full_input_queue_releases_stale_frame_before_copying_newest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    transcoder = SharedH264Transcoder(
        TranscodeConfig(queue_size=2),
        codec_factory=_blocking_codec_factory(entered, release),
    )
    viewer, pending = await _start_viewer(transcoder)
    try:
        await transcoder.push_frame(_frame(value=1), 1)
        assert await asyncio.to_thread(entered.wait, 1)

        depths = []
        for value in (2, 3):
            await transcoder.push_frame(_frame(value=value), value)
            depths.append(transcoder.queue_depth)
        assert transcoder._input is not None
        stale_input = transcoder._input._queue[0]
        stale_frame = weakref.ref(stale_input.frame)
        del stale_input

        stale_released_before_copy: list[bool] = []
        original_ascontiguousarray = transcoder_module.np.ascontiguousarray

        def observe_copy(*args, **kwargs):
            stale_released_before_copy.append(stale_frame() is None)
            return original_ascontiguousarray(*args, **kwargs)

        monkeypatch.setattr(transcoder_module.np, "ascontiguousarray", observe_copy)
        await transcoder.push_frame(_frame(value=4), 4)
        depths.append(transcoder.queue_depth)

        assert max(depths) <= 2
        assert stale_released_before_copy == [True]
        assert stale_frame() is None
        assert transcoder._input is not None
        assert [item.frame[0, 0, 0] for item in transcoder._input._queue] == [3, 4]
    finally:
        release.set()
        await asyncio.wait_for(pending, 2)
        await viewer.aclose()


@pytest.mark.asyncio
async def test_emitted_timestamp_history_retains_the_latest_256_values() -> None:
    transcoder = SharedH264Transcoder()
    viewer, pending = await _start_viewer(transcoder)

    transcoder._publish(
        transcoder.generation,
        tuple((b"safe-packet", timestamp) for timestamp in range(300)),
    )

    assert len(transcoder.emitted_timestamps) == 256
    assert transcoder.emitted_timestamps == tuple(range(44, 300))
    await asyncio.wait_for(pending, 2)
    await viewer.aclose()


@pytest.mark.asyncio
async def test_frames_above_configured_fps_are_dropped_before_encoding() -> None:
    transcoder = SharedH264Transcoder(TranscodeConfig(fps=2, queue_size=8))
    viewer, pending = await _start_viewer(transcoder)
    for pts in (0, 100, 500, 600):
        await transcoder.push_frame(_frame(value=pts // 10), pts)

    await asyncio.wait_for(pending, 2)
    for _ in range(100):
        if len(transcoder.emitted_timestamps) >= 2:
            break
        await asyncio.sleep(0.01)

    assert transcoder.emitted_timestamps == (0, 1)
    assert transcoder.dropped_frames == 2
    await viewer.aclose()


@pytest.mark.asyncio
async def test_stop_clears_waiters_and_rejects_late_old_generation_output() -> None:
    entered = threading.Event()
    release = threading.Event()
    transcoder = SharedH264Transcoder(
        codec_factory=_blocking_codec_factory(entered, release)
    )
    old, old_pending = await _start_viewer(transcoder)
    await transcoder.push_frame(_frame(value=30), 1)
    assert await asyncio.to_thread(entered.wait, 1)

    close = asyncio.create_task(transcoder.stop())
    await asyncio.sleep(0)
    new, new_pending = await _start_viewer(transcoder)
    assert transcoder.generation == 1
    assert not new_pending.done()
    release.set()
    await asyncio.wait_for(close, 2)
    with pytest.raises(StopAsyncIteration):
        await old_pending
    await old.aclose()

    await asyncio.sleep(0.05)
    assert not new_pending.done()
    await transcoder.push_frame(_frame(value=200), 2)
    assert await asyncio.wait_for(new_pending, 2)
    await new.aclose()


@pytest.mark.asyncio
async def test_encoder_failure_closes_viewers_with_safe_error() -> None:
    def broken_codec() -> av.VideoCodecContext:
        raise RuntimeError("rtsp://user:secret@camera/private frame material")

    errors: list[str] = []
    transcoder = SharedH264Transcoder(
        codec_factory=broken_codec, on_error=errors.append
    )
    viewer, pending = await _start_viewer(transcoder)
    await transcoder.push_frame(_frame(), 1)

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(pending, 2)
    assert errors == ["transcode_failed"]
    assert transcoder.error_code == "transcode_failed"
    await viewer.aclose()


@pytest.mark.asyncio
async def test_synthetic_frames_emit_decodable_bounded_annexb_h264() -> None:
    config = TranscodeConfig(max_width=1280, max_height=720, fps=4)
    transcoder = SharedH264Transcoder(config)
    viewer, pending = await _start_viewer(transcoder)

    chunks: list[bytes] = []
    for index in range(10):
        await transcoder.push_frame(_frame(1920, 1080, index * 12), index * 250)
        if index == 0:
            chunks.append(await asyncio.wait_for(pending, 3))
        else:
            chunks.append(await asyncio.wait_for(anext(viewer), 3))

    decoded: list[av.VideoFrame] = []
    decoder = av.CodecContext.create("h264", "r")
    for chunk in chunks:
        decoded.extend(decoder.decode(av.Packet(chunk)))

    assert decoded
    assert {(frame.width, frame.height) for frame in decoded} == {(1280, 720)}
    assert transcoder.emitted_timestamps == tuple(sorted(transcoder.emitted_timestamps))
    assert len(set(transcoder.emitted_timestamps)) == len(transcoder.emitted_timestamps)
    keyframe_indexes = [i for i, chunk in enumerate(chunks) if _contains_idr(chunk)]
    assert keyframe_indexes[0] == 0
    assert len(keyframe_indexes) >= 2
    await viewer.aclose()


@pytest.mark.asyncio
async def test_output_dimensions_preserve_aspect_ratio_and_are_even() -> None:
    transcoder = SharedH264Transcoder(TranscodeConfig(max_width=1280, max_height=720))
    viewer, pending = await _start_viewer(transcoder)
    await transcoder.push_frame(_frame(1001, 777), 1)
    chunk = await asyncio.wait_for(pending, 3)

    decoder = av.CodecContext.create("h264", "r")
    frames = decoder.decode(av.Packet(chunk))
    assert frames
    width, height = frames[0].width, frames[0].height
    assert width <= 1280 and height <= 720
    assert width % 2 == 0 and height % 2 == 0
    assert abs(width / height - 1001 / 777) < 0.01
    await viewer.aclose()
