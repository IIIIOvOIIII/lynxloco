"""omni provider 连通性探测。

统一被 web preflight (admin/router.py) 与运行时 circuit_breaker HALF_OPEN 复用。
两阶段探测：GET /models 验鉴权+可达；再用合成红色 JPEG 真校验视觉能力。

返回统一形状 {ok, code, status?, latency_ms?, message}。探测类 code 与 spec §2 的集合一致;
``fetch_models`` 另有一个 ``list_unsupported``,不参与熔断、不属于该集合(见 error_classifier
的 docstring),别因为在那边找不到就补进去。
"""

from __future__ import annotations

import base64
import io
import json
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import httpx
from PIL import Image

from miloco.perception.engine.omni.error_classifier import safe_provider_config_code

if TYPE_CHECKING:
    from miloco.config.settings import OmniApiProtocol
    from miloco.perception.engine.omni.provider import OmniProviderAdapter

_TIMEOUT = httpx.Timeout(15.0, connect=10.0)
_RESPONSES_VISUAL_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
_RESPONSES_STRUCTURED_TIMEOUT = httpx.Timeout(120.0, connect=10.0)
_RESPONSES_VISUAL_MAX_OUTPUT_TOKENS = 256
_RESPONSES_STRUCTURED_MAX_OUTPUT_TOKENS = 1024
_RESPONSES_VISUAL_MAX_ATTEMPTS = 2
_ALLOWED_SCHEMES = ("http", "https")
_VISUAL_PROBE_SIZE = (64, 64)
_RESPONSES_VISUAL_PROBES = (
    ("red", (255, 0, 0)),
    ("blue", (0, 0, 255)),
)
_VISUAL_PROBE_PROMPT = (
    "Look at the image. Return only this JSON object schema: "
    '{"dominant_color":"red_or_blue"}. The value must be exactly red or blue.'
)
_VISUAL_PROBE_FINAL_PROMPT = (
    "Output JSON now, no markdown, no explanation."
)
_STRUCTURED_PROBE_IMAGES = (
    (255, 0, 0),
    (0, 0, 255),
    (127, 127, 127),
    (127, 127, 127),
    (127, 127, 127),
    (127, 127, 127),
)
_STRUCTURED_PROBE_MESSAGE = "Responses 服务未通过结构化输出验证"
_STRUCTURED_PROBE_EMPTY_MESSAGE = "Responses 结构化预检返回空文本"
_VISUAL_PROBE_EMPTY_MESSAGE = "Responses 视觉预检返回空文本"

# Gemini 原生 generateContent 协议的两条官方接入路径:
#   generativelanguage.googleapis.com   —— Gemini API
#   {区域前缀-}aiplatform.googleapis.com —— Vertex AI
# **两台主机下都是原生协议与 OpenAI 兼容协议并存**,主机名一样,只能靠 path 区分:
#   Gemini API  原生 版本段如 /v1beta      兼容 /v1beta/openai
#   Vertex      原生 路径含 /publishers/   兼容 路径含 /endpoints/openapi
# 兼容那条走 ``Authorization: Bearer``;误判成原生会给它发 x-goog-api-key,被拒后报成
# 「Key 无效」——正是本模块要消除的那类误报。
# 两侧策略**故意不同**:
#   Gemini API 侧用黑名单(只排除含 openai 段的路径)—— 原生的版本段会随 API 版本新增,白名单要跟着改;
#   Vertex 侧用白名单(只放行含 publishers 段的路径)—— 该主机路径形态多,黑名单挡不住未知的非原生路径。
_GEMINI_NATIVE_HOST = "generativelanguage.googleapis.com"
_GEMINI_OPENAI_PATH_SEG = "openai"
_VERTEX_HOSTS = frozenset({"aiplatform.googleapis.com"})
_VERTEX_HOST_SUFFIX = "-aiplatform.googleapis.com"
_VERTEX_NATIVE_PATH_SEG = "publishers"


