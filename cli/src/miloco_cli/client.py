"""HTTP 客户端，封装对 Miloco 后端的请求。

退出码：
  2 — 网络错误（连接失败、超时）
  3 — 业务错误（后端返回非零 code）
"""

import json
import re
import sys
from typing import Any, NoReturn, cast

import httpx

from miloco_cli.config import load_config


def _get_client(cfg: dict) -> httpx.Client:
    server = cfg["server"]
    headers = {}
    if token := server.get("token"):
        headers["Authorization"] = f"Bearer {token}"
    tls = server.get("tls_verify", False)
    verify = tls if isinstance(tls, bool) else str(tls).lower() == "true"
    return httpx.Client(
        base_url=server["url"],
        headers=headers,
        verify=verify,
        timeout=30,
    )


_STABLE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RTSP_URI = re.compile(r"rtsps?://", re.IGNORECASE)
_REDACTED = "[REDACTED]"


def redact_sensitive_scalar(
    value: Any, sensitive_values: tuple[str, ...]
) -> tuple[Any, bool]:
    """Redact exact credential scalars and long credential substrings."""
    secrets = tuple(secret for secret in sensitive_values if secret)
    if any(value == secret for secret in secrets):
        return _REDACTED, True
    if not isinstance(value, str):
        return value, False

    redacted = value
    for secret in secrets:
        if len(secret) >= 4:
            redacted = redacted.replace(secret, _REDACTED)
    return redacted, redacted != value


def _safe_business_error(
    data: object | None,
    *,
    sensitive_values: tuple[str, ...],
) -> NoReturn:
    """Print only the camera API's approved stable error envelope."""
    detail = data.get("detail") if isinstance(data, dict) else None
    code = detail.get("code") if isinstance(detail, dict) else None
    message = detail.get("message") if isinstance(detail, dict) else None
    _redacted_code, code_contains_sensitive = redact_sensitive_scalar(
        code, sensitive_values
    )
    _redacted_message, message_contains_sensitive = redact_sensitive_scalar(
        message, sensitive_values
    )
    is_stable = (
        isinstance(data, dict)
        and set(data) == {"detail"}
        and isinstance(detail, dict)
        and set(detail) == {"code", "message"}
        and isinstance(code, str)
        and bool(_STABLE_ERROR_CODE.fullmatch(code))
        and isinstance(message, str)
        and bool(message)
        and len(message) <= 200
        and not any(ord(character) < 32 for character in message)
        and not code_contains_sensitive
        and not message_contains_sensitive
        and not _RTSP_URI.search(code)
        and not _RTSP_URI.search(message)
    )

    error: object = (
        {"code": code, "message": message}
        if is_stable
        else {
            "code": "camera_request_failed",
            "message": "Camera request failed",
        }
    )
    print(json.dumps({"error": error}, ensure_ascii=False), file=sys.stderr)
    sys.exit(3)


