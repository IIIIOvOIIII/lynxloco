"""error_classifier 单元测试:覆盖 spec §2 完整分类矩阵。"""

from __future__ import annotations

import json

import httpx
import pytest
from miloco.perception.engine.omni import provider
from miloco.perception.engine.omni.error_classifier import (
    ErrorCategory,
    classify_exception,
    classify_response,
)


def _resp(
    status: int,
    headers: dict | None = None,
    *,
    json_body: object | None = None,
) -> httpx.Response:
    kwargs = {"json": json_body} if json_body is not None else {}
    return httpx.Response(
        status,
        headers=headers or {},
        request=httpx.Request("GET", "https://x/y"),
        **kwargs,
    )


# ─── classify_exception ─────────────────────────────────────────────────────


def test_connect_error_is_unreachable_recoverable():
    r = classify_exception(httpx.ConnectError("conn refused"))
    assert r.code == "unreachable" and r.category == ErrorCategory.RECOVERABLE


def test_connect_timeout_is_unreachable():
    assert classify_exception(httpx.ConnectTimeout("timed out")).code == "unreachable"


def test_read_timeout_is_timeout_recoverable():
    r = classify_exception(httpx.ReadTimeout("read"))
    assert r.code == "timeout" and r.category == ErrorCategory.RECOVERABLE


def test_pool_timeout_is_timeout():
    assert classify_exception(httpx.PoolTimeout("p")).code == "timeout"


def test_write_timeout_is_timeout():
    assert classify_exception(httpx.WriteTimeout("w")).code == "timeout"


def test_json_decode_is_bad_response():
    e = json.JSONDecodeError("no", "", 0)
    assert classify_exception(e).code == "bad_response"


def test_value_error_is_bad_response():
    assert classify_exception(ValueError("bad")).code == "bad_response"


def test_unknown_exception_is_unreachable_recoverable():
    r = classify_exception(RuntimeError("mystery"))
    assert r.code == "unreachable" and r.category == ErrorCategory.RECOVERABLE


# ─── classify_response ──────────────────────────────────────────────────────


def test_200_returns_none():
    assert classify_response(_resp(200)) is None


def test_204_returns_none():
    assert classify_response(_resp(204)) is None


def test_401_is_bad_key_config():
    r = classify_response(_resp(401))
    assert r.code == "bad_key" and r.category == ErrorCategory.CONFIG


def test_403_is_bad_key():
    assert classify_response(_resp(403)).code == "bad_key"


def test_404_is_not_found_config():
    r = classify_response(_resp(404))
    assert r.code == "not_found" and r.category == ErrorCategory.CONFIG


def test_400_is_rejected_authed():
    r = classify_response(_resp(400))
    assert r.code == "rejected_authed" and r.category == ErrorCategory.CONFIG


def test_422_is_rejected_authed():
    assert classify_response(_resp(422)).code == "rejected_authed"


def test_400_visual_request_is_visual_payload_rejected_config():
    """Generic visual 400 must not be misreported as an API-key failure."""
    result = classify_response(_resp(400), visual_request=True)
    assert result.code == "visual_payload_rejected"
    assert result.category == ErrorCategory.CONFIG
    assert "API Key" not in result.message


def test_422_non_visual_request_keeps_generic_rejection():
    result = classify_response(_resp(422), visual_request=False)
    assert result.code == "rejected_authed"


@pytest.mark.parametrize(
    ("provider_code", "expected"),
    [
        ("invalid_api_key", "bad_key"),
        ("authentication_error", "bad_key"),
        ("model_not_found", "not_found"),
        ("invalid_model", "not_found"),
    ],
)
def test_safe_structured_error_code_overrides_visual_fallback(
    provider_code, expected
):
    """Removing the structured allow-list would collapse known config errors."""
    result = classify_response(
        _resp(400, json_body={"error": {"code": provider_code}}),
        visual_request=True,
    )
    assert result.code == expected


def test_safe_structured_error_type_overrides_visual_fallback():
    result = classify_response(
        _resp(422, json_body={"error": {"type": "permission_denied"}}),
        visual_request=True,
    )
    assert result.code == "bad_key"


def test_free_form_provider_message_does_not_override_visual_fallback():
    """Provider prose is untrusted and must never drive auth/model classification."""
    result = classify_response(
        _resp(
            400,
            json_body={
                "error": {
                    "message": "invalid_api_key model_not_found sk-secret data:image"
                }
            },
        ),
        visual_request=True,
    )
    assert result.code == "visual_payload_rejected"
    assert "sk-secret" not in result.message
    assert "data:image" not in result.message


def test_messages_have_visual_input_only_for_image_or_video_blocks():
    assert provider.messages_have_visual_input(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/jpeg;base64,eA==",
                        },
                    }
                ],
            }
        ]
    )
    assert provider.messages_have_visual_input(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "video_url",
                        "video_url": {
                            "url": "data:video/mp4;base64,eA==",
                        },
                    }
                ],
            }
        ]
    )
    assert not provider.messages_have_visual_input(
        [
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": "data:audio/m4a;base64,eA==",
                        },
                    }
                ],
            }
        ]
    )


def test_429_is_rate_limited_recoverable():
    r = classify_response(_resp(429))
    assert r.code == "rate_limited" and r.category == ErrorCategory.RECOVERABLE


def test_429_with_retry_after_seconds():
    r = classify_response(_resp(429, {"Retry-After": "30"}))
    assert r.retry_after_seconds == 30.0


def test_429_without_retry_after_header():
    r = classify_response(_resp(429))
    assert r.retry_after_seconds is None


def test_429_ignores_malformed_retry_after():
    r = classify_response(_resp(429, {"Retry-After": "not-a-number"}))
    assert r.retry_after_seconds is None


def test_500_is_http_error_recoverable():
    r = classify_response(_resp(500))
    assert r.code == "http_error" and r.category == ErrorCategory.RECOVERABLE


def test_502_is_http_error():
    assert classify_response(_resp(502)).code == "http_error"


def test_503_is_http_error():
    assert classify_response(_resp(503)).code == "http_error"


def test_402_falls_to_http_error_recoverable():
    """未显式分类的 4xx 保守归 http_error 可恢复。"""
    r = classify_response(_resp(402))
    assert r.code == "http_error" and r.category == ErrorCategory.RECOVERABLE