def _parse_model_ids(payload: Any, *, prefer_native: bool) -> list[str]:
    """从模型列表响应里抽 model id,认识的几种形态都试。

    认识三种形态:Gemini 原生 ``{models:[{name}]}``、Vertex 的 ``{publisherModels:[{name}]}``
    (name 形如 ``publishers/google/models/xxx``)、OpenAI 兼容 ``{data:[{id}]}``。
    前缀归一**按形态各配一条**:两种原生形态各剥各的前缀,兼容形态的 id 原样透传
    (含 ``/`` 合法,如 ``accounts/<org>/models/<name>``,截断后 provider 认不出来)。
    ``prefer_native`` 只决定**先试哪一种**,不再是唯一依据:绑死单一形态时,端点的形态一旦判错
    (鉴权仍通过、只是响应形态与预期不符)就会解出空列表,却仍按 200 成功返回。

    产出保证:恒为字符串列表且元素均非空——调用方据此可直接排序,不必再过滤。顶层非 dict、
    列表位不是 list、条目不是 dict、标识是嵌套结构(dict/list)一律跳过而非抛出;标识为数字等
    标量时转成字符串。
    """
    if not isinstance(payload, dict):
        return []

    def _pick(key: str, field: str, norm: Callable[[str], str]) -> list[str]:
        items = payload.get(key)
        if not isinstance(items, list):
            return []
        out: list[str] = []
        for m in items:
            if not isinstance(m, dict):
                continue
            raw = m.get(field)
            # 只对标量归一:标识为数字时转字符串,免得调用方排序混排类型抛异常。dict / list
            # 这类嵌套值 str() 之后是一串垃圾文本,会通过下面的非空过滤、被当成合法 model id
            # 收进下拉,故与其他异常形态一样跳过。bool 是 int 的子类,一并排除。
            if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
                continue
            # 归一规则**按形态配**,不能写死一条套到三种上:兼容形态的 id 里含 ``/models/``
            # 是合法的(如 ``accounts/<org>/models/<name>``),截断后 provider 认不出来。
            mid = norm(str(raw))
            if mid:
                out.append(mid)
        return out

    native = [
        # Gemini 原生:name 形如 ``models/<id>``
        ("models", "name", lambda v: v.removeprefix("models/")),
        # Vertex:name 形如 ``publishers/<pub>/models/<id>``
        ("publisherModels", "name", lambda v: v.rsplit("/models/", 1)[-1]),
    ]
    # OpenAI 兼容:id 原样透传——含 ``/`` 是合法的,不做任何剥离
    compat = [("data", "id", lambda v: v)]
    for key, field, norm in (native + compat if prefer_native else compat + native):
        ids = _pick(key, field, norm)
        if ids:
            return ids
    return []


def _is_gemini_native_endpoint(base: str) -> bool:
    """按**主机名 + 路径**判是否 Gemini 原生 generateContent 端点。

    主机名取 ``urlparse().hostname`` 比对,不用 ``"…" in base_url`` 子串匹配——子串匹配会被
    ``https://evil.com/generativelanguage.googleapis.com`` 之类绕过(CodeQL 报的 incomplete
    URL substring sanitization)。两台主机都还要再过一道路径判断,用途见上方注释。
    """
    parsed = urlparse(base)
    host = (parsed.hostname or "").lower()
    # 按**路径段**比对而非子串:尾锚定会漏掉 ``…/openai/v1``(不少工具要求 base 以 /v1 结尾),
    # 裸子串又会把 ``…/myopenai`` 之类误判。两侧同一写法,不再一边尾锚定一边包含。
    # 与主机名一样做小写归一:URL 路径按 RFC 大小写敏感,但判定不该因大小写差异漏判——
    # 漏判的后果是给兼容端点发原生鉴权头,正是本模块要消除的那类误报。
    segments = parsed.path.lower().strip("/").split("/")
    if host == _GEMINI_NATIVE_HOST:
        return _GEMINI_OPENAI_PATH_SEG not in segments
    if host in _VERTEX_HOSTS or host.endswith(_VERTEX_HOST_SUFFIX):
        return _VERTEX_NATIVE_PATH_SEG in segments
    return False


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
    - Gemini 原生端点的 404 → None(见下方注释)
    - 其他 4xx/5xx → {code: http_error, ...}

    **已知代价**:原生端点的 404 一律放行,而路径写错同样回 404,两者仅凭状态码分不开。
    于是同族主机上的路径笔误(如版本段敲多一个字母)在**未填 Key 阶段**会被上层的「缺 key」
    盖住,提示里一个字都不提地址。这是本函数契约的直接推论——它的既定职责就是让「缺 key」
    优先于地址错暴露,收紧就得牺牲那个契约。影响只到填 Key 之前:填上 Key 后走拉模型列表
    那条路,拿到的是并列「端点没有此方法」与「路径可能写错」两种可能的提示。
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
    # Gemini 原生端点未必都有 models.list,404 只说明这条路径没有该方法,不代表
    # Base URL 写错;判 http_error 会让可用配置在「缺 key」之前先被报成地址错。
    if r.status_code == 404 and _is_gemini_native_endpoint(base):
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
        # 解析与排序一起放进 try:响应体不是 JSON 时 r.json() 会抛,逃出去会让本接口 500。
        # 排序本身靠 _parse_model_ids「恒产非空字符串」的产出保证不抛,一并纳入只是防后续改动。
        try:
            models = sorted(_parse_model_ids(r.json(), prefer_native=is_gemini))
        except Exception:  # noqa: BLE001
            models = []
        return {"ok": True, "models": models}
    if api_protocol == "openai_responses" and r.status_code in (404, 405):
        return {"ok": True, "code": "unsupported", "models": [], "message": "该协议不支持模型发现，请手动填写模型"}
    if r.status_code == 404 and is_gemini:
        # 无法仅凭 404 区分「该端点没有 models.list」与「Base URL 路径写错」,故文案并列
        # 两种可能,不把不确定说成确定。
        return {
            "ok": False,
            "code": "list_unsupported",
            "models": [],
            "message": "该 Base URL 未返回模型列表，请手动填写模型名（并确认 Base URL 路径无误）",
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
    return _visual_answer_is(value, "red")


def _visual_answer_is(value: Any, expected: str) -> bool:
    if not isinstance(value, str):
        return False
    text = value.casefold().strip().rstrip(".")
    if text == expected.casefold():
        return True

    try:
        from miloco.perception.engine.omni.response_parser import extract_json

        parsed = json.loads(extract_json(value))
    except (json.JSONDecodeError, ValueError, TypeError):
        return False
    if not isinstance(parsed, dict):
        return False
    color = parsed.get("dominant_color")
    return (
        isinstance(color, str)
        and color.casefold().strip().rstrip(".") == expected.casefold()
    )


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
    from miloco.perception.engine.omni.provider import (
        build_request_headers,
        get_adapter,
    )

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
    # 统一走 provider 特化鉴权，并确保 probe 与运行时发送相同的附加头。
    headers = build_request_headers(adapter, base, api_key)
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


def _visual_probe_data_url(rgb: tuple[int, int, int]) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", _VISUAL_PROBE_SIZE, rgb).save(buffer, format="JPEG", quality=95)
    image_data = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{image_data}"


def _visual_probe_messages(rgb: tuple[int, int, int] = (255, 0, 0)) -> list[dict[str, Any]]:
    return [
        {
            "role": "system",
            "content": "You are Miloco vision preflight. Return compact JSON only.",
        },
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _VISUAL_PROBE_PROMPT},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": _visual_probe_data_url(rgb),
                    },
                },
                {"type": "text", "text": _VISUAL_PROBE_FINAL_PROMPT},
            ],
        },
    ]


