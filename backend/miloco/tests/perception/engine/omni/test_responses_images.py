"""Responses image-sequence construction contracts."""

from __future__ import annotations

import base64
from typing import Any
from unittest.mock import AsyncMock, patch

import cv2
import numpy as np
import pytest
from miloco.perception.engine.config import OmniConfig
from miloco.perception.engine.omni import prompt_builder
from miloco.perception.engine.omni.omni import (
    run_omni,
    run_omni_batch,
    run_omni_batch_stream,
    run_omni_stream,
)
from miloco.perception.engine.omni.prompt_builder import (
    build_batch_prompt,
    build_batch_stream_prompt,
    build_fused_payload,
    build_prompt,
    build_stream_prompt,
)
from miloco.perception.engine.omni.provider import (
    GeminiAdapter,
    MiMoAdapter,
    OpenAIResponsesAdapter,
    QwenOmniAdapter,
)
from miloco.perception.engine.types import (
    AudioAnalysis,
    AudioType,
    CropImage,
    FrameInfo,
    FrameResolution,
    GateTrigger,
    IdentityPacket,
    MotionState,
    OmniContext,
    SelectedFrame,
)


def _image(value: int, *, height: int = 24, width: int = 36) -> np.ndarray:
    return np.full((height, width, 3), value, dtype=np.uint8)


def _packet(
    panoramas: list[np.ndarray],
    *,
    crops: list[CropImage] | None = None,
    audio_only: bool = False,
) -> IdentityPacket:
    selected = [
        SelectedFrame(
            frame_index=index,
            image=frame,
            resolution=FrameResolution.HIGH,
            crops=list(crops or []) if index == 0 else [],
        )
        for index, frame in enumerate(panoramas)
    ]
    trigger = GateTrigger(
        visual_changed=not audio_only,
        visual_change_score=0.0 if audio_only else 1.0,
        audio_active=True,
        audio_energy_level=0.5,
        speech_active=True,
    )
    return IdentityPacket(
        packet_id="packet",
        room_name="room",
        timestamp=1.0,
        frame_info=FrameInfo(start_timestamp=0, end_timestamp=1, fps=1),
        targets=[],
        scene_motion=MotionState.DYNAMIC,
        frames=selected,
        all_frames=panoramas,
        audio_clip=np.ones(2048, dtype=np.int16),
        audio_analysis=AudioAnalysis(
            type=AudioType.SPEECH,
            is_urgent=False,
            energy_level=0.5,
        ),
        trigger=trigger,
    )


def _decoded_mean(encoded: str) -> int:
    raw = np.frombuffer(base64.b64decode(encoded), dtype=np.uint8)
    decoded = cv2.imdecode(raw, cv2.IMREAD_COLOR)
    assert decoded is not None
    return int(round(float(decoded.mean())))


def encode_responses_images(*args, **kwargs):
    """Resolve at call time so the pre-feature suite reaches a behavioral RED."""
    return prompt_builder.encode_responses_images(*args, **kwargs)


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (0, []),
        (1, [10]),
        (6, [10, 20, 30, 40, 50, 60]),
        (10, [10, 30, 50, 60, 80, 100]),
    ],
)
def test_panorama_sequence_uses_uniform_bounded_sampling(
    count: int, expected: list[int]
) -> None:
    frames = [_image((index + 1) * 10) for index in range(count)]

    images = encode_responses_images([_packet(frames)])

    assert [image.source for image in images] == ["panorama"] * len(expected)
    assert [_decoded_mean(image.data) for image in images] == expected


def test_zero_and_one_limits_are_defined() -> None:
    packet = _packet([_image(10), _image(20), _image(30)])

    assert encode_responses_images([packet], panorama_limit=0) == []
    images = encode_responses_images([packet], panorama_limit=1)

    assert len(images) == 1
    assert _decoded_mean(images[0].data) == 10


