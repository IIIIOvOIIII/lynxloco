"""OpenAI Responses non-streaming provider contracts."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from miloco.perception.engine.config import OmniConfig
from miloco.perception.engine.omni import omni, omni_client
from miloco.perception.engine.omni.circuit_breaker import (
    get_omni_circuit_breaker,
    reset_omni_circuit_breaker_for_tests,
)
from miloco.perception.engine.omni.provider import OpenAIResponsesAdapter
from miloco.perception.snapshot_context import (
    OmniEventArtifacts,
    event_artifacts_scope,
)


def _messages() -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": "Describe only visible facts."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "What changed?"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64,IMAGE_ONE"},
                },
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/jpeg;base64,IMAGE_TWO"},
                },
            ],
        },
    ]


def _responses_config(*, api_key: str = "") -> OmniConfig:
    return OmniConfig(
        model="local-vlm",
        base_url="http://127.0.0.1:8000/v1/",
        api_key=api_key,
        api_protocol="openai_responses",
        max_completion_tokens=321,
        temperature=0.4,
        top_p=0.8,
        timeout=1.0,
        stream=False,
    )


def _responses_payload() -> dict[str, Any]:
    return {
        "system_prompt": "Describe only visible facts.",
        "user_content": "What changed?",
        "images": [
            {"media_type": "image/jpeg", "data": "IMAGE_ONE"},
            {"media_type": "image/jpeg", "data": "IMAGE_TWO"},
        ],
    }


def _live_settings(
    *,
    model: str,
    base_url: str,
    api_key: str,
    api_protocol: str | None,
):
    class _Omni:
        pass

    current = _Omni()
    current.model = model
    current.base_url = base_url
    current.api_key = api_key
    current.api_protocol = api_protocol

    class _Model:
        omni = current

    class _Settings:
        model = _Model()

    return _Settings()


class _Response:
    status_code = 200
    headers: dict[str, str] = {}
    text = ""

    def __init__(self, raw: Any):
        self._raw = raw

    def json(self) -> Any:
        return self._raw

    def raise_for_status(self) -> None:
        return None


class _FusedClient:
    def __init__(self, response: Any, calls: list[dict[str, Any]]):
        self._response = response
        self._calls = calls

    async def post(self, url, **kwargs):
        self._calls.append({"url": url, **kwargs})
        return self._response


def _capturing_client(raw: Any, calls: list[dict[str, Any]]):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            calls.append({"url": url, **kwargs})
            return _Response(raw)

    return _Client


@pytest.fixture(autouse=True)
def _reset_breaker():
    reset_omni_circuit_breaker_for_tests()
    yield
    reset_omni_circuit_breaker_for_tests()


def test_request_contract_is_exact_and_omits_unsupported_fields() -> None:
    adapter = OpenAIResponsesAdapter()

    body = adapter.build_request_body(
        _messages(),
        model="local-vlm",
        max_tokens=321,
        temperature=0.4,
        top_p=0.8,
        stream=False,
    )

    assert adapter.endpoint("http://localhost:8000/v1/", "local-vlm", stream=False) == (
        "http://localhost:8000/v1/responses"
    )
    assert body == {
        "model": "local-vlm",
        "instructions": "Describe only visible facts.",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "What changed?"},
                    {
                        "type": "input_image",
                        "image_url": "data:image/jpeg;base64,IMAGE_ONE",
                    },
                    {
                        "type": "input_image",
                        "image_url": "data:image/jpeg;base64,IMAGE_TWO",
                    },
                ],
            }
        ],
        "max_output_tokens": 321,
        "stream": False,
    }
    wire = repr(body)
    for forbidden in (
        "temperature",
        "top_p",
        "tools",
        "video_url",
        "input_audio",
        "audio_base64",
        "video_base64",
        "thinking",
        "modalities",
    ):
        assert forbidden not in wire


def test_auth_headers_allow_empty_key_and_use_bearer_when_present() -> None:
    adapter = OpenAIResponsesAdapter()

    assert adapter.auth_headers("") == {}
    assert adapter.auth_headers("local-secret") == {
        "Authorization": "Bearer local-secret"
    }


@pytest.mark.parametrize("block_type", ["video_url", "input_audio", "file"])
def test_unsupported_media_block_fails_locally(block_type: str) -> None:
    messages = _messages()
    messages[-1]["content"].append({"type": block_type, "payload": "SECRET_MEDIA"})

    with pytest.raises(ValueError, match="unsupported Responses content block") as exc:
        OpenAIResponsesAdapter().build_request_body(
            messages,
            model="local-vlm",
            max_tokens=1,
            temperature=0,
            top_p=1,
        )

    assert "SECRET_MEDIA" not in str(exc.value)


@pytest.mark.parametrize(
    ("raw", "expected_text"),
    [
        (
            {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "one"}],
                    }
                ]
            },
            "one",
        ),
        (
            {
                "output": [
                    {
                        "type": "reasoning",
                        "content": [{"type": "summary_text", "text": "ignore"}],
                    },
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": "hello "},
                            {"type": "refusal", "refusal": "ignore"},
                            {"type": "output_text", "text": "world"},
                        ],
                    },
                ]
            },
            "hello world",
        ),
    ],
)
def test_parse_response_aggregates_only_output_text(
    raw: dict[str, Any], expected_text: str
) -> None:
    normalized = OpenAIResponsesAdapter().parse_response(raw)

    assert normalized["choices"] == [{"message": {"content": expected_text}}]
    assert normalized["usage"] == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "prompt_tokens_details": {"cached_tokens": 0},
    }


def test_parse_response_normalizes_usage_and_cache() -> None:
    normalized = OpenAIResponsesAdapter().parse_response(
        {
            "output": [{"content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {
                "input_tokens": 11,
                "output_tokens": 7,
                "total_tokens": 18,
                "input_tokens_details": {"cached_tokens": 3},
            },
        }
    )

    assert normalized["usage"] == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
        "prompt_tokens_details": {"cached_tokens": 3},
    }


def test_parse_response_usage_without_cache_defaults_only_cache_to_zero() -> None:
    normalized = OpenAIResponsesAdapter().parse_response(
        {
            "output": [{"content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {
                "input_tokens": 9,
                "output_tokens": 4,
                "total_tokens": 13,
            },
        }
    )

    assert normalized["usage"] == {
        "prompt_tokens": 9,
        "completion_tokens": 4,
        "total_tokens": 13,
        "prompt_tokens_details": {"cached_tokens": 0},
    }


@pytest.mark.parametrize(
    "usage",
    ["bad", {"input_tokens": "not-a-number"}, {"input_tokens_details": "bad"}],
)
def test_parse_response_malformed_usage_has_stable_error(usage: Any) -> None:
    with pytest.raises(ValueError, match="Responses usage is malformed") as exc:
        OpenAIResponsesAdapter().parse_response(
            {
                "output": [{"content": [{"type": "output_text", "text": "ok"}]}],
                "usage": usage,
            }
        )

    assert repr(usage) not in str(exc.value)


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {"output": []},
        {"output": [{"content": []}]},
        {"output": [{"content": [{"type": "output_text", "text": "   "}]}]},
        {"output": "not-a-list"},
        ["not", "an", "object"],
    ],
)
def test_missing_empty_or_malformed_output_is_stable_bad_response(raw: Any) -> None:
    with pytest.raises(
        ValueError, match="Responses output contains no output_text"
    ) as exc:
        OpenAIResponsesAdapter().parse_response(raw)

    assert repr(raw) not in str(exc.value)


@pytest.mark.asyncio
async def test_call_omni_allows_empty_key_normalizes_before_consumers_and_sanitizes(
    monkeypatch, caplog
) -> None:
    calls: list[dict[str, Any]] = []
    records: list[tuple[Any, ...]] = []
    artifacts = OmniEventArtifacts()
    raw = {
        "output": [{"content": [{"type": "output_text", "text": "normalized answer"}]}],
        "usage": {
            "input_tokens": 5,
            "output_tokens": 2,
            "total_tokens": 7,
            "input_tokens_details": {"cached_tokens": 1},
        },
        "provider_private_secret": "RAW_RESPONSE_SECRET",
    }
    monkeypatch.delenv("MILOCO_MODEL__OMNI__API_KEY", raising=False)
    monkeypatch.setattr(
        omni_client.httpx,
        "AsyncClient",
        _capturing_client(raw, calls),
    )
    monkeypatch.setattr(omni_client, "fire_record", lambda *args: records.append(args))
    with event_artifacts_scope(artifacts):
        result = await omni_client.call_omni(_responses_payload(), _responses_config())

    assert result["choices"][0]["message"]["content"] == "normalized answer"
    assert calls[0]["url"] == "http://127.0.0.1:8000/v1/responses"
    assert "Authorization" not in calls[0]["headers"]
    assert calls[0]["json"] == {
        "model": "local-vlm",
        "instructions": "Describe only visible facts.",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "What changed?"},
                    {
                        "type": "input_image",
                        "image_url": "data:image/jpeg;base64,IMAGE_ONE",
                    },
                    {
                        "type": "input_image",
                        "image_url": "data:image/jpeg;base64,IMAGE_TWO",
                    },
                ],
            }
        ],
        "max_output_tokens": 321,
        "stream": False,
    }
    assert records == [
        (
            "local-vlm",
            {
                "prompt_tokens": 5,
                "completion_tokens": 2,
                "total_tokens": 7,
                "prompt_tokens_details": {"cached_tokens": 1},
            },
            "realtime",
        )
    ]
    assert artifacts.trace is not None
    trace_call = artifacts.trace["calls"][0]
    assert trace_call["request"] == {
        "system": "Describe only visible facts.",
        "user_blocks": [
            {"type": "text", "text": "What changed?"},
            {"type": "image_url"},
            {"type": "image_url"},
        ],
    }
    assert trace_call["response"] == {
        "content": "normalized answer",
        "usage": result["usage"],
    }
    assert "provider_private_secret" not in repr(trace_call)
    log_text = caplog.text
    for secret in ("IMAGE_ONE", "IMAGE_TWO", "RAW_RESPONSE_SECRET"):
        assert secret not in log_text


@pytest.mark.asyncio
async def test_responses_bad_shape_reaches_bad_response_breaker(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    monkeypatch.delenv("MILOCO_MODEL__OMNI__API_KEY", raising=False)
    monkeypatch.setattr(
        omni_client.httpx,
        "AsyncClient",
        _capturing_client({"output": []}, calls),
    )

    for _ in range(3):
        with pytest.raises(omni_client.OmniError, match="call_omni failed"):
            await omni_client.call_omni(_responses_payload(), _responses_config())

    snapshot = get_omni_circuit_breaker().snapshot()
    assert snapshot.state == "warn"
    assert snapshot.code == "bad_response"


@pytest.mark.asyncio
async def test_existing_chat_protocol_still_requires_a_key(monkeypatch) -> None:
    monkeypatch.delenv("MILOCO_MODEL__OMNI__API_KEY", raising=False)
    config = OmniConfig(
        model="mimo",
        base_url="https://example.invalid/v1",
        api_key="",
        api_protocol="openai_chat_completions",
    )

    with pytest.raises(ValueError, match="MILOCO_MODEL__OMNI__API_KEY"):
        await omni_client.call_omni(
            {"system_prompt": "system", "user_content": "user"}, config
        )


@pytest.mark.asyncio
async def test_live_switch_to_keyless_responses_never_sends_old_cloud_key(
    monkeypatch,
) -> None:
    calls: list[dict[str, Any]] = []
    old_snapshot = OmniConfig(
        model="cloud-chat",
        base_url="https://cloud.example/v1",
        api_key="OLD_CLOUD_SECRET",
        api_protocol="openai_chat_completions",
    )
    monkeypatch.setattr(
        "miloco.config.get_settings",
        lambda: _live_settings(
            model="local-vlm",
            base_url="http://127.0.0.1:8000/v1/",
            api_key="",
            api_protocol="openai_responses",
        ),
    )
    monkeypatch.delenv("MILOCO_MODEL__OMNI__API_KEY", raising=False)
    monkeypatch.setattr(
        omni_client.httpx,
        "AsyncClient",
        _capturing_client(
            {"output": [{"content": [{"type": "output_text", "text": "local"}]}]},
            calls,
        ),
    )

    resolved = omni_client.resolve_live_omni_config(old_snapshot)
    result = await omni_client.call_omni(_responses_payload(), resolved)

    assert resolved.api_key == ""
    assert result["choices"][0]["message"]["content"] == "local"
    assert calls[0]["url"] == "http://127.0.0.1:8000/v1/responses"
    assert "Authorization" not in calls[0]["headers"]
    assert "OLD_CLOUD_SECRET" not in repr(calls)
    assert omni_client._maybe_reset_breaker_on_config_change._last_triple == (
        "openai_responses",
        "local-vlm",
        "http://127.0.0.1:8000/v1/",
        "",
    )


@pytest.mark.parametrize(
    ("current_protocol", "current_model", "current_base_url"),
    [
        ("openai_responses", "same-model", "https://same.example/v1"),
        ("openai_chat_completions", "different-model", "https://same.example/v1"),
        ("openai_chat_completions", "same-model", "https://other.example/v1"),
    ],
)
def test_live_identity_change_never_inherits_snapshot_key(
    monkeypatch,
    current_protocol: str,
    current_model: str,
    current_base_url: str,
) -> None:
    base = OmniConfig(
        model="same-model",
        base_url="https://same.example/v1",
        api_key="OLD_ENDPOINT_SECRET",
        api_protocol="openai_chat_completions",
    )
    monkeypatch.setattr(
        "miloco.config.get_settings",
        lambda: _live_settings(
            model=current_model,
            base_url=current_base_url,
            api_key="",
            api_protocol=current_protocol,
        ),
    )

    resolved = omni_client.resolve_live_omni_config(base)

    assert resolved.api_key == ""


@pytest.mark.parametrize(
    ("base_protocol", "current_protocol", "current_base_url"),
    [
        (
            "openai_chat_completions",
            "openai_chat_completions",
            "https://same.example/v1",
        ),
        (None, "openai_chat_completions", "https://same.example/v1"),
        ("openai_chat_completions", None, "https://same.example/v1/"),
    ],
)
def test_same_effective_identity_preserves_snapshot_key_when_current_key_empty(
    monkeypatch,
    base_protocol: str | None,
    current_protocol: str | None,
    current_base_url: str,
) -> None:
    base = OmniConfig(
        model="same-model",
        base_url="https://same.example/v1",
        api_key="SNAPSHOT_KEY",
        api_protocol=base_protocol,
    )
    monkeypatch.setattr(
        "miloco.config.get_settings",
        lambda: _live_settings(
            model="same-model",
            base_url=current_base_url,
            api_key="",
            api_protocol=current_protocol,
        ),
    )

    resolved = omni_client.resolve_live_omni_config(base)

    assert resolved.api_key == "SNAPSHOT_KEY"


def test_explicit_current_key_always_overrides_snapshot_key(monkeypatch) -> None:
    base = OmniConfig(
        model="old-model",
        base_url="https://old.example/v1",
        api_key="OLD_KEY",
        api_protocol="openai_chat_completions",
    )
    monkeypatch.setattr(
        "miloco.config.get_settings",
        lambda: _live_settings(
            model="new-model",
            base_url="https://new.example/v1",
            api_key="NEW_KEY",
            api_protocol="openai_responses",
        ),
    )

    resolved = omni_client.resolve_live_omni_config(base)

    assert resolved.api_key == "NEW_KEY"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("api_key", "expected_authorization"),
    [("", None), ("fused-local-key", "Bearer fused-local-key")],
)
async def test_fused_responses_key_policy_and_normalization_precede_consumers(
    monkeypatch, api_key: str, expected_authorization: str | None
) -> None:
    calls: list[dict[str, Any]] = []
    records: list[tuple[Any, ...]] = []
    artifacts = OmniEventArtifacts()
    raw = {
        "output": [
            {"content": [{"type": "output_text", "text": "fused normalized answer"}]}
        ],
        "usage": {
            "input_tokens": 8,
            "output_tokens": 3,
            "total_tokens": 11,
            "input_tokens_details": {"cached_tokens": 2},
        },
        "private_provider_field": "FUSED_RAW_SECRET",
    }
    monkeypatch.delenv("MILOCO_MODEL__OMNI__API_KEY", raising=False)
    monkeypatch.setattr(
        omni,
        "_get_fused_http_client",
        lambda timeout: _FusedClient(_Response(raw), calls),
    )
    monkeypatch.setattr(omni, "fire_record", lambda *args: records.append(args))

    config = _responses_config(api_key=api_key)
    with event_artifacts_scope(artifacts):
        result = await omni._call_omni_messages(_messages(), config)

    assert calls[0]["url"] == "http://127.0.0.1:8000/v1/responses"
    assert calls[0]["headers"].get("Authorization") == expected_authorization
    assert result == {
        "choices": [{"message": {"content": "fused normalized answer"}}],
        "usage": {
            "prompt_tokens": 8,
            "completion_tokens": 3,
            "total_tokens": 11,
            "prompt_tokens_details": {"cached_tokens": 2},
        },
    }
    assert records == [("local-vlm", result["usage"], "realtime")]
    assert omni.extract_usage(result) == {
        "input_tokens": 8,
        "output_tokens": 3,
        "cached_tokens": 2,
        "audio_tokens": 0,
        "video_tokens": 0,
    }
    assert artifacts.trace is not None
    trace_call = artifacts.trace["calls"][0]
    assert trace_call["response"] == {
        "content": "fused normalized answer",
        "usage": result["usage"],
    }
    assert "FUSED_RAW_SECRET" not in repr(trace_call)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai_chat_completions", "gemini_native"])
async def test_fused_existing_keyed_protocols_still_require_key(
    monkeypatch, protocol: str
) -> None:
    monkeypatch.delenv("MILOCO_MODEL__OMNI__API_KEY", raising=False)
    config = OmniConfig(
        model="existing-provider",
        base_url="https://example.invalid/v1",
        api_key="",
        api_protocol=protocol,
    )

    with pytest.raises(ValueError, match="MILOCO_MODEL__OMNI__API_KEY"):
        await omni._call_omni_messages(_messages(), config)


@pytest.mark.asyncio
async def test_fused_http_error_log_omits_raw_body_base64_and_key(
    monkeypatch, caplog
) -> None:
    calls: list[dict[str, Any]] = []
    request = httpx.Request("POST", "http://127.0.0.1:8000/v1/responses")
    response = httpx.Response(
        400,
        request=request,
        text="HTTP_RAW_SECRET IMAGE_ONE fused-local-key",
    )
    monkeypatch.setattr(
        omni,
        "_get_fused_http_client",
        lambda timeout: _FusedClient(response, calls),
    )

    with pytest.raises(omni_client.OmniError):
        await omni._call_omni_messages(
            _messages(), _responses_config(api_key="fused-local-key")
        )

    assert "400" in caplog.text
    for secret in ("HTTP_RAW_SECRET", "IMAGE_ONE", "fused-local-key"):
        assert secret not in caplog.text


@pytest.mark.asyncio
async def test_fused_non_dict_log_omits_raw_response(monkeypatch, caplog) -> None:
    calls: list[dict[str, Any]] = []
    raw = ["NON_DICT_RAW_SECRET"]
    monkeypatch.setattr(
        omni,
        "_get_fused_http_client",
        lambda timeout: _FusedClient(_Response(raw), calls),
    )
    config = OmniConfig(
        model="existing-provider",
        base_url="https://example.invalid/v1",
        api_key="configured-key",
        api_protocol="openai_chat_completions",
    )

    with pytest.raises(omni_client.OmniError, match="not a dict"):
        await omni._call_omni_messages(_messages(), config)

    assert "non-dict" in caplog.text
    assert "NON_DICT_RAW_SECRET" not in caplog.text