def _structured_probe_messages() -> list[dict[str, Any]]:
    """Build a lightweight Miloco-like structured probe for Responses endpoints.

    A color-only probe can prove image ingestion but still miss qwen-like models
    that spend the whole output budget on reasoning for the real Miloco schema.
    This probe deliberately keeps the same bounded JPEG input shape and a small
    subset of the runtime output schema, then requires parseable visible text.
    """
    from miloco.perception.engine.omni.field_registry import (
        SceneDescriptor,
        render_field_spec,
        render_schema,
    )

    scene = SceneDescriptor(
        route="video",
        has_identity=False,
        stream=False,
        has_audio=False,
        has_speech=False,
        has_pets=False,
    )
    text = (
        "Miloco runtime compatibility probe. Return only a JSON object, "
        "no markdown.\n"
        "The image sequence contains a red test card, a blue test card, "
        "and gray neutral frames.\n"
        "Use this exact schema and field meanings from the real Miloco "
        "runtime.\n"
        f"# 输出格式\n{render_schema(scene)}\n\n"
        f"# 字段说明\n{render_field_spec(scene)}\n\n"
        "Required facts: caption must mention red and blue test cards; "
        "matched_rules must be []; suggestions must be []."
    )
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    content.extend(
        {"type": "image_url", "image_url": {"url": _visual_probe_data_url(rgb)}}
        for rgb in _STRUCTURED_PROBE_IMAGES
    )
    content.append(
        {
            "type": "text",
            "text": (
                "Final instruction after the image inputs: output only one "
                "compact JSON object now, with no markdown or reasoning text."
            ),
        }
    )
    return [
        {
            "role": "system",
            "content": (
                "You are Miloco preflight. Produce concise "
                "machine-readable perception JSON only."
            ),
        },
        {"role": "user", "content": content},
    ]


def _bad_responses_probe(message: str, latency_ms: int) -> dict[str, Any]:
    return {
        "ok": False,
        "code": "bad_response",
        "status": 200,
        "latency_ms": latency_ms,
        "message": message,
    }


def _responses_has_blank_output_text(raw: dict[str, Any]) -> bool:
    """Return True when Responses carried output_text parts but all were blank."""
    output = raw.get("output")
    if not isinstance(output, list):
        return False

    found_output_text = False
    text_parts: list[str] = []
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            found_output_text = True
            text = part.get("text")
            if isinstance(text, str):
                text_parts.append(text)
    return found_output_text and not "".join(text_parts).strip()


