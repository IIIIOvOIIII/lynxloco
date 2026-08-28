"""OpenAI Responses perception contract integration tests."""

from __future__ import annotations

import base64
import json
import logging
import os
import signal
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import numpy as np
import pytest
from miloco.perception.engine.config import OmniConfig
from miloco.perception.engine.omni.circuit_breaker import (
    get_omni_circuit_breaker,
    reset_omni_circuit_breaker_for_tests,
)
from miloco.perception.engine.omni.omni_client import (
    OmniError,
    call_omni,
    call_omni_stream,
    extract_usage,
)
from miloco.perception.engine.omni.probe import probe_omni
from miloco.perception.engine.omni.prompt_builder import (
    build_prompt,
    build_stream_prompt,
)
from miloco.perception.engine.omni.provider import (
    GeminiAdapter,
    MiMoAdapter,
    QwenOmniAdapter,
    get_adapter,
)
from miloco.perception.engine.omni.response_parser import (
    parse_omni_response,
    parse_omni_response_from_text,
)
from miloco.perception.engine.types import (
    AudioAnalysis,
    AudioType,
    CropImage,
    FrameInfo,
    FrameResolution,
    IdentityPacket,
    MotionState,
    OmniContext,
    SelectedFrame,
)
from miloco.perception.snapshot_context import OmniEventArtifacts, event_artifacts_scope
from responses_fixture_server import ResponsesFixtureServer

_SECRET = "fixture-secret-that-must-not-leak"
_DATA_URL_PREFIX = "data:image/jpeg;base64,"


@pytest.fixture(autouse=True)
def _reset_breaker_and_api_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    reset_omni_circuit_breaker_for_tests()
    monkeypatch.delenv("MILOCO_MODEL__OMNI__API_KEY", raising=False)
    yield
    reset_omni_circuit_breaker_for_tests()


def _packet() -> IdentityPacket:
    frames = [
        np.full((24, 36, 3), value, dtype=np.uint8)
        for value in (10, 20, 30, 40, 50, 60, 70, 80)
    ]
    return IdentityPacket(
        packet_id="responses-integration",
        room_name="lab",
        timestamp=1.0,
        frame_info=FrameInfo(start_timestamp=0.0, end_timestamp=1.0, fps=1),
        targets=[],
        scene_motion=MotionState.DYNAMIC,
        frames=[
            SelectedFrame(
                frame_index=index,
                image=frame,
                resolution=FrameResolution.HIGH,
                crops=(
                    [
                        CropImage(
                            track_id=track_id,
                            image=np.full((12, 18, 3), value, dtype=np.uint8),
                            resolution=FrameResolution.HIGH,
                        )
                        for track_id, value in enumerate(
                            (110, 120, 130, 140, 150, 160, 170, 180), start=1
                        )
                    ]
                    if index == 0
                    else []
                ),
            )
            for index, frame in enumerate(frames)
        ],
        all_frames=frames,
        audio_clip=np.zeros(0, dtype=np.int16),
        audio_analysis=AudioAnalysis(
            type=AudioType.SILENCE,
            is_urgent=False,
            energy_level=0.0,
        ),
    )


def _payload(*, stream: bool = False) -> dict:
    packet = _packet()
    adapter = get_adapter("openai_responses", "fixture-vlm")
    builder = build_stream_prompt if stream else build_prompt
    return builder(packet, OmniContext(), media_mode=adapter.media_mode)


def _config(base_url: str, *, api_key: str = "") -> OmniConfig:
    return OmniConfig(
        model="fixture-vlm",
        base_url=base_url,
        api_protocol="openai_responses",
        api_key=api_key,
        timeout=2.0,
    )


@pytest.mark.parametrize("api_key", ["", _SECRET])
@pytest.mark.asyncio
async def test_non_stream_perception_reaches_existing_parser(api_key: str) -> None:
    payload = _payload()

    with ResponsesFixtureServer(api_key=api_key) as fixture:
        normalized = await call_omni(
            payload, _config(fixture.base_url, api_key=api_key)
        )

    parsed = parse_omni_response(normalized)
    assert parsed.caption[0].description == "fixture saw the room"
    assert extract_usage(normalized) == {
        "input_tokens": 23,
        "output_tokens": 7,
        "cached_tokens": 3,
        "audio_tokens": 0,
        "video_tokens": 0,
    }
    assert fixture.requests[-1].path == "/v1/responses"
    assert fixture.requests[-1].image_count == 12
    assert fixture.requests[-1].auth_present is bool(api_key)