def test_crops_keep_priority_deduplicate_and_cap_total_at_twelve() -> None:
    duplicate = _image(201, height=12, width=18)
    crops = [
        CropImage(track_id=7, image=duplicate, resolution=FrameResolution.HIGH),
        CropImage(track_id=7, image=duplicate.copy(), resolution=FrameResolution.HIGH),
        *[
            CropImage(
                track_id=track_id,
                image=_image(value, height=12, width=18),
                resolution=FrameResolution.HIGH,
            )
            for track_id, value in zip(range(8, 15), range(202, 209))
        ],
    ]
    packet = _packet([_image(value) for value in (10, 20, 30, 40, 50, 60)], crops=crops)

    images = encode_responses_images([packet])

    assert len(images) == 12
    assert [image.source for image in images] == ["panorama"] * 6 + ["crop"] * 6
    assert [image.track_id for image in images[6:]] == [7, 8, 9, 10, 11, 12]
    assert [_decoded_mean(image.data) for image in images[6:]] == [
        201,
        202,
        203,
        204,
        205,
        206,
    ]
    assert (
        len(encode_responses_images([packet], panorama_limit=99, crop_limit=99)) == 12
    )


def test_images_are_valid_quality_85_jpegs_and_inputs_are_not_modified(
    monkeypatch,
) -> None:
    panorama = np.arange(24 * 36 * 3, dtype=np.uint8).reshape(24, 36, 3)
    crop = np.flip(panorama[:12, :18], axis=1).copy()
    panorama_before = panorama.copy()
    crop_before = crop.copy()
    packet = _packet(
        [panorama],
        crops=[CropImage(track_id=3, image=crop, resolution=FrameResolution.HIGH)],
    )
    original_all_frames = list(packet.all_frames)
    qualities: list[int] = []
    real_imencode = cv2.imencode

    def recording_imencode(ext, image, params=None):
        qualities.append(params[1] if params else -1)
        return real_imencode(ext, image, params or [])

    monkeypatch.setattr(cv2, "imencode", recording_imencode)

    images = encode_responses_images([packet])

    assert len(images) == 2
    assert qualities == [85, 85]
    assert all(base64.b64decode(image.data).startswith(b"\xff\xd8") for image in images)
    assert all(
        cv2.imdecode(np.frombuffer(base64.b64decode(image.data), np.uint8), 1)
        is not None
        for image in images
    )
    decoded_panorama = cv2.imdecode(
        np.frombuffer(base64.b64decode(images[0].data), np.uint8), cv2.IMREAD_COLOR
    )
    decoded_crop = cv2.imdecode(
        np.frombuffer(base64.b64decode(images[1].data), np.uint8), cv2.IMREAD_COLOR
    )
    assert decoded_panorama is not None
    assert decoded_crop is not None
    assert decoded_panorama.shape[:2] == panorama.shape[:2]
    assert decoded_crop.shape[:2] == (512, 512)
    assert packet.all_frames == original_all_frames
    assert all(
        actual is original
        for actual, original in zip(packet.all_frames, original_all_frames)
    )
    np.testing.assert_array_equal(panorama, panorama_before)
    np.testing.assert_array_equal(crop, crop_before)


def test_adapter_media_modes_are_explicit() -> None:
    assert MiMoAdapter().media_mode == "video_audio"
    assert QwenOmniAdapter().media_mode == "video_audio"
    assert GeminiAdapter().media_mode == "video_audio"
    assert OpenAIResponsesAdapter().media_mode == "image_sequence"


def test_responses_prompt_has_images_and_no_video_or_audio() -> None:
    packet = _packet([_image(10), _image(20)])

    payload = build_prompt(packet, OmniContext(), media_mode="image_sequence")

    assert len(payload["images"]) == 2
    assert "video_base64" not in payload
    assert "audio_base64" not in payload


def test_responses_audio_only_prompt_is_text_only() -> None:
    packet = _packet([], audio_only=True)

    payload = build_prompt(
        packet, OmniContext(current_time="12:00"), media_mode="image_sequence"
    )

    assert payload["images"] == []
    assert payload["text_only"] is True
    assert "当前时间" in payload["user_content"]
    assert "audio_base64" not in payload
    assert "video_base64" not in payload
    assert "## speeches" not in payload["system_prompt"]
    assert "## env_sounds" not in payload["system_prompt"]


