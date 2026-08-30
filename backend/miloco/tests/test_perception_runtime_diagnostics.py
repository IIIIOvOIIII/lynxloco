from __future__ import annotations

from typing import Any

import httpx
import pytest

from miloco.perception.engine.config import OmniConfig
from miloco.perception.types import CaptionEntry, RealtimePerceptionResult, Speech, Suggestion


class _AsyncClient:
    def __init__(self, response: httpx.Response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        return self._response


class _FusedClient:
    def __init__(self, response: httpx.Response):
        self._response = response

    async def post(self, *args, **kwargs):
        return self._response


@pytest.fixture(autouse=True)
def _reset_runtime_diagnostics():
    from miloco.perception.runtime_diagnostics import get_runtime_diagnostics

    get_runtime_diagnostics().reset_for_tests()
    yield
    get_runtime_diagnostics().reset_for_tests()


def _responses_config() -> OmniConfig:
    return OmniConfig(
        model="local-vlm",
        base_url="http://127.0.0.1:8000/v1/",
        api_key="test-key",
        api_protocol="openai_responses",
        max_completion_tokens=32,
        temperature=0,
        top_p=1,
        timeout=1.0,
        stream=False,
    )


def _messages() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "secret system prompt"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "secret user prompt"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64,AAAA"},
                },
            ],
        },
    ]


def _responses_raw(text: str) -> dict[str, Any]:
    return {
        "output": [{"content": [{"type": "output_text", "text": text}]}],
        "usage": {
            "input_tokens": 3,
            "output_tokens": 2,
            "total_tokens": 5,
            "input_tokens_details": {"cached_tokens": 0},
        },
    }


def test_summarize_omni_messages_counts_blocks_without_content():
    from miloco.perception.runtime_diagnostics import summarize_omni_messages

    messages = [
        {"role": "system", "content": "secret system prompt"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "secret user prompt"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64,AAAA"},
                },
            ],
        },
    ]

    summary = summarize_omni_messages(messages)

    assert summary == {
        "message_count": 2,
        "text_block_count": 2,
        "image_block_count": 1,
        "video_block_count": 0,
        "audio_block_count": 0,
    }
    assert "secret" not in repr(summary)
    assert "AAAA" not in repr(summary)


@pytest.mark.asyncio
async def test_call_omni_records_sanitized_runtime_diagnostic(monkeypatch):
    from miloco.perception.engine.omni import omni_client
    from miloco.perception.runtime_diagnostics import get_runtime_diagnostics

    response = httpx.Response(
        200,
        json=_responses_raw('{"caption": []}'),
        request=httpx.Request("POST", "http://127.0.0.1:8000/v1/responses"),
    )
    monkeypatch.setattr(
        omni_client.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _AsyncClient(response),
    )

    await omni_client.call_omni(
        {
            "system_prompt": "secret system prompt",
            "user_content": "secret user prompt",
            "images": [{"media_type": "image/jpeg", "data": "AAAA"}],
        },
        _responses_config(),
    )

    latest = get_runtime_diagnostics().latest("realtime")
    assert latest is not None
    assert latest.protocol == "openai_responses"
    assert latest.message_count == 2
    assert latest.text_block_count == 2
    assert latest.image_block_count == 1
    assert latest.response_text_length == len('{"caption": []}')
    assert latest.response_json_like is True
    assert "secret" not in repr(latest.to_dict())
    assert "AAAA" not in repr(latest.to_dict())


@pytest.mark.asyncio
async def test_fused_call_records_sanitized_runtime_diagnostic(monkeypatch):
    from miloco.perception.engine.omni import omni
    from miloco.perception.runtime_diagnostics import get_runtime_diagnostics

    response = httpx.Response(
        200,
        json=_responses_raw('{"caption": []}'),
        request=httpx.Request("POST", "http://127.0.0.1:8000/v1/responses"),
    )
    monkeypatch.setattr(
        omni,
        "_get_fused_http_client",
        lambda timeout: _FusedClient(response),
    )

    await omni._call_omni_messages(_messages(), _responses_config())

    latest = get_runtime_diagnostics().latest("realtime")
    assert latest is not None
    assert latest.protocol == "openai_responses"
    assert latest.message_count == 2
    assert latest.text_block_count == 2
    assert latest.image_block_count == 1
    assert latest.response_text_length == len('{"caption": []}')
    assert latest.response_json_like is True
    assert "secret" not in repr(latest.to_dict())
    assert "AAAA" not in repr(latest.to_dict())


