"""omni provider 连通性探测。

统一被 web preflight (admin/router.py) 与运行时 circuit_breaker HALF_OPEN 复用。
两阶段探测：GET /models 验鉴权+可达；再用合成红色 JPEG 真校验视觉能力。

返回统一形状 {ok, code, status?, latency_ms?, message}。code 集合与 spec §2 一致。
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx

from miloco.perception.engine.omni.error_classifier import safe_provider_config_code

if TYPE_CHECKING:
    from miloco.config.settings import OmniApiProtocol
    from miloco.perception.engine.omni.provider import OmniProviderAdapter

_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
_RESPONSES_VISUAL_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_RESPONSES_VISUAL_MAX_OUTPUT_TOKENS = 256
_ALLOWED_SCHEMES = ("http", "https")
_VISUAL_PROBE_IMAGE = Path(__file__).with_name("assets") / "visual_probe_red.jpg"
_VISUAL_PROBE_PROMPT = (
    "What is the dominant color of this image? "
    "Reply with exactly one English color word and no other text."
)


def _normalize_base_url(base_url: str) -> tuple[str | None, str | None]:
    """校验 base_url 并归一化(去尾斜杠)。

    只挡非 http/https scheme(拒 file/gopher/ftp/data 等)。**不挡内网/链路本地 IP**——
    家用场景的自建 LLM (Ollama http://127.0.0.1:11434 / vLLM http://192.168.x.x:8000
    / Tailscale http://100.64.x.x) 就是常见 base_url,禁内网 = 禁自建。

    防"key 通过 base_url 外泄"靠的是 admin/router.py::_key_by_label 的跨 URL 凭证隔离
    (base_url 变了不沿用旧 key),不靠这里的 IP 黑名单。docstring 明说这点避免后续读者
    误以为这层做了 SSRF 防护。

    返回 (normalized, error_message);合法时 error 为 None。
    """
    parsed = urlparse(base_url)
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        return (
            None,
            f"Base URL 协议非法（仅支持 http/https，实际: {scheme or 'empty'}）",
        )
    if not parsed.netloc:
        return None, "Base URL 缺少主机名"
    return base_url.rstrip("/"), None


async def probe_reachable(base_url: str) -> dict | None:
    """无 key 时判 Base URL 是否明显有问题;使 URL 错优先于「缺 key」暴露。

    - scheme 非法 / 网络错 → {code: unreachable, ...}
    - 2xx/3xx 或 401/403 → None(URL 没问题,问题在缺 key)
    - 其他 4xx/5xx → {code: http_error, ...}
    """
    base, err = _normalize_base_url(base_url)
    if err is not None:
        return {"code": "unreachable", "message": err}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{base}/models")
    except Exception as e:  # noqa: BLE001
        return {
            "code": "unreachable",
            "message": f"无法连接 Base URL（{type(e).__name__}）",
        }
    if r.status_code < 400 or r.status_code in (401, 403):
        return None
    return {"code": "http_error", "message": f"服务返回异常（HTTP {r.status_code}）"}


async def fetch_models(
    base_url: str,
    api_key: str,
    api_protocol: OmniApiProtocol,
) -> dict[str, Any]:
    """拉取 provider 模型列表(GET /models)。

    协议由调用方显式传入,Base URL 只负责寻址,绝不参与 provider 推断。Gemini 原生
    使用 ``x-goog-api-key`` 和 ``{models:[{name}]}``;Chat/Responses 使用 OpenAI
    ``{data:[{id}]}`` 形状。Responses 的 Key 可空,非空时才发送 Bearer。
    """
    base, err = _normalize_base_url(base_url)
    if err is not None:
        return {"ok": False, "code": "unreachable", "models": [], "message": err}
    is_gemini = api_protocol == "gemini_native"
    if is_gemini:
        headers = {"x-goog-api-key": api_key}
    elif api_key:
        headers = {"Authorization": f"Bearer {api_key}"}
    else:
        headers = {}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(f"{base}/models", headers=headers)
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "code": "unreachable",
            "models": [],
            "message": f"无法连接 Base URL（{type(e).__name__}）",
        }
    if r.status_code == 200:
        try:
            if is_gemini:
                ids = [
                    (m.get("name") or "").removeprefix("models/")
                    for m in (r.json().get("models") or [])
                    if m.get("name")
                ]
            else:
                ids = [m.get("id") for m in (r.json().get("data") or []) if m.get("id")]
        except Exception:  # noqa: BLE001
            ids = []
        return {"ok": True, "models": sorted(i for i in ids if i)}
    if api_protocol == "openai_responses" and r.status_code in (404, 405):
        return {
            "ok": True,
            "code": "unsupported",
            "models": [],
            "message": "该协议不支持模型发现，请手动填写模型",
        }
    if r.status_code in (401, 403):
        return {
            "ok": False,
            "code": "bad_key",
            "models": [],
            "message": "API Key 无效或无权限",
        }
    return {
        "ok": False,
        "code": "http_error",
        "models": [],
        "message": f"服务返回异常（HTTP {r.status_code}）",
    }


class _FakeStatusResp:
    """占位:probe_chat 流式路径下,非 200 场景把 status_code 塞进"看起来像 httpx.Response"
    的最小对象里,复用下方 status_code 分支代码。仅用 status_code / json / text / headers
    四个属性。

    headers 包成 httpx.Headers 而非 plain dict:非流式路径的真实 Response.headers 是
    httpx.Headers(大小写不敏感);_probe_stream_chat 传上来的 dict(resp.headers) 已经把
    header 名小写化了(httpx 语义),若这里存成 plain dict,下方 429 分支的
    r.headers.get("Retry-After")(大写 R/A)会大小写敏感 miss → 恒 None,与非流式路径的
    大小写不敏感 hit 行为不一致,Qwen 撞 429 时会丢掉 server 明示的 Retry-After。
    """

    def __init__(
        self, status_code: int, json_body: dict, text: str, headers: dict | None = None
    ):
        self.status_code = status_code
        self._json = json_body
        self.text = text
        self.headers = httpx.Headers(headers or {})

    def json(self) -> Any:
        return self._json


async def _probe_stream_chat(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    t0: float,
    adapter: OmniProviderAdapter,
) -> tuple[int, int, str | None, dict[str, str], str | None]:
    """流式探测:解析 SSE 并返回累积输出。

    非 200 status 回带 response headers 与已映射的安全配置码。上层可保留
    Retry-After 和 allow-listed auth/model 分类，但不会接触 provider 原始 body。
    """
    async with client.stream(
        "POST",
        url,
        headers=headers,
        json=body,
    ) as resp:
        if resp.status_code != 200:
            await resp.aread()  # 允许连接释放
            safe_config_code = (
                safe_provider_config_code(resp)
                if resp.status_code in (400, 422)
                else None
            )
            return resp.status_code, 0, None, dict(resp.headers), safe_config_code
        output_parts: list[str] = []
        async for line in resp.aiter_lines():
            line = line.strip()
            if not line.startswith("data: "):
                continue
            data = line[6:]
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
                delta, _usage = adapter.parse_stream_chunk(chunk)
            except (
                AttributeError,
                json.JSONDecodeError,
                KeyError,
                TypeError,
                ValueError,
            ):
                latency_ms = round((time.monotonic() - t0) * 1000)
                return 200, latency_ms, None, {}, None
            if delta is not None and not isinstance(delta, str):
                latency_ms = round((time.monotonic() - t0) * 1000)
                return 200, latency_ms, None, {}, None
            if delta:
                output_parts.append(delta)
        latency_ms = round((time.monotonic() - t0) * 1000)
        return 200, latency_ms, "".join(output_parts), {}, None


def _visual_answer_is_red(value: Any) -> bool:
    return isinstance(value, str) and value.casefold().strip() in {"red", "red."}


async def probe_chat(
    model: str,
    base_url: str,
    api_key: str,
    api_protocol: OmniApiProtocol | None = None,
) -> dict[str, Any]:
    """使用合成红色 JPEG 的 chat 探测(max_tokens=16)真校验视觉能力。

    走 provider adapter 生成 body,兼容不同 provider 的强制要求(Qwen 强制
    stream=True + modalities=["text"])。之前硬编码非流式 body 打 Qwen 会被
    400/422 判成 rejected_authed,合法配置反而进 OPEN_CONFIG。
    """
    base, err = _normalize_base_url(base_url)
    if err is not None:
        return {"ok": False, "code": "unreachable", "message": err}
    assert base is not None
    # 延迟 import 避免 probe 被 wire 时循环拉起 provider (provider 只依赖标准库,
    # 但保险起见延后到函数内)。
    from miloco.perception.engine.omni.provider import get_adapter

    adapter = get_adapter(api_protocol, model)
    body = adapter.build_request_body(
        _visual_probe_messages(),
        model=model,
        max_tokens=16,
        temperature=0.0,
        top_p=1.0,
        stream=False,  # 请求非流式;adapter 若强制 stream=True (Qwen) 会覆盖
    )
    forced_stream = body.get("stream", False)
    url = adapter.endpoint(base, model, stream=forced_stream)
    # adapter.auth_headers 走 provider 特化 —— Gemini 用 ``x-goog-api-key`` 头,
    # OpenAI 兼容族用 ``Authorization: Bearer``。硬编码 Bearer 会对合法 Gemini
    # 配置误报失败(401)。
    headers = {
        **adapter.auth_headers(api_key),
        "Content-Type": "application/json",
    }
    t0 = time.monotonic()
    stream_safe_config_code: str | None = None
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            if forced_stream:
                # forced-stream provider (Qwen):走 SSE 流并用 adapter 累积输出文本，
                # 直到 [DONE] 或流结束后再验证视觉答案。任何 status/auth/model 错
                # 都在开 stream 时直接返回，与非流式行为对齐。
                (
                    status_code,
                    latency_ms,
                    output_text,
                    resp_headers,
                    stream_safe_config_code,
                ) = await _probe_stream_chat(client, url, headers, body, t0, adapter)
                if status_code == 200:
                    if not _visual_answer_is_red(output_text):
                        return {
                            "ok": False,
                            "code": "bad_response",
                            "status": 200,
                            "latency_ms": latency_ms,
                            "message": "视觉预检未识别合成图片",
                        }
                    return {
                        "ok": True,
                        "code": "ok",
                        "status": status_code,
                        "latency_ms": latency_ms,
                        "message": "视觉预检通过",
                    }
                # 非 200:复用下方 status_code 分支。headers 保留 Retry-After；
                # allow-listed auth/model 只通过 stream_safe_config_code 传递。
                r = _FakeStatusResp(status_code, {}, "", resp_headers)
            else:
                r = await client.post(  # type: ignore[assignment]
                    url,
                    headers=headers,
                    json=body,
                )
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "code": "unreachable",
            "message": f"无法连接 Base URL（{type(e).__name__}）",
        }
    latency_ms = round((time.monotonic() - t0) * 1000)
    if r.status_code == 200:
        # status 200 不代表 body 合法:mock/中间层可能返 200 + 非 JSON。运行时 omni_client
        # 走 json.loads + 非 dict → bad_response,probe 需对齐,否则 mock/异常网关下 probe
        # 误判 ok,熔断状态被 record_probe_result(True) 复位 CLOSED,与真实调用行为背离。
        try:
            normalized = adapter.parse_response(r.json())
            output_text = normalized["choices"][0]["message"]["content"]
        except (
            AttributeError,
            json.JSONDecodeError,
            IndexError,
            KeyError,
            TypeError,
            ValueError,
        ):
            output_text = None
        if not _visual_answer_is_red(output_text):
            return {
                "ok": False,
                "code": "bad_response",
                "status": 200,
                "latency_ms": latency_ms,
                "message": "视觉预检未识别合成图片",
            }
        return {
            "ok": True,
            "code": "ok",
            "status": 200,
            "latency_ms": latency_ms,
            "message": "视觉预检通过",
        }
    if r.status_code in (401, 403):
        return {
            "ok": False,
            "code": "bad_key",
            "status": r.status_code,
            "message": "API Key 无效或无权限",
        }
    if r.status_code == 404:
        return {
            "ok": False,
            "code": "not_found",
            "status": 404,
            "message": "模型或地址不存在",
        }
    if r.status_code in (400, 422):
        code = stream_safe_config_code or safe_provider_config_code(r)
        if code is not None:
            return {
                "ok": False,
                "code": code,
                "status": r.status_code,
                "latency_ms": latency_ms,
                "message": {
                    "bad_key": "API Key 无效或无权限",
                    "not_found": "模型或地址不存在",
                }[code],
            }
        return {
            "ok": False,
            "code": "visual_payload_rejected",
            "status": r.status_code,
            "latency_ms": latency_ms,
            "message": "端点可连接，但当前协议或视觉请求不受支持",
        }
    if r.status_code == 429:
        # 429 不加分支时会掉到 http_error 兜底,后果:上层用 http_error 走 _default cap
        # (600s)而非 rate_limited cap(60s),且丢 Retry-After header → backoff 无法尊重
        # server 明示的等待时长,可能过快复触发限流或过慢恢复。
        retry_after: float | None = None
        rah = r.headers.get("Retry-After")
        if rah:
            try:
                retry_after = float(rah)
            except ValueError:
                # HTTP-date 格式不解析,靠默认 backoff 兜底
                retry_after = None
        payload: dict[str, Any] = {
            "ok": False,
            "code": "rate_limited",
            "status": 429,
            "latency_ms": latency_ms,
            "message": "被 provider 限流",
        }
        if retry_after is not None:
            payload["retry_after_seconds"] = retry_after
        return payload
    return {
        "ok": False,
        "code": "http_error",
        "status": r.status_code,
        "message": f"服务返回异常（HTTP {r.status_code}）",
    }


def _retry_after_seconds(response: Any) -> float | None:
    retry_after = response.headers.get("Retry-After")
    if not retry_after:
        return None
    try:
        return float(retry_after)
    except ValueError:
        return None


def _probe_http_failure(
    response: Any,
    latency_ms: int,
    *,
    visual_request: bool = False,
) -> dict[str, Any]:
    status = response.status_code
    if status in (401, 403):
        return {
            "ok": False,
            "code": "bad_key",
            "status": status,
            "latency_ms": latency_ms,
            "message": "API Key 无效或无权限",
        }
    if status == 404:
        return {
            "ok": False,
            "code": "not_found",
            "status": status,
            "latency_ms": latency_ms,
            "message": "模型或地址不存在",
        }
    if status in (400, 422):
        code = safe_provider_config_code(response)
        if code is None:
            code = "visual_payload_rejected" if visual_request else "rejected_authed"
        return {
            "ok": False,
            "code": code,
            "status": status,
            "latency_ms": latency_ms,
            "message": {
                "bad_key": "API Key 无效或无权限",
                "not_found": "模型或地址不存在",
                "rejected_authed": "已连接，但请求被拒绝（模型名或 API Key 可能有误）",
                "visual_payload_rejected": "端点可连接，但当前协议或视觉请求不受支持",
            }[code],
        }
    if status == 429:
        result: dict[str, Any] = {
            "ok": False,
            "code": "rate_limited",
            "status": status,
            "latency_ms": latency_ms,
            "message": "被 provider 限流",
        }
        retry_after = _retry_after_seconds(response)
        if retry_after is not None:
            result["retry_after_seconds"] = retry_after
        return result
    return {
        "ok": False,
        "code": "http_error",
        "status": status,
        "latency_ms": latency_ms,
        "message": f"服务返回异常（HTTP {status}）",
    }


def _visual_probe_messages() -> list[dict[str, Any]]:
    image_data = base64.b64encode(_VISUAL_PROBE_IMAGE.read_bytes()).decode("ascii")
    return [
        {
            "role": "system",
            "content": "Answer with one short visual fact only.",
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _VISUAL_PROBE_PROMPT},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{image_data}",
                    },
                },
            ],
        },
    ]


async def _probe_responses(
    model: str,
    base: str,
    api_key: str,
) -> dict[str, Any]:
    from miloco.perception.engine.omni.provider import OpenAIResponsesAdapter

    adapter = OpenAIResponsesAdapter()
    auth_headers = adapter.auth_headers(api_key)
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            models_response = await client.get(
                f"{base}/models",
                headers=auth_headers,
            )
            if not (
                200 <= models_response.status_code < 300
                or models_response.status_code in (404, 405)
            ):
                return _probe_http_failure(
                    models_response,
                    round((time.monotonic() - t0) * 1000),
                    visual_request=False,
                )

            body = adapter.build_request_body(
                _visual_probe_messages(),
                model=model,
                max_tokens=_RESPONSES_VISUAL_MAX_OUTPUT_TOKENS,
                temperature=0.0,
                top_p=1.0,
                stream=False,
            )
            response = await client.post(
                adapter.endpoint(base, model, stream=False),
                headers={**auth_headers, "Content-Type": "application/json"},
                json=body,
                timeout=_RESPONSES_VISUAL_TIMEOUT,
            )
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "code": "unreachable",
            "message": f"无法连接 Base URL（{type(exc).__name__}）",
        }

    latency_ms = round((time.monotonic() - t0) * 1000)
    if response.status_code != 200:
        return _probe_http_failure(response, latency_ms, visual_request=True)

    try:
        raw = response.json()
    except Exception:  # noqa: BLE001
        raw = None
    if not isinstance(raw, dict):
        return {
            "ok": False,
            "code": "bad_response",
            "status": 200,
            "latency_ms": latency_ms,
            "message": "Responses 视觉预检响应格式异常",
        }

    usage_warning = raw.get("usage") is None
    try:
        normalized = adapter.parse_response(raw)
    except ValueError:
        without_usage = dict(raw)
        without_usage["usage"] = None
        try:
            normalized = adapter.parse_response(without_usage)
        except ValueError:
            return {
                "ok": False,
                "code": "bad_response",
                "status": 200,
                "latency_ms": latency_ms,
                "message": "Responses 视觉预检响应格式异常",
            }
        usage_warning = True

    output_text = normalized["choices"][0]["message"]["content"]
    if not _visual_answer_is_red(output_text):
        return {
            "ok": False,
            "code": "bad_response",
            "status": 200,
            "latency_ms": latency_ms,
            "message": "Responses 服务未通过图片颜色验证",
        }

    result: dict[str, Any] = {
        "ok": True,
        "code": "ok",
        "status": 200,
        "latency_ms": latency_ms,
        "message": "视觉预检通过",
    }
    if usage_warning:
        result["warning"] = "usage_unavailable"
    return result


async def probe_omni(
    model: str,
    base_url: str,
    api_key: str,
    api_protocol: OmniApiProtocol | None = None,
) -> dict[str, Any]:
    """协议定向探测，显式 Responses 使用真实图片验证视觉能力。

    Responses 先把 GET /models 当可选发现（仅 404/405 可忽略），再使用运行时
    OpenAIResponsesAdapter 将确定性红色 JPEG 发往 /responses，并要求 output_text
    包含 red。Chat/Gemini 保留原有两阶段/原生探测行为，不做协议回退。

    缺省 api_protocol 仅用于兼容 Task6 前仍以三参数调用的现有 admin/runtime 路径。
    """
    base, err = _normalize_base_url(base_url)
    if err is not None:
        return {"ok": False, "code": "unreachable", "message": err}
    assert base is not None
    # 延迟 import 避免顶层循环依赖。
    from miloco.perception.engine.omni.provider import (
        OpenAICompatAdapter,
        get_adapter,
    )

    adapter = get_adapter(api_protocol, model)
    if api_protocol == "openai_responses":
        return await _probe_responses(model, base, api_key)
    if not isinstance(adapter, OpenAICompatAdapter):
        return await probe_chat(model, base, api_key, api_protocol)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.get(
                f"{base}/models", headers={"Authorization": f"Bearer {api_key}"}
            )
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "code": "unreachable",
            "message": f"无法连接 Base URL（{type(e).__name__}）",
        }
    if r.status_code in (401, 403):
        return {
            "ok": False,
            "code": "bad_key",
            "status": r.status_code,
            "message": "API Key 无效或无权限",
        }
    if r.status_code >= 500:
        return {
            "ok": False,
            "code": "http_error",
            "status": r.status_code,
            "message": f"服务返回异常（HTTP {r.status_code}）",
        }
    return await probe_chat(model, base, api_key, api_protocol)
