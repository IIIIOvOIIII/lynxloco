"""Retention policy behavior for the real multi-track stream buffer."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest
from miloco.perception.collect.stream_buffer import (
    MultiTrackSyncBuffer,
    TrackRetentionPolicy,
)


@dataclass(frozen=True)
class _VideoPayload:
    frame: np.ndarray
    sequence: int


def _policy(*, frames: int = 13, bytes_: int = 128 * 1024 * 1024):
    return TrackRetentionPolicy(
        track="decoded_video",
        max_items_per_window=frames,
        max_payload_bytes=bytes_,
        payload_size=lambda item: item.frame.nbytes,
    )


def _video(sequence: int, bytes_: int) -> _VideoPayload:
    return _VideoPayload(np.zeros(bytes_, dtype=np.uint8), sequence)


def _buffer(
    *, policy: TrackRetentionPolicy | None = None, **kwargs: object
) -> MultiTrackSyncBuffer:
    return MultiTrackSyncBuffer(
        ["decoded_video", "audio"],
        window_ms=4_000,
        window_settle_ms=0,
        retention_policy=policy,
        **kwargs,
    )


@pytest.mark.parametrize(
    "policy",
    [
        TrackRetentionPolicy("missing", 1, 1, lambda _item: 0),
        TrackRetentionPolicy("decoded_video", 0, 1, lambda _item: 0),
        TrackRetentionPolicy("decoded_video", 1, 0, lambda _item: 0),
    ],
)
def test_rejects_retention_policy_with_unknown_track_or_non_positive_limits(
    policy: TrackRetentionPolicy,
) -> None:
    with pytest.raises(ValueError):
        _buffer(policy=policy)


@pytest.mark.parametrize(
    "payload_size",
    [
        lambda _item: (_ for _ in ()).throw(RuntimeError("size unavailable")),
        lambda _item: -1,
        lambda _item: 1.5,
        lambda _item: True,
    ],
)
def test_invalid_payload_measurement_is_rejected_once_without_payload_logging(
    payload_size: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    policy = TrackRetentionPolicy("decoded_video", 13, 100, payload_size)
    buffer = _buffer(policy=policy)

    class Payload:
        def __repr__(self) -> str:
            return "PAYLOAD_REPRESENTATION_MUST_NOT_APPEAR"

    buffer.put("decoded_video", Payload(), 10, 10)
    buffer.put("decoded_video", Payload(), 20, 20)

    assert buffer.window_count == 0
    assert buffer.retained_payload_bytes == 0
    assert buffer.retention_dropped_items == 2
    assert buffer.retention_last_action == "invalid_payload"
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) <= 1
    assert all(
        "PAYLOAD_REPRESENTATION_MUST_NOT_APPEAR" not in record.message
        for record in warnings
    )


def test_without_policy_retains_more_than_future_frame_and_byte_limits() -> None:
    buffer = _buffer()

    for sequence in range(30):
        buffer.put("decoded_video", _video(sequence, 8), sequence, 100)
        buffer.put("audio", sequence, sequence, 100)

    window = buffer._windows[0]
    assert [
        fragment.data.sequence for fragment in window.tracks["decoded_video"]
    ] == list(range(30))
    assert [fragment.data for fragment in window.tracks["audio"]] == list(range(30))
    assert buffer.retained_payload_bytes == 0
    assert buffer.retention_dropped_items == 0
    assert buffer.consume_drop_stats() == (0, 0, 0, None)


def test_frame_limit_keeps_newest_video_without_evicting_interleaved_audio() -> None:
    buffer = _buffer(policy=_policy(frames=13, bytes_=10_000))

    for sequence in range(30):
        buffer.put("decoded_video", _video(sequence, 1), sequence, 100)
        buffer.put("audio", sequence, sequence, 100)

    window = buffer._windows[0]
    video = window.tracks["decoded_video"]
    assert [fragment.data.sequence for fragment in video] == list(range(17, 30))
    assert len(video) == 13
    assert [fragment.data for fragment in window.tracks["audio"]] == list(range(30))
    assert buffer.retention_dropped_items == 17
    assert buffer.retention_last_action == "frame_limit"
    assert buffer.consume_drop_stats() == (0, 0, 0, None)


def test_byte_budget_evicts_oldest_policy_fragment_across_drained_and_active_windows() -> (
    None
):
    buffer = _buffer(policy=_policy(frames=13, bytes_=10))
    buffer.put("decoded_video", _video(0, 4), 10, 10)
    buffer.put("decoded_video", _video(1, 4), 4_010, 4_010)
    buffer.put("decoded_video", _video(2, 4), 8_010, 8_010)
    buffer.drain_ready()

    buffer.put("decoded_video", _video(3, 6), 12_010, 12_010)

    assert buffer.retained_payload_bytes == 10
    assert buffer.retention_dropped_items == 1
    assert buffer.retention_last_action == "byte_limit"
    peeked = buffer.peek_latest(duration_ms=20_000)
    assert peeked is not None
    assert [fragment.data.sequence for fragment in peeked["decoded_video"]] == [2, 3]


def test_oversized_payload_is_rejected_without_creating_video_window_or_evicting_audio() -> (
    None
):
    buffer = _buffer(policy=_policy(bytes_=4))
    buffer.put("audio", "audio", 10, 10)
    buffer.put("decoded_video", _video(0, 5), 20, 20)

    assert buffer._windows[0].tracks["audio"][-1].data == "audio"
    assert "decoded_video" not in buffer._windows[0].tracks
    assert buffer.retained_payload_bytes == 0
    assert buffer.retention_dropped_items == 1
    assert buffer.retention_last_action == "oversized"


def test_partial_window_expiry_subtracts_policy_payload() -> None:
    buffer = _buffer(policy=_policy(bytes_=100))
    buffer.put("decoded_video", _video(0, 4), 10, 10)
    buffer.put("decoded_video", _video(1, 4), 4_010, 4_010)

    assert buffer.retained_payload_bytes == 4
    assert 0 not in buffer._windows


@pytest.mark.parametrize("action", ["drop", "clear"])
def test_ready_backpressure_removes_policy_payload(action: str) -> None:
    buffer = _buffer(
        policy=_policy(bytes_=100),
        max_windows=0,
        buffer_full_action=action,
    )
    buffer.put("decoded_video", _video(0, 4), 10, 10)
    buffer.put("decoded_video", _video(1, 4), 4_010, 4_010)
    buffer.put("decoded_video", _video(2, 4), 8_010, 8_010)

    assert buffer.retained_payload_bytes == 4
    assert buffer._windows[8_000].tracks["decoded_video"][-1].data.sequence == 2


def test_drain_moves_bytes_once_then_drained_trim_subtracts_oldest_window() -> None:
    buffer = _buffer(policy=_policy(bytes_=100), max_windows=2)
    for sequence in range(5):
        wall_ms = sequence * 4_000 + 10
        buffer.put("decoded_video", _video(sequence, 4), wall_ms, wall_ms)

    assert buffer.retained_payload_bytes == 16
    buffer.drain_ready()

    assert len(buffer._drained) == 2
    assert buffer.retained_payload_bytes == 12
    assert [
        window.tracks["decoded_video"][-1].data.sequence for window in buffer._drained
    ] == [2, 3]


def test_clear_zeros_retained_bytes_preserves_peak_and_cleans_structures() -> None:
    buffer = _buffer(policy=_policy(bytes_=100))
    buffer.put("decoded_video", _video(0, 4), 10, 10)
    buffer.put("audio", "audio", 20, 20)

    assert buffer.retained_payload_bytes == 4
    assert buffer.peak_retained_payload_bytes == 4
    buffer.clear()

    assert buffer.retained_payload_bytes == 0
    assert buffer.peak_retained_payload_bytes == 4
    assert buffer.window_count == 0
    assert not buffer._ready_queue
    assert not buffer._ready_keys
    assert not buffer._drained


def test_payload_measurement_is_stored_once_and_buffers_keep_independent_counters() -> (
    None
):
    calls = 0

    def payload_size(payload: _VideoPayload) -> int:
        nonlocal calls
        calls += 1
        return payload.frame.nbytes

    policy = TrackRetentionPolicy("decoded_video", 1, 4, payload_size)
    first = _buffer(policy=policy)
    second = _buffer(policy=policy)
    first.put("decoded_video", _video(0, 4), 10, 10)
    first.put("decoded_video", _video(1, 4), 20, 20)
    second.put("decoded_video", _video(2, 4), 10, 10)
    first.clear()

    assert calls == 3
    assert first.retained_payload_bytes == 0
    assert first.retention_dropped_items == 1
    assert second.retained_payload_bytes == 4
    assert second.retention_dropped_items == 0


def test_thirty_second_logical_load_bounds_each_buffer_and_preserves_audio() -> None:
    first = _buffer(policy=_policy(), max_windows=100)
    second = _buffer(policy=_policy(), max_windows=100)
    frame_720p = np.zeros((720, 1280, 3), dtype=np.uint8)
    frame_1080p = np.zeros((1080, 1920, 3), dtype=np.uint8)

    for sequence in range(25 * 30):
        wall_ms = sequence * 40
        first.put(
            "decoded_video", _VideoPayload(frame_720p, sequence), wall_ms, wall_ms
        )
        first.put("audio", sequence, wall_ms, wall_ms)
    for sequence in range(30 * 30):
        wall_ms = sequence * 33
        second.put(
            "decoded_video", _VideoPayload(frame_1080p, sequence), wall_ms, wall_ms
        )
        second.put("audio", sequence, wall_ms, wall_ms)

    for buffer, last_sequence in ((first, 749), (second, 899)):
        assert buffer.retained_payload_bytes <= 128 * 1024 * 1024
        assert all(
            len(window.tracks.get("decoded_video", [])) <= 13
            for window in buffer._windows.values()
        )
        newest = max(
            fragment.data.sequence
            for window in buffer._windows.values()
            for fragment in window.tracks.get("decoded_video", [])
        )
        assert newest == last_sequence
        assert any(
            fragment.data == last_sequence
            for window in buffer._windows.values()
            for fragment in window.tracks.get("audio", [])
        )

    assert (
        first.retained_payload_bytes + second.retained_payload_bytes
        <= 256 * 1024 * 1024
    )
    first.clear()
    second.clear()
    assert first.retained_payload_bytes == second.retained_payload_bytes == 0