def _handle_response(
    resp: httpx.Response,
    *,
    safe_errors: bool = False,
    sensitive_values: tuple[str, ...] = (),
) -> dict | list:
    """统一处理响应，业务错误 sys.exit(3)。"""
    try:
        data = resp.json()
    except Exception:
        if safe_errors:
            _safe_business_error(None, sensitive_values=sensitive_values)
        print(
            json.dumps(
                {
                    "error": f"invalid JSON response: {resp.status_code} {resp.text[:200]}"
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        sys.exit(3)

    # FastAPI 4xx/5xx 返回的错误体（如 422 {"detail": [...]}）无 code 字段
    if not resp.is_success:
        if safe_errors:
            _safe_business_error(data, sensitive_values=sensitive_values)
        print(json.dumps({"error": data}, ensure_ascii=False), file=sys.stderr)
        sys.exit(3)

    # observability 系列 endpoint（/api/actions、/api/traces 等）直接返回裸 list,
    # 无 NormalResponse 信封;2xx 已判过,list 恒为成功,原样透传。
    if isinstance(data, dict) and data.get("code", 0) != 0:
        if safe_errors:
            _safe_business_error(data, sensitive_values=sensitive_values)
        print(json.dumps(data, ensure_ascii=False), file=sys.stderr)
        sys.exit(3)

    return data


def _connect_error(url: str) -> NoReturn:
    print(
        json.dumps(
            {"error": f"cannot connect to Miloco backend at {url}"},
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )
    sys.exit(2)


def api_get(
    path: str,
    params: dict | list[tuple[str, str | int | float | None]] | None = None,
    *,
    timeout: float | None = None,
    safe_errors: bool = False,
    sensitive_values: tuple[str, ...] = (),
) -> dict | list:
    cfg = load_config()
    try:
        with _get_client(cfg) as client:
            kw = {"timeout": timeout} if timeout is not None else {}
            resp = client.get(path, params=params, **kw)
            return _handle_response(
                resp,
                safe_errors=safe_errors,
                sensitive_values=sensitive_values,
            )
    except httpx.RequestError:
        _connect_error(cfg["server"]["url"])


def api_post(
    path: str,
    body: dict | None = None,
    *,
    safe_errors: bool = False,
    sensitive_values: tuple[str, ...] = (),
) -> dict:
    cfg = load_config()
    try:
        with _get_client(cfg) as client:
            resp = client.post(path, json=body or {})
            return cast(
                dict,
                _handle_response(
                    resp,
                    safe_errors=safe_errors,
                    sensitive_values=sensitive_values,
                ),
            )
    except httpx.RequestError:
        _connect_error(cfg["server"]["url"])


def api_post_multipart(
    path: str,
    files: list[tuple[str, tuple[str, bytes, str]]],
    data: dict | None = None,
    *,
    safe_errors: bool = False,
    sensitive_values: tuple[str, ...] = (),
) -> dict:
    """POST multipart/form-data（上传文件 + 表单字段）。

    ``files``：``[("字段名", ("文件名", 字节, content_type)), ...]``；同名字段可重复
    （如 medias / crops 多文件）。``data``：普通表单字段，list 值会展开成重复字段
    （如 scores=[...]）。不设 Content-Type，交 httpx 按 multipart 自动生成 boundary。
    """
    cfg = load_config()
    try:
        with _get_client(cfg) as client:
            resp = client.post(path, files=files, data=data or {})
            return cast(
                dict,
                _handle_response(
                    resp,
                    safe_errors=safe_errors,
                    sensitive_values=sensitive_values,
                ),
            )
    except httpx.RequestError:
        _connect_error(cfg["server"]["url"])


def api_put(
    path: str,
    body: dict | None = None,
    *,
    safe_errors: bool = False,
    sensitive_values: tuple[str, ...] = (),
) -> dict:
    cfg = load_config()
    try:
        with _get_client(cfg) as client:
            resp = client.put(path, json=body or {})
            return cast(
                dict,
                _handle_response(
                    resp,
                    safe_errors=safe_errors,
                    sensitive_values=sensitive_values,
                ),
            )
    except httpx.RequestError:
        _connect_error(cfg["server"]["url"])


def api_patch(
    path: str,
    body: dict | None = None,
    *,
    safe_errors: bool = False,
    sensitive_values: tuple[str, ...] = (),
) -> dict:
    cfg = load_config()
    try:
        with _get_client(cfg) as client:
            resp = client.patch(path, json=body or {})
            return cast(
                dict,
                _handle_response(
                    resp,
                    safe_errors=safe_errors,
                    sensitive_values=sensitive_values,
                ),
            )
    except httpx.RequestError:
        _connect_error(cfg["server"]["url"])


def api_delete(
    path: str,
    params: dict | None = None,
    *,
    safe_errors: bool = False,
    sensitive_values: tuple[str, ...] = (),
) -> dict:
    cfg = load_config()
    try:
        with _get_client(cfg) as client:
            resp = client.delete(path, params=params)
            return cast(
                dict,
                _handle_response(
                    resp,
                    safe_errors=safe_errors,
                    sensitive_values=sensitive_values,
                ),
            )
    except httpx.RequestError:
        _connect_error(cfg["server"]["url"])