@pytest.mark.asyncio
async def test_stream_perception_reaches_existing_parser_and_usage() -> None:
    usage: dict[str, int] = {}

    with ResponsesFixtureServer() as fixture:
        chunks = [
            chunk
            async for chunk in call_omni_stream(
                _payload(stream=True),
                _config(fixture.base_url),
                usage_out=usage,
            )
        ]

    parsed = parse_omni_response_from_text("".join(chunks))
    assert parsed.caption[0].description == "fixture saw the room"
    assert usage == {
        "input_tokens": 23,
        "output_tokens": 7,
        "cached_tokens": 3,
        "audio_tokens": 0,
        "video_tokens": 0,
    }
    assert fixture.requests[-1].stream is True
    assert fixture.requests[-1].image_count == 12


@pytest.mark.asyncio
async def test_visual_preflight_tolerates_missing_models_endpoint() -> None:
    with ResponsesFixtureServer(models_status=404) as fixture:
        result = await probe_omni(
            "fixture-vlm",
            fixture.base_url,
            "",
            "openai_responses",
        )

    assert result["ok"] is True
    assert [(request.method, request.path) for request in fixture.requests] == [
        ("GET", "/v1/models"),
        ("POST", "/v1/responses"),
    ]
    assert fixture.requests[-1].image_count == 1


def _minimal_jpeg_data_url() -> str:
    return _DATA_URL_PREFIX + base64.b64encode(b"\xff\xd8\xff\xd9").decode()


def _valid_wire_body() -> dict:
    return {
        "model": "fixture-vlm",
        "instructions": "Describe visible facts.",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Describe the image."},
                    {"type": "input_image", "image_url": _minimal_jpeg_data_url()},
                ],
            }
        ],
        "max_output_tokens": 32,
        "stream": False,
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda body: body["input"][0].update(
            content=[{"type": "input_text", "text": "text only"}]
        ),
        lambda body: body["input"][0].update(
            content=[
                {"type": "input_text", "text": "too many"},
                *[
                    {"type": "input_image", "image_url": _minimal_jpeg_data_url()}
                    for _ in range(13)
                ],
            ]
        ),
        lambda body: body.update(temperature=0.1),
        lambda body: body.update(top_p=0.9),
        lambda body: body.update(tools=[]),
        lambda body: body.update(provider_private={"secret": True}),
        lambda body: body["input"][0]["content"].append(
            {"type": "input_audio", "input_audio": {"data": "not-allowed"}}
        ),
        lambda body: body["input"][0]["content"].append(
            {"type": "video_url", "video_url": {"url": "not-allowed"}}
        ),
    ],
    ids=[
        "missing-image",
        "over-twelve",
        "temperature",
        "top-p",
        "tools",
        "private-field",
        "audio",
        "video",
    ],
)
def test_strict_fixture_rejects_unsupported_requests(mutate) -> None:
    body = _valid_wire_body()
    mutate(body)

    with ResponsesFixtureServer() as fixture:
        response = httpx.post(f"{fixture.base_url}/responses", json=body, timeout=2)

    assert response.status_code == 400
    assert fixture.requests == []


def test_strict_fixture_rejects_wrong_auth_and_path() -> None:
    with ResponsesFixtureServer(api_key=_SECRET) as fixture:
        wrong_auth = httpx.post(
            f"{fixture.base_url}/responses", json=_valid_wire_body(), timeout=2
        )
        wrong_path = httpx.post(
            f"{fixture.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {_SECRET}"},
            json=_valid_wire_body(),
            timeout=2,
        )

    assert wrong_auth.status_code == 401
    assert wrong_path.status_code == 404
    assert fixture.requests == []


@pytest.mark.asyncio
async def test_repeated_http_failure_opens_breaker_and_short_circuits() -> None:
    with ResponsesFixtureServer(responses_status=503) as fixture:
        for _ in range(3):
            with pytest.raises(OmniError):
                await call_omni(_payload(), _config(fixture.base_url))
        with pytest.raises(OmniError, match="short-circuited"):
            await call_omni(_payload(), _config(fixture.base_url))

    snapshot = get_omni_circuit_breaker().snapshot()
    assert snapshot.state == "warn"
    assert snapshot.consecutive_failures == 3
    assert len(fixture.requests) == 3