def _responses_output_text(
    adapter: "OmniProviderAdapter",
    response: Any,
    latency_ms: int,
    *,
    bad_response_message: str,
    blank_output_message: str | None = None,
) -> tuple[str | None, dict[str, Any] | None, bool]:
    try:
        raw = response.json()
    except Exception:  # noqa: BLE001
        raw = None
    if not isinstance(raw, dict):
        return None, _bad_responses_probe(bad_response_message, latency_ms), False

    usage_warning = raw.get("usage") is None
    try:
        normalized = adapter.parse_response(raw)
    except ValueError:
        without_usage = dict(raw)
        without_usage["usage"] = None
        try:
            normalized = adapter.parse_response(without_usage)
        except ValueError:
            message = (
                blank_output_message
                if blank_output_message and _responses_has_blank_output_text(raw)
                else bad_response_message
            )
            return None, _bad_responses_probe(message, latency_ms), False
        usage_warning = True

    return normalized["choices"][0]["message"]["content"], None, usage_warning


def _structured_output_is_valid(output_text: str) -> bool:
    from miloco.perception.engine.omni.response_parser import extract_json

    try:
        parsed = json.loads(extract_json(output_text))
    except (json.JSONDecodeError, ValueError, TypeError):
        return False
    if not isinstance(parsed, dict):
        return False
    return (
        isinstance(parsed.get("caption"), str)
        and isinstance(parsed.get("matched_rules"), list)
        and isinstance(parsed.get("suggestions"), list)
    )


async def _probe_responses(
    model: str,
    base: str,
    api_key: str,
) -> dict[str, Any]:
    from miloco.perception.engine.omni.provider import OpenAIResponsesAdapter

    adapter = OpenAIResponsesAdapter()
    auth_headers = adapter.auth_headers(api_key)
    t0 = time.monotonic()
    usage_warning = False
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

            for expected_color, rgb in _RESPONSES_VISUAL_PROBES:
                matched_expected_color = False
                for _attempt in range(_RESPONSES_VISUAL_MAX_ATTEMPTS):
                    body = adapter.build_request_body(
                        _visual_probe_messages(rgb),
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
                    if response.status_code != 200:
                        return _probe_http_failure(
                            response,
                            round((time.monotonic() - t0) * 1000),
                            visual_request=True,
                        )

                    output_text, error, warn = _responses_output_text(
                        adapter,
                        response,
                        round((time.monotonic() - t0) * 1000),
                        bad_response_message="Responses 视觉预检响应格式异常",
                        blank_output_message=_VISUAL_PROBE_EMPTY_MESSAGE,
                    )
                    if error is not None:
                        return error
                    assert output_text is not None
                    usage_warning = usage_warning or warn

                    if _visual_answer_is(output_text, expected_color):
                        matched_expected_color = True
                        break

                if not matched_expected_color:
                    return {
                        "ok": False,
                        "code": "bad_response",
                        "status": 200,
                        "latency_ms": round((time.monotonic() - t0) * 1000),
                        "message": "Responses 服务未通过图片颜色验证",
                    }

            body = adapter.build_request_body(
                _structured_probe_messages(),
                model=model,
                max_tokens=_RESPONSES_STRUCTURED_MAX_OUTPUT_TOKENS,
                temperature=0.0,
                top_p=1.0,
                stream=False,
            )
            structured_response = await client.post(
                adapter.endpoint(base, model, stream=False),
                headers={**auth_headers, "Content-Type": "application/json"},
                json=body,
                timeout=_RESPONSES_STRUCTURED_TIMEOUT,
            )
            latency_ms = round((time.monotonic() - t0) * 1000)
            if structured_response.status_code != 200:
                return _probe_http_failure(
                    structured_response,
                    latency_ms,
                    visual_request=True,
                )
            output_text, error, warn = _responses_output_text(
                adapter,
                structured_response,
                latency_ms,
                bad_response_message=_STRUCTURED_PROBE_MESSAGE,
                blank_output_message=_STRUCTURED_PROBE_EMPTY_MESSAGE,
            )
            if error is not None:
                return error
            assert output_text is not None
            usage_warning = usage_warning or warn
            if not _structured_output_is_valid(output_text):
                return _bad_responses_probe(_STRUCTURED_PROBE_MESSAGE, latency_ms)
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "code": "unreachable",
            "message": f"无法连接 Base URL（{type(exc).__name__}）",
        }

    latency_ms = round((time.monotonic() - t0) * 1000)
    result: dict[str, Any] = {
        "ok": True,
        "code": "ok",
        "status": 200,
        "latency_ms": latency_ms,
        "message": "视觉与结构化输出预检通过",
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
        # 同 fetch_models:预检刻意只发 Authorization、**不**带 x-opencode-session。
        # 这里的 model 已经能拿到 adapter,但 GET /models 只问「这个 endpoint 认不认我的
        # key」,不是一次推理,session 头(运行时按 endpoint 派生的进程级路由亲和 ID)在这
        # 里没有作用;真校验模型的 probe_chat 才按运行时同头发。别"顺手收敛"成半对齐。
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
