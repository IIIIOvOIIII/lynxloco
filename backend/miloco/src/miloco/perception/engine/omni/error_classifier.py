"""omni 调用异常/响应到统一错误码集合的映射。

映射规则见 spec §2。CODES 与 web 前端 omniHealth.codes 一一对应,
前端直接复用 i18n。「测试连接」结果表 OMNI_CODE_KEY 是本集合的裁剪变体:去掉
probe 路径不会产生的 timeout(probe 把所有异常统一归 unreachable)、加上成功码 ok。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum

import httpx


class ErrorCategory(Enum):
    RECOVERABLE = "recoverable"  # 进指数退避熔断
    CONFIG = "config"  # 直接软停,等用户改配置


@dataclass(frozen=True)
class ClassifiedError:
    code: str
    message: str
    category: ErrorCategory
    retry_after_seconds: float | None = (
        None  # 仅 rate_limited 且 Retry-After 存在时非空
    )


CODES: set[str] = {
    "unreachable",
    "timeout",
    "http_error",
    "rate_limited",
    "bad_key",
    "no_key",
    "not_found",
    "rejected_authed",
    "visual_payload_rejected",
    "bad_response",
    "cancelled",
}


_MESSAGES: dict[str, str] = {
    "unreachable": "无法连接 omni 服务",
    "timeout": "omni 服务响应超时",
    "http_error": "omni 服务返回异常",
    "rate_limited": "被 provider 限流",
    "bad_key": "API Key 无效或无权限",
    "no_key": "未配置 API Key",
    "not_found": "模型或地址不存在",
    "rejected_authed": "已连接，但请求被拒绝（模型名或 API Key 可能有误）",
    "visual_payload_rejected": "端点可连接，但当前协议或视觉请求不受支持",
    "bad_response": "omni 响应格式异常",
    "cancelled": "重试被中断",
}

_SAFE_PROVIDER_CONFIG_CODES = {
    "invalid_api_key": "bad_key",
    "authentication_error": "bad_key",
    "unauthorized": "bad_key",
    "permission_denied": "bad_key",
    "model_not_found": "not_found",
    "invalid_model": "not_found",
}

CONFIG_CODES = frozenset(
    {"bad_key", "no_key", "not_found", "rejected_authed", "visual_payload_rejected"}
)


def category_for_code(code: str) -> ErrorCategory:
    return ErrorCategory.CONFIG if code in CONFIG_CODES else ErrorCategory.RECOVERABLE


def classify_exception(exc: BaseException) -> ClassifiedError:
    """httpx 异常/本地异常 → ClassifiedError。未知异常保守归 unreachable。"""
    if isinstance(exc, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout)):
        return ClassifiedError(
            "timeout", _MESSAGES["timeout"], ErrorCategory.RECOVERABLE
        )
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError, httpx.NetworkError)):
        return ClassifiedError(
            "unreachable", _MESSAGES["unreachable"], ErrorCategory.RECOVERABLE
        )
    if isinstance(exc, (json.JSONDecodeError, ValueError)):
        return ClassifiedError(
            "bad_response", _MESSAGES["bad_response"], ErrorCategory.RECOVERABLE
        )
    return ClassifiedError(
        "unreachable", _MESSAGES["unreachable"], ErrorCategory.RECOVERABLE
    )


def safe_provider_config_code(resp: httpx.Response) -> str | None:
    try:
        payload = resp.json()
    except (json.JSONDecodeError, httpx.ResponseNotRead, RuntimeError, ValueError):
        return None
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict):
        return None
    for field in ("code", "type"):
        value = error.get(field)
        if isinstance(value, str):
            mapped = _SAFE_PROVIDER_CONFIG_CODES.get(value.casefold().strip())
            if mapped:
                return mapped
    return None


def classify_response(
    resp: httpx.Response,
    *,
    visual_request: bool = False,
) -> ClassifiedError | None:
    """HTTP 响应 → ClassifiedError；2xx 返 None(调用方按成功处理)。"""
    s = resp.status_code
    if 200 <= s < 300:
        return None
    if s in (401, 403):
        return ClassifiedError("bad_key", _MESSAGES["bad_key"], ErrorCategory.CONFIG)
    if s == 404:
        return ClassifiedError(
            "not_found", _MESSAGES["not_found"], ErrorCategory.CONFIG
        )
    if s in (400, 422):
        code = safe_provider_config_code(resp)
        if code is None:
            code = "visual_payload_rejected" if visual_request else "rejected_authed"
        return ClassifiedError(code, _MESSAGES[code], ErrorCategory.CONFIG)
    if s == 429:
        retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
        return ClassifiedError(
            "rate_limited",
            _MESSAGES["rate_limited"],
            ErrorCategory.RECOVERABLE,
            retry_after,
        )
    if s >= 500:
        return ClassifiedError(
            "http_error",
            f"{_MESSAGES['http_error']}（HTTP {s}）",
            ErrorCategory.RECOVERABLE,
        )
    return ClassifiedError(
        "http_error",
        f"{_MESSAGES['http_error']}（HTTP {s}）",
        ErrorCategory.RECOVERABLE,
    )


def _parse_retry_after(v: str | None) -> float | None:
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None  # HTTP-date 格式不解析,交给默认 backoff