@pytest.mark.asyncio
async def test_trace_and_logs_never_contain_key_or_image_payload(caplog) -> None:
    artifacts = OmniEventArtifacts()
    caplog.set_level(logging.DEBUG)

    with ResponsesFixtureServer(api_key=_SECRET) as fixture:
        with event_artifacts_scope(artifacts):
            await call_omni(
                _payload(),
                _config(fixture.base_url, api_key=_SECRET),
            )

    serialized_trace = json.dumps(artifacts.trace, ensure_ascii=False)
    ordinary_logs = caplog.text
    assert _SECRET not in serialized_trace
    assert _DATA_URL_PREFIX not in serialized_trace
    assert _SECRET not in ordinary_logs
    assert _DATA_URL_PREFIX not in ordinary_logs
    assert artifacts.trace is not None
    call = artifacts.trace["calls"][0]
    image_blocks = [
        block
        for block in call["request"]["user_blocks"]
        if block["type"] == "image_url"
    ]
    assert image_blocks == [{"type": "image_url"}] * 12


def test_mimo_qwen_and_gemini_fixtures_keep_existing_wire_and_parse_contracts() -> None:
    messages = [{"role": "user", "content": "fixture"}]
    openai_raw = {
        "choices": [{"message": {"content": "fixture output"}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 2},
    }

    mimo = MiMoAdapter()
    mimo_body = mimo.build_request_body(
        messages,
        model="xiaomi/mimo-v2.5",
        max_tokens=64,
        temperature=0.1,
        top_p=0.95,
    )
    assert mimo.endpoint(
        "https://mimo.example/v1", "xiaomi/mimo-v2.5", stream=False
    ) == ("https://mimo.example/v1/chat/completions")
    assert mimo_body["thinking"] == {"type": "disabled"}
    assert mimo.parse_response(openai_raw) is openai_raw

    qwen = QwenOmniAdapter()
    qwen_body = qwen.build_request_body(
        messages,
        model="qwen3.5-omni-flash",
        max_tokens=64,
        temperature=0.1,
        top_p=0.95,
    )
    assert qwen_body["stream"] is True
    assert qwen_body["modalities"] == ["text"]
    assert qwen.parse_response(openai_raw) is openai_raw

    gemini = GeminiAdapter()
    gemini_body = gemini.build_request_body(
        messages,
        model="gemini-3-flash",
        max_tokens=64,
        temperature=0.1,
        top_p=0.95,
    )
    gemini_raw = {
        "candidates": [{"content": {"parts": [{"text": "fixture output"}]}}],
        "usageMetadata": {
            "promptTokenCount": 3,
            "candidatesTokenCount": 2,
            "totalTokenCount": 5,
        },
    }
    assert gemini_body["contents"] == [{"role": "user", "parts": [{"text": "fixture"}]}]
    assert gemini.parse_response(gemini_raw) == {
        "choices": [{"message": {"content": "fixture output"}}],
        "usage": {
            "prompt_tokens": 3,
            "completion_tokens": 2,
            "total_tokens": 5,
        },
    }


def test_smoke_runs_visual_preflight_then_synthetic_perception_without_leaks() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    script = repo_root / "scripts" / "responses-vlm-smoke.sh"

    with ResponsesFixtureServer(api_key=_SECRET, models_status=404) as fixture:
        completed = subprocess.run(
            [str(script)],
            cwd=repo_root,
            env={
                "PATH": os.environ["PATH"],
                "MILOCO_RESPONSES_BASE_URL": fixture.base_url,
                "MILOCO_RESPONSES_MODEL": "fixture-vlm",
                "MILOCO_RESPONSES_API_KEY": _SECRET,
            },
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    assert completed.returncode == 0, completed.stderr
    output = dict(line.split("=", 1) for line in completed.stdout.splitlines())
    assert set(output) == {
        "protocol",
        "model",
        "latency_ms",
        "image_count",
        "output_present",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
    }
    assert output["protocol"] == "openai_responses"
    assert output["model"] == "fixture-vlm"
    assert int(output["latency_ms"]) >= 0
    assert int(output["image_count"]) >= 1
    assert output["output_present"] == "true"
    assert output["input_tokens"] == "23"
    assert output["output_tokens"] == "7"
    assert output["cached_tokens"] == "3"
    assert completed.stderr == ""
    assert _SECRET not in completed.stdout
    assert _DATA_URL_PREFIX not in completed.stdout
    assert [(request.method, request.path) for request in fixture.requests] == [
        ("GET", "/v1/models"),
        ("POST", "/v1/responses"),
        ("POST", "/v1/responses"),
    ]


def test_smoke_does_not_inherit_generic_omni_key_for_no_key_responses() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    script = repo_root / "scripts" / "responses-vlm-smoke.sh"
    inherited_key = "old-generic-key-that-must-not-cross-endpoints"

    with ResponsesFixtureServer(models_status=404) as fixture:
        completed = subprocess.run(
            [str(script)],
            cwd=repo_root,
            env={
                "PATH": os.environ["PATH"],
                "MILOCO_RESPONSES_BASE_URL": fixture.base_url,
                "MILOCO_RESPONSES_MODEL": "fixture-vlm",
                "MILOCO_MODEL__OMNI__API_KEY": inherited_key,
            },
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    assert completed.returncode == 0
    assert inherited_key not in completed.stdout
    assert completed.stderr == ""
    assert [(request.method, request.path) for request in fixture.requests] == [
        ("GET", "/v1/models"),
        ("POST", "/v1/responses"),
        ("POST", "/v1/responses"),
    ]
    assert all(request.auth_present is False for request in fixture.requests)


def _process_group_python_or_uv_members(process_group: int) -> list[int]:
    completed = subprocess.run(
        ["ps", "-axo", "pid=,pgid=,command="],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    members: list[int] = []
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=2)
        if len(fields) != 3:
            continue
        pid_text, pgid_text, command = fields
        if int(pgid_text) == process_group and (
            "python" in command.casefold() or "uv" in command.casefold()
        ):
            members.append(int(pid_text))
    return members


def test_smoke_sigterm_exits_143_without_residual_uv_or_python_child() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    script = repo_root / "scripts" / "responses-vlm-smoke.sh"

    with ResponsesFixtureServer(hang_perception=True) as fixture:
        process = subprocess.Popen(
            [str(script)],
            cwd=repo_root,
            env={
                "PATH": os.environ["PATH"],
                "MILOCO_RESPONSES_BASE_URL": fixture.base_url,
                "MILOCO_RESPONSES_MODEL": "fixture-vlm",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            assert fixture.perception_hang_started.wait(timeout=10)
            os.kill(process.pid, signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=3)
        except BaseException:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            raise

    assert process.returncode == 143
    assert stdout == ""
    assert stderr == ""
    deadline = time.monotonic() + 1.0
    remaining = _process_group_python_or_uv_members(process.pid)
    while remaining and time.monotonic() < deadline:
        time.sleep(0.02)
        remaining = _process_group_python_or_uv_members(process.pid)
    assert remaining == []


@pytest.mark.parametrize(
    "server_options",
    [{"responses_status": 503}, {"malformed_response": True}],
    ids=["http-failure", "parse-failure"],
)
def test_smoke_fails_closed_without_printing_remote_data(server_options: dict) -> None:
    repo_root = Path(__file__).resolve().parents[4]
    script = repo_root / "scripts" / "responses-vlm-smoke.sh"

    with ResponsesFixtureServer(**server_options) as fixture:
        completed = subprocess.run(
            [str(script)],
            cwd=repo_root,
            env={
                "PATH": os.environ["PATH"],
                "MILOCO_RESPONSES_BASE_URL": fixture.base_url,
                "MILOCO_RESPONSES_MODEL": "fixture-vlm",
            },
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    assert completed.returncode != 0
    assert completed.stdout == ""
    assert completed.stderr == ""


def test_smoke_rejects_credentialed_or_query_urls_without_echoing_them() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    script = repo_root / "scripts" / "responses-vlm-smoke.sh"
    unsafe_url = "http://user:password@127.0.0.1:1/v1?token=do-not-print"

    completed = subprocess.run(
        [str(script)],
        cwd=repo_root,
        env={
            "PATH": os.environ["PATH"],
            "MILOCO_RESPONSES_BASE_URL": unsafe_url,
            "MILOCO_RESPONSES_MODEL": "fixture-vlm",
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr == ""


def test_smoke_has_valid_shell_syntax_and_owner_executable_mode() -> None:
    repo_root = Path(__file__).resolve().parents[4]
    script = repo_root / "scripts" / "responses-vlm-smoke.sh"

    syntax = subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert syntax.returncode == 0
    assert syntax.stdout == ""
    assert syntax.stderr == ""
    assert script.stat().st_mode & 0o777 == 0o755