def test_runtime_diagnostics_is_bounded_and_returns_latest():
    from miloco.perception.runtime_diagnostics import (
        RealtimeOmniDiagnostic,
        RuntimeDiagnostics,
    )

    diag = RuntimeDiagnostics(maxlen=2)
    diag.record(
        RealtimeOmniDiagnostic(
            timestamp_ms=1,
            protocol="openai_responses",
            route="realtime",
            message_count=1,
            text_block_count=1,
            image_block_count=0,
            video_block_count=0,
            audio_block_count=0,
            response_text_length=0,
            response_json_like=False,
            parse_ok=True,
            skipped=False,
            caption_count=0,
            matched_rule_count=0,
            suggestion_count=0,
            speech_count=0,
            complete_speech_count=0,
            needs_response_speech_count=0,
        )
    )
    diag.record(
        RealtimeOmniDiagnostic(
            timestamp_ms=2,
            protocol="openai_responses",
            route="on_demand",
            message_count=1,
            text_block_count=1,
            image_block_count=1,
            video_block_count=0,
            audio_block_count=0,
            response_text_length=42,
            response_json_like=True,
            parse_ok=True,
            skipped=False,
            caption_count=1,
            matched_rule_count=0,
            suggestion_count=0,
            speech_count=0,
            complete_speech_count=0,
            needs_response_speech_count=0,
        )
    )
    diag.record(
        RealtimeOmniDiagnostic(
            timestamp_ms=3,
            protocol="openai_responses",
            route="realtime",
            message_count=1,
            text_block_count=1,
            image_block_count=1,
            video_block_count=0,
            audio_block_count=0,
            response_text_length=20,
            response_json_like=False,
            parse_ok=True,
            skipped=False,
            caption_count=0,
            matched_rule_count=0,
            suggestion_count=0,
            speech_count=0,
            complete_speech_count=0,
            needs_response_speech_count=0,
        )
    )

    assert [s.timestamp_ms for s in diag.snapshot()] == [2, 3]
    assert diag.latest("realtime").timestamp_ms == 3


def test_summarize_realtime_result_counts_semantic_fields():
    from miloco.perception.runtime_diagnostics import summarize_realtime_result

    result = RealtimePerceptionResult(
        caption=[CaptionEntry(description="客厅有人走动", area="客厅")],
        suggestions=[Suggestion(event="灯没关", action="提醒关灯", room_name="客厅")],
        speeches=[
            Speech(
                speaker="客厅",
                content="打开灯",
                is_complete=True,
                needs_response=True,
            )
        ],
    )

    assert summarize_realtime_result(result) == {
        "parse_ok": True,
        "skipped": False,
        "caption_count": 1,
        "matched_rule_count": 0,
        "suggestion_count": 1,
        "speech_count": 1,
        "complete_speech_count": 1,
        "needs_response_speech_count": 1,
    }


def test_classify_omni_response_shape_separates_provider_and_semantic_states():
    from miloco.perception.runtime_diagnostics import classify_omni_response_shape

    assert (
        classify_omni_response_shape(
            error_code="ReadTimeout",
            response_text_length=0,
            response_json_like=False,
            parse_ok=False,
            skipped=True,
            caption_count=0,
            matched_rule_count=0,
            suggestion_count=0,
            speech_count=0,
        )
        == "provider_timeout"
    )
    assert (
        classify_omni_response_shape(
            error_code=None,
            response_text_length=0,
            response_json_like=False,
            parse_ok=True,
            skipped=False,
            caption_count=0,
            matched_rule_count=0,
            suggestion_count=0,
            speech_count=0,
        )
        == "provider_http_ok_no_text"
    )
    assert (
        classify_omni_response_shape(
            error_code=None,
            response_text_length=24,
            response_json_like=False,
            parse_ok=False,
            skipped=True,
            caption_count=0,
            matched_rule_count=0,
            suggestion_count=0,
            speech_count=0,
        )
        == "provider_http_ok_but_parse_skipped"
    )
    assert (
        classify_omni_response_shape(
            error_code=None,
            response_text_length=48,
            response_json_like=True,
            parse_ok=True,
            skipped=False,
            caption_count=0,
            matched_rule_count=0,
            suggestion_count=0,
            speech_count=0,
        )
        == "semantic_empty"
    )
    assert (
        classify_omni_response_shape(
            error_code=None,
            response_text_length=48,
            response_json_like=True,
            parse_ok=True,
            skipped=False,
            caption_count=1,
            matched_rule_count=0,
            suggestion_count=0,
            speech_count=0,
        )
        == "semantic_non_empty"
    )