def test_chat_and_gemini_prompt_regression_keeps_video_audio_and_crops() -> None:
    packet = _packet([_image(10)])
    audio_packet = _packet([], audio_only=True)

    for adapter in (MiMoAdapter(), QwenOmniAdapter(), GeminiAdapter()):
        video = build_prompt(packet, OmniContext(), media_mode=adapter.media_mode)
        audio = build_prompt(audio_packet, OmniContext(), media_mode=adapter.media_mode)
        assert video["video_base64"]
        assert video["crops"] == []
        assert audio["audio_base64"]
        assert "images" not in video
        assert "images" not in audio


def test_fused_responses_uses_only_bounded_sequence_images() -> None:
    packet = _packet([_image(value) for value in (10, 20, 30, 40, 50, 60, 70)])

    payload = build_fused_payload(
        packets=[packet],
        context=OmniContext(),
        candidates=[],
        gallery_snapshot={},
        adapter=OpenAIResponsesAdapter(),
    )
    user_blocks = payload["messages"][-1]["content"]

    assert sum(block["type"] == "image_url" for block in user_blocks) == 6
    assert all(
        block["type"] not in {"video_url", "input_audio"} for block in user_blocks
    )


@pytest.mark.asyncio
async def test_run_omni_resolves_adapter_before_prompt_construction() -> None:
    packet = _packet([_image(10)])
    config = OmniConfig(
        model="local-vlm",
        api_protocol="openai_responses",
        api_key="test",
    )
    response = {
        "choices": [
            {
                "message": {
                    "content": '{"caption":[],"speeches":[],"env_sounds":[],"matched_rules":[],"suggestions":[]}'
                }
            }
        ],
        "usage": {},
    }

    with (
        patch(
            "miloco.perception.engine.omni.omni.build_prompt",
            wraps=build_prompt,
        ) as build,
        patch(
            "miloco.perception.engine.omni.omni.call_omni",
            new_callable=AsyncMock,
            return_value=response,
        ),
    ):
        await run_omni(packet, OmniContext(), config)

    assert build.call_args.kwargs["media_mode"] == "image_sequence"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("runner_name", "builder_name", "builder"),
    [
        ("run_omni_batch", "build_batch_prompt", build_batch_prompt),
        ("run_omni_stream", "build_stream_prompt", build_stream_prompt),
        (
            "run_omni_batch_stream",
            "build_batch_stream_prompt",
            build_batch_stream_prompt,
        ),
    ],
)
async def test_other_omni_entry_points_pass_selected_media_mode(
    runner_name: str,
    builder_name: str,
    builder,
) -> None:
    packet = _packet([_image(10)])
    config = OmniConfig(
        model="local-vlm",
        api_protocol="openai_responses",
        api_key="test",
    )
    runner: Any = {
        "run_omni_batch": run_omni_batch,
        "run_omni_stream": run_omni_stream,
        "run_omni_batch_stream": run_omni_batch_stream,
    }[runner_name]
    first_arg = [packet] if "batch" in runner_name else packet

    with (
        patch(
            f"miloco.perception.engine.omni.omni.{builder_name}", wraps=builder
        ) as build,
        patch(
            "miloco.perception.engine.omni.omni.call_omni",
            new_callable=AsyncMock,
            return_value={
                "choices": [
                    {
                        "message": {
                            "content": '{"caption":[],"speeches":[],"env_sounds":[],"matched_rules":[],"suggestions":[]}'
                        }
                    }
                ],
                "usage": {},
            },
        ),
        patch(
            "miloco.perception.engine.omni.omni._stream_and_parse",
            new_callable=AsyncMock,
            return_value=object(),
        ),
    ):
        await runner(first_arg, OmniContext(), config)

    assert build.call_args.kwargs["media_mode"] == "image_sequence"
