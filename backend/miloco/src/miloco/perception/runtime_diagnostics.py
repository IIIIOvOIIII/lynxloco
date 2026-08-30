"""Sanitized runtime diagnostics for realtime perception.

This module deliberately records only shape/count metadata. It must never retain
prompt text, response text, media bytes, URLs, headers, tokens, or account data.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from threading import RLock
from typing import Any, Literal

from miloco.perception.types import RealtimePerceptionResult
from miloco.utils.time_utils import now_ms

OmniDiagnosticRoute = Literal["realtime", "on_demand", "probe", "unknown"]
OmniResponseClassification = Literal[
    "provider_unreachable",
    "provider_timeout",
    "provider_http_error",
    "provider_http_ok_no_text",
    "provider_http_ok_but_parse_skipped",
    "semantic_empty",
    "semantic_non_empty",
]


@dataclass(frozen=True)
class RealtimeOmniDiagnostic:
    timestamp_ms: int
    protocol: str | None
    route: OmniDiagnosticRoute
    message_count: int
    text_block_count: int
    image_block_count: int
    video_block_count: int
    audio_block_count: int
    response_text_length: int
    response_json_like: bool
    parse_ok: bool
    skipped: bool
    caption_count: int
    matched_rule_count: int
    suggestion_count: int
    speech_count: int
    complete_speech_count: int
    needs_response_speech_count: int
    error_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class RuntimeDiagnostics:
    def __init__(self, maxlen: int = 64):
        self._samples: deque[RealtimeOmniDiagnostic] = deque(maxlen=maxlen)
        self._lock = RLock()

    def record(self, sample: RealtimeOmniDiagnostic) -> None:
        with self._lock:
            self._samples.append(sample)

    def latest(self, route: str | None = None) -> RealtimeOmniDiagnostic | None:
        with self._lock:
            if route is None:
                return self._samples[-1] if self._samples else None
            for sample in reversed(self._samples):
                if sample.route == route:
                    return sample
        return None

    def snapshot(self) -> list[RealtimeOmniDiagnostic]:
        with self._lock:
            return list(self._samples)

    def reset_for_tests(self) -> None:
        with self._lock:
            self._samples.clear()


_DIAGNOSTICS = RuntimeDiagnostics()


def get_runtime_diagnostics() -> RuntimeDiagnostics:
    return _DIAGNOSTICS


def summarize_omni_messages(messages: object) -> dict[str, int]:
    summary = {
        "message_count": 0,
        "text_block_count": 0,
        "image_block_count": 0,
        "video_block_count": 0,
        "audio_block_count": 0,
    }
    if not isinstance(messages, list):
        return summary

    summary["message_count"] = len(messages)
    for message in messages:
        if not isinstance(message, dict):
            continue
        _count_content_shape(message.get("content"), summary)
    return summary


def summarize_realtime_result(
    result: RealtimePerceptionResult | None,
) -> dict[str, int | bool]:
    if result is None:
        return {
            "parse_ok": False,
            "skipped": True,
            "caption_count": 0,
            "matched_rule_count": 0,
            "suggestion_count": 0,
            "speech_count": 0,
            "complete_speech_count": 0,
            "needs_response_speech_count": 0,
        }

    speeches = list(result.speeches or [])
    return {
        "parse_ok": not bool(result.skipped),
        "skipped": bool(result.skipped),
        "caption_count": len(result.caption or []),
        "matched_rule_count": len(result.matched_rules or []),
        "suggestion_count": len(result.suggestions or []),
        "speech_count": len(speeches),
        "complete_speech_count": sum(1 for speech in speeches if speech.is_complete),
        "needs_response_speech_count": sum(
            1 for speech in speeches if speech.needs_response
        ),
    }


def extract_response_text(raw: object) -> str:
    if not isinstance(raw, dict):
        return ""

    choice_text = _extract_choices_text(raw)
    if choice_text:
        return choice_text
    return _extract_responses_output_text(raw)


def response_text_is_json_like(text: str) -> bool:
    stripped = text.strip()
    return (stripped.startswith("{") and stripped.endswith("}")) or (
        stripped.startswith("[") and stripped.endswith("]")
    )


def classify_omni_response_shape(
    *,
    error_code: str | None,
    response_text_length: int,
    response_json_like: bool,
    parse_ok: bool,
    skipped: bool,
    caption_count: int,
    matched_rule_count: int,
    suggestion_count: int,
    speech_count: int,
) -> OmniResponseClassification:
    if error_code:
        normalized = error_code.lower()
        if "timeout" in normalized:
            return "provider_timeout"
        if normalized.startswith("httpstatuserror"):
            return "provider_http_error"
        return "provider_unreachable"

    parsed_structured_result = parse_ok and not skipped and response_json_like
    if response_text_length <= 0 and not parsed_structured_result:
        return "provider_http_ok_no_text"

    if skipped or not parse_ok:
        return "provider_http_ok_but_parse_skipped"

    if not response_json_like:
        return "provider_http_ok_but_parse_skipped"

    semantic_count = (
        caption_count + matched_rule_count + suggestion_count + speech_count
    )
    if semantic_count <= 0:
        return "semantic_empty"
    return "semantic_non_empty"


def record_omni_http_diagnostic(
    *,
    request_messages: object,
    response_raw: object,
    protocol: str | None,
    route: str,
    error_code: str | None,
) -> None:
    route_value = _normalize_route(route)
    request_summary = summarize_omni_messages(request_messages)
    response_text = extract_response_text(response_raw)
    response_summary = {
        "response_text_length": len(response_text),
        "response_json_like": response_text_is_json_like(response_text),
        "parse_ok": error_code is None,
        "skipped": error_code is not None,
        "caption_count": 0,
        "matched_rule_count": 0,
        "suggestion_count": 0,
        "speech_count": 0,
        "complete_speech_count": 0,
        "needs_response_speech_count": 0,
    }
    get_runtime_diagnostics().record(
        RealtimeOmniDiagnostic(
            timestamp_ms=now_ms(),
            protocol=protocol,
            route=route_value,
            **request_summary,
            **response_summary,
            error_code=error_code,
        )
    )


def record_realtime_result_diagnostic(
    *,
    result: RealtimePerceptionResult | None,
    protocol: str | None = None,
) -> None:
    semantic_summary = summarize_realtime_result(result)
    get_runtime_diagnostics().record(
        RealtimeOmniDiagnostic(
            timestamp_ms=now_ms(),
            protocol=protocol,
            route="realtime",
            message_count=0,
            text_block_count=0,
            image_block_count=0,
            video_block_count=0,
            audio_block_count=0,
            response_text_length=0,
            response_json_like=True,
            **semantic_summary,
            error_code=getattr(result, "error_code", None) if result is not None else None,
        )
    )


def _count_content_shape(content: object, summary: dict[str, int]) -> None:
    if isinstance(content, str):
        summary["text_block_count"] += 1
        return
    if isinstance(content, dict):
        _count_block_shape(content, summary)
        return
    if not isinstance(content, list):
        return
    for block in content:
        if isinstance(block, dict):
            _count_block_shape(block, summary)
        elif isinstance(block, str):
            summary["text_block_count"] += 1


def _count_block_shape(block: dict[str, Any], summary: dict[str, int]) -> None:
    block_type = block.get("type")
    if block_type in {"text", "input_text", "output_text"}:
        summary["text_block_count"] += 1
    elif block_type in {"image_url", "input_image", "image"}:
        summary["image_block_count"] += 1
    elif block_type in {"video_url", "input_video", "video"}:
        summary["video_block_count"] += 1
    elif block_type in {"input_audio", "audio_url", "audio"}:
        summary["audio_block_count"] += 1


def _extract_choices_text(raw: dict[str, Any]) -> str:
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return _text_from_content(content)


def _extract_responses_output_text(raw: dict[str, Any]) -> str:
    output = raw.get("output")
    if not isinstance(output, list):
        return ""
    chunks: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, list):
            for block in content:
                text = _text_from_content(block)
                if text:
                    chunks.append(text)
        else:
            text = _text_from_content(content)
            if text:
                chunks.append(text)
    return "\n".join(chunks)


def _text_from_content(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, dict):
        return ""
    text = content.get("text")
    return text if isinstance(text, str) else ""


def _normalize_route(route: str) -> OmniDiagnosticRoute:
    if route in {"realtime", "on_demand", "probe", "unknown"}:
        return route  # type: ignore[return-value]
    return "unknown"
