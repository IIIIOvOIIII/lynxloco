"""OpenAI Responses SSE streaming contracts."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from miloco.perception.engine.config import OmniConfig
from miloco.perception.engine.omni import omni_client
from miloco.perception.engine.omni.circuit_breaker import (
    get_omni_circuit_breaker,
    reset_omni_circuit_breaker_for_tests,
)
from miloco.perception.engine.omni.provider import OpenAIResponsesAdapter


def _responses_config() -> OmniConfig:
    return OmniConfig(
        model="local-vlm",
        base_url="http://local.test/v1",
        api_key="",
        api_protocol="openai_responses",
        max_completion_tokens=64,
        temperature=0.4,
        top_p=0.8,
        timeout=1.0,
        stream=True,
    )


def _responses_payload() -> dict[str, Any]:
    return {
        "system_prompt": "Describe visible facts.",
        "user_content": "What changed?",
        "images": [{"media_type": "image/jpeg", "data": "SAFE_IMAGE"}],
    }


def _event(event_type: str | None, data: dict[str, Any]) -> bytes:
    lines = []
    if event_type is not None:
        lines.append(f"event: {event_type}")
    lines.append(f"data: {json.dumps(data)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


class _FragmentedBytes(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


def _stream_client(
    chunks: list[bytes],
    calls: list[httpx.Request] | None = None,
) -> httpx.AsyncClient:
    async def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_FragmentedBytes(chunks),
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _reset_breaker() -> None:
    reset_omni_circuit_breaker_for_tests()
    yield
    reset_omni_circuit_breaker_for_tests()


def test_adapter_emits_delta_and_completed_usage_in_event_order() -> None:
    adapter = OpenAIResponsesAdapter()

    first = adapter.parse_stream_chunk(
        {"type": "response.output_text.delta", "delta": "hel"}
    )
    second = adapter.parse_stream_chunk(
        {"type": "response.output_text.delta", "delta": "lo"}
    )
    completed = adapter.parse_stream_chunk(
        {
            "type": "response.completed",
            "response": {
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 4,
                    "total_tokens": 15,
                    "input_tokens_details": {"cached_tokens": 3},
                }
            },
        }
    )

    assert first == ("hel", None)
    assert second == ("lo", None)
    assert completed == (
        None,
        {
            "prompt_tokens": 11,
            "completion_tokens": 4,
            "total_tokens": 15,
            "prompt_tokens_details": {"cached_tokens": 3},
        },
    )


def test_explicit_sse_event_type_takes_priority_over_json_type() -> None:
    adapter = OpenAIResponsesAdapter()

    parsed = adapter.parse_stream_chunk(
        {
            "type": "response.failed",
            "delta": "kept",
            "response": {"error": {"code": "must_not_win"}},
        },
        event_type="response.output_text.delta",
    )

    assert parsed == ("kept", None)


def test_unknown_event_is_counted_without_logging_payload(caplog) -> None:
    adapter = OpenAIResponsesAdapter()
    caplog.set_level(
        logging.DEBUG,
        logger="miloco.perception.engine.omni.provider",
    )

    assert adapter.parse_stream_chunk(
        {
            "type": "response.future.secret_event",
            "payload": "UNKNOWN_RAW_SECRET_DATA",
        }
    ) == (None, None)

    assert adapter.unknown_stream_event_count == 1
    assert "response.future.secret_event" in caplog.text
    assert "UNKNOWN_RAW_SECRET_DATA" not in caplog.text


@pytest.mark.parametrize(
    ("event_type", "data", "expected_code", "expected_message"),
    [
        (
            "response.failed",
            {
                "response": {
                    "error": {
                        "code": "server_error",
                        "message": "RAW_FAILED_SECRET",
                    }
                }
            },
            "server_error",
            "Responses stream failed",
        ),
        (
            "response.incomplete",
            {
                "response": {
                    "incomplete_details": {
                        "reason": "max_output_tokens",
                        "raw": "RAW_INCOMPLETE_SECRET",
                    }
                }
            },
            "max_output_tokens",
            "Responses stream incomplete",
        ),
        (
            "error",
            {
                "code": "invalid_request",
                "message": "RAW_ERROR_SECRET",
            },
            "invalid_request",
            "Responses stream error",
        ),
    ],
)
def test_terminal_events_raise_only_stable_code_and_message(
    event_type: str,
    data: dict[str, Any],
    expected_code: str,
    expected_message: str,
) -> None:
    adapter = OpenAIResponsesAdapter()

    with pytest.raises(ValueError) as exc_info:
        adapter.parse_stream_chunk(data, event_type=event_type)

    error = exc_info.value
    assert getattr(error, "code") == expected_code
    assert getattr(error, "message") == expected_message
    assert str(error) == f"{expected_code}: {expected_message}"
    for secret in (
        "RAW_FAILED_SECRET",
        "RAW_INCOMPLETE_SECRET",
        "RAW_ERROR_SECRET",
    ):
        assert secret not in str(error)


@pytest.mark.asyncio
async def test_collect_handles_fragmented_crlf_comments_and_multiline_data() -> None:
    body = (
        b": keep-alive\r\n"
        b"event: response.output_text.delta\r\n"
        b'data: {"type":\r\n'
        b'data: "response.output_text.delta", "delta": "A"}\r\n\r\n'
        + _event(
            "response.completed",
            {
                "type": "response.completed",
                "response": {
                    "usage": {
                        "input_tokens": 5,
                        "output_tokens": 1,
                        "total_tokens": 6,
                    }
                },
            },
        )
    )
    # Split inside CRLF, field names, and JSON values to exercise transport
    # fragmentation through httpx's real aiter_lines implementation.
    chunks = [body[:7], body[7:31], body[31:74], body[74:109], body[109:]]
    async with _stream_client(chunks) as client:
        result = await omni_client._collect_stream_response(
            client,
            "http://local.test/v1/responses",
            {},
            {"stream": True},
            OpenAIResponsesAdapter(),
        )

    assert result == {
        "choices": [{"message": {"content": "A"}}],
        "usage": {
            "prompt_tokens": 5,
            "completion_tokens": 1,
            "total_tokens": 6,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }


@pytest.mark.asyncio
async def test_clean_close_dispatches_final_event_without_blank_line() -> None:
    body = b'event: response.output_text.delta\ndata: {"delta":"tail"}'
    async with _stream_client([body]) as client:
        result = await omni_client._collect_stream_response(
            client,
            "http://local.test/v1/responses",
            {},
            {"stream": True},
            OpenAIResponsesAdapter(),
        )

    assert result["choices"] == [{"message": {"content": "tail"}}]
    assert result["usage"] == {}


@pytest.mark.asyncio
async def test_malformed_responses_json_is_stable_bad_response() -> None:
    body = b"event: response.output_text.delta\ndata: {RAW_SECRET_BAD_JSON}\n\n"
    async with _stream_client([body]) as client:
        with pytest.raises(
            ValueError, match="Responses stream event is malformed"
        ) as exc:
            await omni_client._collect_stream_response(
                client,
                "http://local.test/v1/responses",
                {},
                {"stream": True},
                OpenAIResponsesAdapter(),
            )

    assert "RAW_SECRET_BAD_JSON" not in str(exc.value)


@pytest.mark.asyncio
async def test_done_sentinel_does_not_hide_responses_terminal_failure() -> None:
    body = b"data: [DONE]\n\n" + _event(
        "response.failed",
        {"response": {"error": {"code": "server_error", "message": "RAW_SECRET"}}},
    )
    async with _stream_client([body]) as client:
        with pytest.raises(ValueError) as exc:
            await omni_client._collect_stream_response(
                client,
                "http://local.test/v1/responses",
                {},
                {"stream": True},
                OpenAIResponsesAdapter(),
            )

    assert getattr(exc.value, "code") == "server_error"
    assert "RAW_SECRET" not in str(exc.value)


@pytest.mark.asyncio
async def test_call_omni_stream_yields_fragments_and_normalizes_usage(
    monkeypatch,
) -> None:
    calls: list[httpx.Request] = []
    body = (
        _event(None, {"type": "response.output_text.delta", "delta": "hel"})
        + _event("response.output_text.delta", {"delta": "lo"})
        + _event(
            None,
            {
                "type": "response.completed",
                "response": {
                    "usage": {
                        "input_tokens": 9,
                        "output_tokens": 2,
                        "total_tokens": 11,
                        "input_tokens_details": {"cached_tokens": 4},
                    }
                },
            },
        )
    )
    real_client = _stream_client([body[:5], body[5:42], body[42:91], body[91:]], calls)
    monkeypatch.setattr(
        omni_client.httpx,
        "AsyncClient",
        lambda *args, **kwargs: real_client,
    )
    monkeypatch.setattr(omni_client, "fire_record", lambda *args: None)
    usage: dict[str, int] = {}

    fragments = [
        fragment
        async for fragment in omni_client.call_omni_stream(
            _responses_payload(), _responses_config(), usage_out=usage
        )
    ]

    assert fragments == ["hel", "lo"]
    assert usage == {
        "input_tokens": 9,
        "output_tokens": 2,
        "cached_tokens": 4,
        "audio_tokens": 0,
        "video_tokens": 0,
    }
    assert calls[0].url == httpx.URL("http://local.test/v1/responses")
    assert json.loads(calls[0].content)["stream"] is True
    assert "Authorization" not in calls[0].headers


@pytest.mark.asyncio
async def test_terminal_failure_reaches_bad_response_breaker(monkeypatch) -> None:
    body = _event(
        "response.failed",
        {"response": {"error": {"code": "server_error", "message": "RAW_SECRET"}}},
    )
    clients = iter([_stream_client([body]) for _ in range(3)])
    monkeypatch.setattr(
        omni_client.httpx,
        "AsyncClient",
        lambda *args, **kwargs: next(clients),
    )
    monkeypatch.setattr(omni_client, "fire_record", lambda *args: None)

    for _ in range(3):
        with pytest.raises(omni_client.OmniError) as exc:
            async for _ in omni_client.call_omni_stream(
                _responses_payload(), _responses_config()
            ):
                pass

    assert "RAW_SECRET" not in str(exc.value)
    snapshot = get_omni_circuit_breaker().snapshot()
    assert snapshot.state == "warn"
    assert snapshot.code == "bad_response"
