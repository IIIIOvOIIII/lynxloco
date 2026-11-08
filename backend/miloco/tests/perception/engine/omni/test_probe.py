"""probe.py 单元测试。用 monkeypatch 替换 httpx.AsyncClient 走 fake 响应。"""

from __future__ import annotations

import base64
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from miloco.perception.engine.omni import probe
from miloco.perception.engine.omni.provider import build_request_headers, get_adapter
from PIL import Image


@pytest.fixture
def recording_http_server():
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def _record(self):
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            requests.append(
                {
                    "method": self.command,
                    "path": self.path,
                    "headers": dict(self.headers),
                    "body": json.loads(body) if body else None,
                }
            )

        def _json(self, payload: dict, status: int = 200):
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802
            self._record()
            if self.path == "/v1/models":
                self._json({"data": [{"id": "gemini-named-local-model"}]})
            else:
                self._json({}, 404)

        def do_POST(self):  # noqa: N802
            self._record()
            if self.path == "/v1/chat/completions":
                self._json({"choices": [{"message": {"content": "red"}}]})
            else:
                self._json({}, 404)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/v1", requests
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


class _FakeResp:
    def __init__(
        self,
        status_code: int,
        json_data: object | None = None,
        text: str = "",
        headers: dict[str, str] | None = None,
    ):
        self.status_code = status_code
        self._json: object = json_data if json_data is not None else {}
        self.text = text
        self.headers = httpx.Headers(headers or {})

    def json(self):
        return self._json


def _fake_async_client(
    resp: object | None = None,
    *,
    exc: Exception | None = None,
    get_exc: Exception | None = None,
    post_exc: Exception | None = None,
    get_resp: _FakeResp | None = None,
    post_resp: _FakeResp | None = None,
    calls: list[tuple[str, str, dict]] | None = None,
    seen: dict | None = None,
    sent: list[dict] | None = None,
):
    g = get_resp if get_resp is not None else resp
    p = post_resp if post_resp is not None else resp

    class _C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            if seen is not None:
                seen["url"] = a[0] if a else k.get("url")
                seen["headers"] = k.get("headers") or {}
            if calls is not None:
                calls.append(("GET", a[0], k))
            raised = get_exc if get_exc is not None else exc
            if raised is not None:
                raise raised
            return g

        async def post(self, *a, **k):
            if sent is not None:
                sent.append({"url": a[0] if a else k.get("url"), **k})
            if calls is not None:
                calls.append(("POST", a[0], k))
            raised = post_exc if post_exc is not None else exc
            if raised is not None:
                raise raised
            return p

    return _C


def _fake_async_client_post_sequence(
    *,
    get_resp: _FakeResp,
    post_resps: list[_FakeResp],
    calls: list[tuple[str, str, dict]] | None = None,
):
    class _C:
        def __init__(self, *a, **k):
            self._post_resps = list(post_resps)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            if calls is not None:
                calls.append(("GET", a[0], k))
            return get_resp

        async def post(self, *a, **k):
            if calls is not None:
                calls.append(("POST", a[0], k))
            return self._post_resps.pop(0)

    return _C


# ─── probe_reachable ────────────────────────────────────────────────────────


async def test_probe_reachable_returns_none_on_200(monkeypatch):
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(resp=_FakeResp(200, {"data": []})),
    )
    assert await probe.probe_reachable("https://ok.example/v1") is None


async def test_probe_reachable_returns_none_on_401(monkeypatch):
    """401 表示"地址对、只是需 key",不算 URL 错。"""
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(401))
    )
    assert await probe.probe_reachable("https://ok.example/v1") is None


async def test_probe_reachable_unreachable_on_connect_error(monkeypatch):
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(exc=httpx.ConnectError("dns fail")),
    )
    r = await probe.probe_reachable("https://nope.example/v1")
    assert r == {"code": "unreachable", "message": "无法连接 Base URL（ConnectError）"}


async def test_probe_reachable_http_error_on_404(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(404))
    )
    r = await probe.probe_reachable("https://ok.example/v1")
    assert r == {"code": "http_error", "message": "服务返回异常（HTTP 404）"}


# ─── fetch_models ───────────────────────────────────────────────────────────


async def test_fetch_models_ok(monkeypatch):
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(resp=_FakeResp(200, {"data": [{"id": "m1"}, {"id": "m2"}]})),
    )
    r = await probe.fetch_models("https://ok/v1", "sk-x", "openai_chat_completions")
    assert r == {"ok": True, "models": ["m1", "m2"]}


async def test_fetch_models_bad_key_on_401(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(401))
    )
    r = await probe.fetch_models("https://ok/v1", "sk-x", "openai_chat_completions")
    assert r["ok"] is False and r["code"] == "bad_key"


async def test_fetch_models_unreachable_on_exception(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(exc=httpx.ConnectError("nope"))
    )
    r = await probe.fetch_models("https://ok/v1", "sk-x", "openai_chat_completions")
    assert r["ok"] is False and r["code"] == "unreachable"


async def test_fetch_models_chat_protocol_overrides_gemini_hostname(monkeypatch):
    """显式 Chat 协议必须决定鉴权和解析，URL 主机名不得覆盖它。"""
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            resp=_FakeResp(200, {"data": [{"id": "openai-shape"}]}),
            calls=calls,
        ),
    )

    result = await probe.fetch_models(
        "https://generativelanguage.googleapis.com/v1beta",
        "chat-key",
        "openai_chat_completions",
    )

    assert result == {"ok": True, "models": ["openai-shape"]}
    headers = calls[0][2]["headers"]
    assert headers == {"Authorization": "Bearer chat-key"}


async def test_fetch_models_gemini_protocol_overrides_arbitrary_hostname(monkeypatch):
    """显式 Gemini 协议在任意代理 URL 上仍使用 Gemini 鉴权和响应形状。"""
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            resp=_FakeResp(200, {"models": [{"name": "models/gemini-proxy"}]}),
            calls=calls,
        ),
    )

    result = await probe.fetch_models(
        "https://proxy.example/v1beta",
        "gemini-key",
        "gemini_native",
    )

    assert result == {"ok": True, "models": ["gemini-proxy"]}
    headers = calls[0][2]["headers"]
    assert headers == {"x-goog-api-key": "gemini-key"}


@pytest.mark.parametrize("status", [404, 405])
async def test_fetch_models_responses_treats_missing_endpoint_as_unsupported(
    monkeypatch, status
):
    """Responses 可没有 /models；稳定返回空发现结果，让用户继续手填并做视觉预检。"""
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(resp=_FakeResp(status), calls=calls),
    )

    result = await probe.fetch_models(
        "https://responses.example/v1",
        "",
        "openai_responses",
    )

    assert result == {
        "ok": True,
        "code": "unsupported",
        "models": [],
        "message": "该协议不支持模型发现，请手动填写模型",
    }
    assert calls[0][2]["headers"] == {}


async def test_fetch_models_responses_adds_bearer_only_when_key_present(monkeypatch):
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(resp=_FakeResp(200, {"data": []}), calls=calls),
    )

    await probe.fetch_models(
        "https://responses.example/v1",
        "responses-key",
        "openai_responses",
    )

    assert calls[0][2]["headers"] == {"Authorization": "Bearer responses-key"}


# ─── probe_chat ─────────────────────────────────────────────────────────────


async def test_probe_chat_ok(monkeypatch):
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            resp=_FakeResp(200, {"choices": [{"message": {"content": "red"}}]})
        ),
    )
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x", api_protocol="openai_chat_completions")
    assert r["ok"] is True and r["code"] == "ok" and r["status"] == 200
    assert "latency_ms" in r


async def test_probe_chat_bad_key(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(401))
    )
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x", api_protocol="openai_chat_completions")
    assert r["ok"] is False and r["code"] == "bad_key" and r["status"] == 401


async def test_probe_chat_not_found(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(404))
    )
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x", api_protocol="openai_chat_completions")
    assert r["code"] == "not_found" and r["status"] == 404


async def test_probe_chat_visual_400_is_visual_payload_rejected(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(400))
    )
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x", api_protocol="openai_chat_completions")
    assert r["code"] == "visual_payload_rejected"
    assert "API Key" not in r["message"]


async def test_probe_chat_visual_422_is_visual_payload_rejected(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(422))
    )
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x", api_protocol="openai_chat_completions")
    assert r["code"] == "visual_payload_rejected"


@pytest.mark.parametrize(
    ("provider_code", "expected"),
    [
        ("invalid_api_key", "bad_key"),
        ("model_not_found", "not_found"),
    ],
)
async def test_probe_chat_structured_config_code_overrides_visual_fallback(
    monkeypatch, provider_code, expected
):
    """Removing the safe override would hide known auth/model failures."""
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            resp=_FakeResp(
                400,
                {
                    "error": {
                        "code": provider_code,
                        "message": "RAW_PROVIDER_SECRET data:image",
                    }
                },
            )
        ),
    )

    result = await probe.probe_chat("m1", "https://ok/v1", "sk-x", api_protocol="openai_chat_completions")

    assert result["code"] == expected
    assert "RAW_PROVIDER_SECRET" not in result["message"]
    assert "data:image" not in result["message"]


async def test_probe_chat_http_error_on_500(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(500))
    )
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x", api_protocol="openai_chat_completions")
    assert r["code"] == "http_error"


async def test_probe_chat_unreachable_on_exception(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(exc=httpx.ReadTimeout("slow"))
    )
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x", api_protocol="openai_chat_completions")
    assert r["code"] == "unreachable"


async def test_probe_chat_bad_response_on_json_decode_error(monkeypatch):
    """status=200 但 body 非 JSON → bad_response(而非误判 ok)。"""
    import json as _json

    class _Bad200:
        status_code = 200
        text = "not a json body"

        def json(self):
            raise _json.JSONDecodeError("Expecting value", "", 0)

    monkeypatch.setattr(probe.httpx, "AsyncClient", _fake_async_client(resp=_Bad200()))
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x", api_protocol="openai_chat_completions")
    assert r["ok"] is False
    assert r["code"] == "bad_response"
    assert r["status"] == 200


async def test_probe_chat_bad_response_on_non_dict_body(monkeypatch):
    """status=200 但 body 是 list/非 dict → bad_response。"""
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(resp=_FakeResp(200, json_data=["not", "a", "dict"])),
    )
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x", api_protocol="openai_chat_completions")
    assert r["ok"] is False
    assert r["code"] == "bad_response"
    assert r["status"] == 200


# ─── scheme 白名单(防 SSRF) ───────────────────────────────────────────────


async def test_probe_reachable_rejects_file_scheme():
    """file:// 被拒,不发 HTTP;不需要 mock httpx 因为压根不会调。"""
    r = await probe.probe_reachable("file:///etc/passwd")
    assert r == {
        "code": "unreachable",
        "message": "Base URL 协议非法（仅支持 http/https，实际: file）",
    }


async def test_probe_reachable_rejects_gopher_scheme():
    r = await probe.probe_reachable("gopher://evil/x")
    assert r["code"] == "unreachable" and "gopher" in r["message"]


async def test_probe_chat_rejects_file_scheme():
    r = await probe.probe_chat("m", "file:///etc/passwd", "sk-x", api_protocol="openai_chat_completions")
    assert r["ok"] is False and r["code"] == "unreachable"


async def test_probe_omni_rejects_file_scheme():
    r = await probe.probe_omni("m", "file:///etc/passwd", "sk-x", api_protocol="openai_chat_completions")
    assert r["ok"] is False and r["code"] == "unreachable"


async def test_fetch_models_rejects_ftp_scheme():
    r = await probe.fetch_models("ftp://x/y", "sk-x", "openai_chat_completions")
    assert r["ok"] is False and r["code"] == "unreachable" and r["models"] == []


async def test_probe_reachable_rejects_empty_host():
    r = await probe.probe_reachable("https:///")
    assert r["code"] == "unreachable" and "主机名" in r["message"]


# ─── probe_omni (两阶段) ────────────────────────────────────────────────────


async def test_probe_omni_get_401_short_circuits_to_bad_key(monkeypatch):
    """GET /models 401 立刻判 bad_key,不走 chat。"""
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(get_resp=_FakeResp(401), post_resp=_FakeResp(200)),
    )
    r = await probe.probe_omni("m1", "https://ok/v1", "sk-x", api_protocol="openai_chat_completions")
    assert r["code"] == "bad_key"


async def test_probe_omni_get_500_short_circuits_to_http_error(monkeypatch):
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(get_resp=_FakeResp(500), post_resp=_FakeResp(200)),
    )
    r = await probe.probe_omni("m1", "https://ok/v1", "sk-x", api_protocol="openai_chat_completions")
    assert r["code"] == "http_error"


async def test_probe_omni_get_ok_then_chat_ok(monkeypatch):
    """GET /models 200 后调 chat,chat 200 → ok。"""
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            get_resp=_FakeResp(200, {"data": [{"id": "m1"}]}),
            post_resp=_FakeResp(
                200, {"choices": [{"message": {"content": "red"}}]}
            ),
        ),
    )
    r = await probe.probe_omni("m1", "https://ok/v1", "sk-x", api_protocol="openai_chat_completions")
    assert r["ok"] is True and r["code"] == "ok"


async def test_probe_omni_get_ok_then_chat_not_found(monkeypatch):
    """模型不在列表但 GET 200:走 chat,chat 404 → not_found。"""
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            get_resp=_FakeResp(200, {"data": [{"id": "other"}]}),
            post_resp=_FakeResp(404),
        ),
    )
    r = await probe.probe_omni("m1", "https://ok/v1", "sk-x", api_protocol="openai_chat_completions")
    assert r["code"] == "not_found"


async def test_probe_omni_connect_error(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(exc=httpx.ConnectError("nope"))
    )
    r = await probe.probe_omni("m1", "https://nope/v1", "sk-x", api_protocol="openai_chat_completions")
    assert r["code"] == "unreachable"


# ─── probe_chat × provider adapter (review #3 回归) ─────────────────────────


class _FakeStreamResp:
    """模拟 client.stream() 返回的 async context manager。

    headers 存 httpx.Headers 而非 plain dict,精确复现生产路径:
    _probe_stream_chat 里 dict(resp.headers) 会把 httpx.Headers 里的 key 全部小写化
    (httpx 0.28.1: dict(Headers({"Retry-After": "45"})) == {"retry-after": "45"});
    若测试 fake 用 plain dict 装原大小写 key,dict() 会保留原样,反而绕过生产路径的
    小写化,让「不区分大小写」相关的 bug 不能被回归测试守住。
    """

    def __init__(
        self,
        status_code: int,
        lines: list[str] | None = None,
        headers: httpx.Headers | dict[str, str] | None = None,
        json_data: object | None = None,
    ):
        self.status_code = status_code
        self._lines = lines or []
        self._json = json_data if json_data is not None else {}
        # 强制包成 httpx.Headers,即使传入 plain dict 也走真实 httpx 语义
        self.headers = httpx.Headers(headers or {})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""

    def json(self):
        return self._json


def _fake_stream_client(get_resp, stream_resp):
    class _C:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, *a, **k):
            return get_resp

        async def post(self, *a, **k):
            raise AssertionError("forced-stream should not call POST")

        def stream(self, *a, **k):
            return stream_resp

    return _C


async def test_probe_chat_uses_adapter_body_for_qwen(monkeypatch):
    """review #3 回归:Qwen adapter forced stream=True + modalities=["text"],
    probe_chat 必须走 SSE 流不是硬编码非流式 POST。原实现固定发非流式 body,合法
    Qwen 配置会被 400/422 判成 rejected_authed → OPEN_CONFIG,用户被卡死。"""
    stream_resp = _FakeStreamResp(
        200,
        lines=[
            'data: {"choices":[{"delta":{"content":"red"}}]}',
            "data: [DONE]",
        ],
    )
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_stream_client(
            _FakeResp(200, {"data": [{"id": "qwen-omni"}]}), stream_resp
        ),
    )
    r = await probe.probe_omni("qwen3.5-omni-plus", "https://qwen.example/v1", "sk-x", api_protocol="openai_chat_completions")
    assert r["ok"] is True
    assert r["code"] == "ok"


async def test_probe_chat_stream_401_maps_to_bad_key(monkeypatch):
    """forced-stream 路径撞 401 也要正常走 bad_key 分类,不能因为走了流式就丢掉状态码。"""
    stream_resp = _FakeStreamResp(401, lines=[])
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_stream_client(_FakeResp(200, {"data": []}), stream_resp),
    )
    r = await probe.probe_omni("qwen3.5-omni-plus", "https://qwen.example/v1", "sk-x", api_protocol="openai_chat_completions")
    assert r["ok"] is False
    assert r["code"] == "bad_key"


async def test_probe_chat_non_qwen_still_uses_post(monkeypatch):
    """回归防护:非 Qwen 模型 (MiMo 默认) 仍走非流式 POST,行为未变。"""
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            get_resp=_FakeResp(200, {"data": [{"id": "xiaomi/mimo-v2.5"}]}),
            post_resp=_FakeResp(200, {"choices": [{"message": {"content": "red"}}]}),
        ),
    )
    r = await probe.probe_omni("xiaomi/mimo-v2.5", "https://mimo.example/v1", "sk-x", api_protocol="openai_chat_completions")
    assert r["ok"] is True


async def test_probe_chat_stream_429_preserves_retry_after(monkeypatch):
    """review 🟡 回归:Qwen 撞 429 时 forced-stream 路径必须回传 Retry-After header,
    不然熔断退避走纯指数(early 12s vs server 说的 45s),对着限流的 Qwen 反复打 429、
    拖慢恢复。修复前 _probe_stream_chat 只返 (status, latency, ok),headers 恒空。"""
    stream_resp = _FakeStreamResp(429, lines=[], headers={"Retry-After": "45"})
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_stream_client(
            _FakeResp(200, {"data": [{"id": "qwen-omni"}]}), stream_resp
        ),
    )
    r = await probe.probe_omni("qwen3.5-omni-plus", "https://qwen.example/v1", "sk-x", api_protocol="openai_chat_completions")
    assert r["ok"] is False
    assert r["code"] == "rate_limited"
    # 关键:Retry-After 被解析出来传给上层 _grow_backoff_locked
    assert r["retry_after_seconds"] == 45.0


@pytest.mark.parametrize(
    ("status", "json_body", "expected"),
    [
        (
            400,
            {
                "error": {
                    "code": "invalid_api_key",
                    "message": "FORCED_STREAM_RAW_SECRET data:image",
                }
            },
            "bad_key",
        ),
        (
            422,
            {
                "error": {
                    "type": "model_not_found",
                    "message": "FORCED_STREAM_RAW_SECRET data:image",
                }
            },
            "not_found",
        ),
    ],
)
async def test_probe_chat_stream_preserves_only_safe_structured_config_code(
    monkeypatch, caplog, status, json_body, expected
):
    """Discarding the drained error body hides known auth/model failures."""
    stream_resp = _FakeStreamResp(status, json_data=json_body)
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_stream_client(_FakeResp(200, {"data": []}), stream_resp),
    )

    result = await probe.probe_omni(
        "qwen3.5-omni-plus",
        "https://qwen.example/v1",
        "sk-x",
        "openai_chat_completions",
    )

    assert result["ok"] is False
    assert result["code"] == expected
    assert "FORCED_STREAM_RAW_SECRET" not in result["message"]
    assert "data:image" not in result["message"]
    assert "FORCED_STREAM_RAW_SECRET" not in caplog.text
    assert "data:image" not in caplog.text


@pytest.mark.parametrize(
    ("protocol", "model", "expected_path"),
    [
        ("openai_chat_completions", "xiaomi/mimo-v2.5", "/v1/chat/completions"),
        ("gemini_native", "gemini-vision", "/models/gemini-vision:generateContent"),
    ],
)
async def test_non_responses_probe_uses_the_synthetic_red_image(
    monkeypatch, caplog, protocol, model, expected_path
):
    """Removing the image block must fail because reachability alone is not visual proof."""
    calls: list[tuple[str, str, dict]] = []
    payload = (
        {"choices": [{"message": {"content": "red"}}]}
        if protocol == "openai_chat_completions"
        else {"candidates": [{"content": {"parts": [{"text": "red"}]}}]}
    )
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            get_resp=_FakeResp(200, {"data": [{"id": model}]}),
            post_resp=_FakeResp(200, payload),
            calls=calls,
        ),
    )

    result = await probe.probe_omni(
        model, "https://vlm.example/v1", "sk-secret", protocol
    )

    assert result["ok"] is True
    post = next(call for call in calls if call[0] == "POST")
    assert post[1].endswith(expected_path)
    assert "image" in json.dumps(post[2]["json"])
    assert "sk-secret" not in result["message"]
    assert "sk-secret" not in caplog.text
    assert "data:image" not in caplog.text


@pytest.mark.parametrize(
    ("protocol", "model", "payload"),
    [
        (
            "openai_chat_completions",
            "xiaomi/mimo-v2.5",
            {"choices": [{"message": {"content": "Request acknowledged."}}]},
        ),
        (
            "gemini_native",
            "gemini-vision",
            {
                "candidates": [
                    {"content": {"parts": [{"text": "Request acknowledged."}]}}
                ]
            },
        ),
    ],
)
async def test_non_responses_probe_rejects_text_only_acknowledgement(
    monkeypatch, protocol, model, payload
):
    """Accepting an arbitrary 200 response would let text-only endpoints go green."""
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            get_resp=_FakeResp(200, {"data": [{"id": model}]}),
            post_resp=_FakeResp(200, payload),
        ),
    )

    result = await probe.probe_omni(
        model, "https://vlm.example/v1", "sk-secret", protocol
    )

    assert result["ok"] is False
    assert result["code"] == "bad_response"


async def test_forced_stream_probe_rejects_non_red_answer(monkeypatch):
    """Accepting the first SSE data line would hide a non-visual Qwen response."""
    stream_resp = _FakeStreamResp(
        200,
        lines=[
            'data: {"choices":[{"delta":{"content":"acknowledged"}}]}',
            "data: [DONE]",
        ],
    )
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_stream_client(_FakeResp(200, {"data": []}), stream_resp),
    )

    result = await probe.probe_omni(
        "qwen3.5-omni-plus",
        "https://qwen.example/v1",
        "sk-x",
        "openai_chat_completions",
    )

    assert result["ok"] is False
    assert result["code"] == "bad_response"


async def test_forced_stream_probe_rejects_malformed_sse_json(monkeypatch):
    """Malformed provider SSE is a bad response, not a reachability success."""
    stream_resp = _FakeStreamResp(200, lines=["data: {not-json", "data: [DONE]"])
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_stream_client(_FakeResp(200, {"data": []}), stream_resp),
    )

    result = await probe.probe_omni(
        "qwen3.5-omni-plus",
        "https://qwen.example/v1",
        "sk-x",
        "openai_chat_completions",
    )

    assert result["ok"] is False
    assert result["code"] == "bad_response"


async def test_forced_stream_probe_rejects_non_string_provider_delta(monkeypatch):
    """A malformed adapter delta is a provider response failure, not unreachable."""
    stream_resp = _FakeStreamResp(
        200,
        lines=[
            'data: {"choices":[{"delta":{"content":{"secret":"value"}}}]}',
            "data: [DONE]",
        ],
    )
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_stream_client(_FakeResp(200, {"data": []}), stream_resp),
    )

    result = await probe.probe_omni(
        "qwen3.5-omni-plus",
        "https://qwen.example/v1",
        "sk-x",
        "openai_chat_completions",
    )

    assert result["ok"] is False
    assert result["code"] == "bad_response"
    assert "secret" not in result["message"]


async def test_forced_stream_probe_rejects_malformed_nested_openai_sse(
    monkeypatch, caplog
):
    """An AttributeError from a nested SSE shape must stay inside parsing."""
    stream_resp = _FakeStreamResp(
        200,
        lines=[
            'data: {"choices":[{"delta":[]}],"provider_value":"SSE_RAW_SECRET"}',
            "data: [DONE]",
        ],
    )
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_stream_client(_FakeResp(200, {"data": []}), stream_resp),
    )

    result = await probe.probe_omni(
        "qwen3.5-omni-plus",
        "https://qwen.example/v1",
        "sk-x",
        "openai_chat_completions",
    )

    assert result["ok"] is False
    assert result["code"] == "bad_response"
    assert "SSE_RAW_SECRET" not in result["message"]
    assert "SSE_RAW_SECRET" not in caplog.text


async def test_gemini_probe_rejects_malformed_candidate_content(
    monkeypatch, caplog
):
    """Malformed Gemini candidates must not escape the response boundary."""
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            post_resp=_FakeResp(
                200,
                {
                    "candidates": [
                        {
                            "content": [
                                {"parts": "GEMINI_RAW_SECRET"},
                            ]
                        }
                    ]
                },
            ),
        ),
    )

    result = await probe.probe_omni(
        "gemini-vision",
        "https://gemini.example/v1beta",
        "gemini-key",
        "gemini_native",
    )

    assert result["ok"] is False
    assert result["code"] == "bad_response"
    assert "GEMINI_RAW_SECRET" not in result["message"]
    assert "GEMINI_RAW_SECRET" not in caplog.text


# ─── OpenAI Responses visual preflight ─────────────────────────────────────


def _responses_output(text: str, usage: object = ...):
    payload: dict[str, object] = {
        "output": [
            {
                "type": "message",
                "content": [{"type": "output_text", "text": text}],
            }
        ]
    }
    if usage is ...:
        payload["usage"] = {
            "input_tokens": 12,
            "output_tokens": 1,
            "total_tokens": 13,
            "input_tokens_details": {"cached_tokens": 0},
        }
    elif usage is not None:
        payload["usage"] = usage
    return payload


def _responses_structured_output(text: str | None = None, usage: object = ...):
    return _responses_output(
        text
        or json.dumps(
            {
                "caption": "red and blue test cards are visible",
                "matched_rules": [],
                "suggestions": [],
            },
            ensure_ascii=False,
        ),
        usage=usage,
    )


async def test_responses_visual_probe_without_key_sends_valid_red_jpeg(monkeypatch):
    """缺失 Responses 视觉分支会让显式协议无法调用 /responses。"""
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client_post_sequence(
            get_resp=_FakeResp(200, {"data": [{"id": "local-vlm"}]}),
            post_resps=[
                _FakeResp(200, _responses_output("RED")),
                _FakeResp(200, _responses_output("blue")),
                _FakeResp(200, _responses_structured_output()),
            ],
            calls=calls,
        ),
    )

    result = await probe.probe_omni(
        "local-vlm",
        "http://127.0.0.1:8000/v1",
        "",
        api_protocol="openai_responses",
    )

    assert result["ok"] is True
    assert result["code"] == "ok"
    assert "warning" not in result
    assert [call[:2] for call in calls] == [
        ("GET", "http://127.0.0.1:8000/v1/models"),
        ("POST", "http://127.0.0.1:8000/v1/responses"),
        ("POST", "http://127.0.0.1:8000/v1/responses"),
        ("POST", "http://127.0.0.1:8000/v1/responses"),
    ]
    assert "Authorization" not in calls[0][2]["headers"]
    assert "Authorization" not in calls[1][2]["headers"]
    assert "Authorization" not in calls[2][2]["headers"]
    assert "Authorization" not in calls[3][2]["headers"]

    body = calls[1][2]["json"]
    assert body["model"] == "local-vlm"
    assert body["max_output_tokens"] == 256
    assert body["stream"] is False
    assert "temperature" not in body
    assert "top_p" not in body
    content = body["input"][0]["content"]
    assert [block["type"] for block in content] == [
        "input_text",
        "input_image",
        "input_text",
    ]
    assert "dominant_color" in content[0]["text"]
    assert "json" in content[-1]["text"].lower()
    data_url = content[1]["image_url"]
    assert data_url.startswith("data:image/jpeg;base64,")
    image_bytes = base64.b64decode(data_url.partition(",")[2], validate=True)
    with Image.open(io.BytesIO(image_bytes)) as image:
        image.load()
        assert image.format == "JPEG"
        assert image.size == (64, 64)
        pixel = image.convert("RGB").resize((1, 1)).getpixel((0, 0))
        assert isinstance(pixel, tuple)
        red, green, blue = pixel
    assert red > 240 and green < 20 and blue < 20

    body = calls[2][2]["json"]
    content = body["input"][0]["content"]
    data_url = content[1]["image_url"]
    image_bytes = base64.b64decode(data_url.partition(",")[2], validate=True)
    with Image.open(io.BytesIO(image_bytes)) as image:
        image.load()
        pixel = image.convert("RGB").resize((1, 1)).getpixel((0, 0))
        assert isinstance(pixel, tuple)
        red, green, blue = pixel
    assert blue > 240 and red < 20 and green < 20


async def test_responses_visual_probe_rejects_structured_runtime_without_output_text(
    monkeypatch,
):
    """颜色题通过但真实 schema 产不出 output_text 时必须拒绝，避免运行时假阳性。"""
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client_post_sequence(
            get_resp=_FakeResp(200, {"data": [{"id": "local-vlm"}]}),
            post_resps=[
                _FakeResp(200, _responses_output("red")),
                _FakeResp(200, _responses_output("blue")),
                _FakeResp(
                    200,
                    {
                        "status": "completed",
                        "output": [
                            {"type": "reasoning", "summary": []},
                            {
                                "type": "message",
                                "content": [
                                    {"type": "output_text", "text": ""},
                                ],
                            },
                        ],
                        "usage": {
                            "input_tokens": 110,
                            "output_tokens": 512,
                            "total_tokens": 622,
                            "input_tokens_details": {"cached_tokens": 0},
                        },
                    },
                ),
            ],
            calls=calls,
        ),
    )

    result = await probe.probe_omni(
        "qwen3.5:2b-mlx",
        "http://127.0.0.1:11434/v1",
        "",
        api_protocol="openai_responses",
    )

    assert result["ok"] is False
    assert result["code"] == "bad_response"
    assert "结构化" in result["message"]
    assert [call[0] for call in calls] == ["GET", "POST", "POST", "POST"]


def test_responses_structured_probe_repeats_json_instruction_after_images():
    """Some Responses VLMs attend best to the final text block after image inputs."""
    from miloco.perception.engine.omni.provider import OpenAIResponsesAdapter

    messages = probe._structured_probe_messages()
    content = messages[1]["content"]

    assert content[0]["type"] == "text"
    assert content[-1]["type"] == "text"
    assert "JSON" in content[-1]["text"]
    assert [block["type"] for block in content[1:-1]] == [
        "image_url",
        "image_url",
        "image_url",
        "image_url",
        "image_url",
        "image_url",
    ]

    body = OpenAIResponsesAdapter().build_request_body(
        messages,
        model="local-vlm",
        max_tokens=1024,
        temperature=0.0,
        top_p=1.0,
        stream=False,
    )
    converted = body["input"][0]["content"]
    assert converted[0]["type"] == "input_text"
    assert converted[-1]["type"] == "input_text"
    assert [block["type"] for block in converted[1:-1]] == ["input_image"] * 6


async def test_responses_structured_probe_empty_text_reports_actionable_reason(
    monkeypatch,
):
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client_post_sequence(
            get_resp=_FakeResp(404),
            post_resps=[
                _FakeResp(200, _responses_output("red")),
                _FakeResp(200, _responses_output("blue")),
                _FakeResp(200, _responses_output("")),
            ],
        ),
    )

    result = await probe.probe_omni(
        "qwen3.5:2b-mlx",
        "http://127.0.0.1:11434/v1",
        "",
        api_protocol="openai_responses",
    )

    assert result["ok"] is False
    assert result["code"] == "bad_response"
    assert result["message"] == "Responses 结构化预检返回空文本"


async def test_responses_visual_probe_accepts_json_color_answers(monkeypatch):
    """Grok-like Responses models may obey JSON better than exact free text."""
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client_post_sequence(
            get_resp=_FakeResp(404),
            post_resps=[
                _FakeResp(200, _responses_output('{"dominant_color":"red"}')),
                _FakeResp(200, _responses_output('{"dominant_color":"blue"}')),
                _FakeResp(200, _responses_structured_output()),
            ],
            calls=calls,
        ),
    )

    result = await probe.probe_omni(
        "grok-4.6",
        "https://vlm.example/v1",
        "sk-x",
        api_protocol="openai_responses",
    )

    assert result["ok"] is True
    assert result["code"] == "ok"
    visual_body = calls[1][2]["json"]
    assert "dominant_color" in visual_body["input"][0]["content"][0]["text"]


async def test_responses_visual_probe_rejects_model_that_always_answers_red(
    monkeypatch,
):
    """固定红图预检会让不看图、总答 red 的 provider 假阳性通过。"""
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client_post_sequence(
            get_resp=_FakeResp(404),
            post_resps=[
                _FakeResp(200, _responses_output("red")),
                _FakeResp(200, _responses_output("red")),
                _FakeResp(200, _responses_output("red")),
            ],
            calls=calls,
        ),
    )

    result = await probe.probe_omni(
        "local-vlm",
        "https://vlm.example/v1",
        "",
        api_protocol="openai_responses",
    )

    assert result["ok"] is False
    assert result["code"] == "bad_response"
    assert [call[0] for call in calls] == ["GET", "POST", "POST", "POST"]


async def test_responses_visual_probe_retries_once_after_color_mismatch(
    monkeypatch,
):
    """A single flaky color answer should not block an otherwise valid VLM."""
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client_post_sequence(
            get_resp=_FakeResp(404),
            post_resps=[
                _FakeResp(200, _responses_output("blue")),
                _FakeResp(200, _responses_output("red")),
                _FakeResp(200, _responses_output("blue")),
                _FakeResp(200, _responses_structured_output()),
            ],
            calls=calls,
        ),
    )

    result = await probe.probe_omni(
        "grok-4.6",
        "https://vlm.example/v1",
        "sk-x",
        api_protocol="openai_responses",
    )

    assert result["ok"] is True
    assert result["code"] == "ok"
    assert [call[0] for call in calls] == ["GET", "POST", "POST", "POST", "POST"]


async def test_responses_visual_probe_budget_allows_reasoning_before_text(monkeypatch):
    """Qwen-like Responses servers may spend small output budgets on reasoning first."""
    calls: list[tuple[str, str, dict]] = []

    class _ReasoningBudgetClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            calls.append(("GET", args[0], kwargs))
            return _FakeResp(404)

        async def post(self, *args, **kwargs):
            calls.append(("POST", args[0], kwargs))
            max_output_tokens = kwargs["json"]["max_output_tokens"]
            if max_output_tokens < 256:
                return _FakeResp(
                    200,
                    {
                        "status": "completed",
                        "output": [
                            {"type": "reasoning", "summary": []},
                            {
                                "type": "message",
                                "content": [
                                    {"type": "output_text", "text": ""},
                                ],
                            },
                        ],
                        "usage": {
                            "input_tokens": 110,
                            "output_tokens": max_output_tokens,
                            "total_tokens": 110 + max_output_tokens,
                            "input_tokens_details": {"cached_tokens": 0},
                        },
                    },
                )
            if len(calls) == 2:
                return _FakeResp(200, _responses_output("red"))
            if len(calls) == 3:
                return _FakeResp(200, _responses_output("blue"))
            return _FakeResp(200, _responses_structured_output())

    monkeypatch.setattr(probe.httpx, "AsyncClient", _ReasoningBudgetClient)

    result = await probe.probe_omni(
        "qwen3.5:2b-mlx",
        "http://127.0.0.1:11434/v1",
        "",
        api_protocol="openai_responses",
    )

    assert result["ok"] is True
    visual_body = calls[1][2]["json"]
    structured_body = calls[3][2]["json"]
    assert visual_body["max_output_tokens"] == 256
    assert structured_body["max_output_tokens"] == 1024


async def test_responses_visual_probe_sends_bearer_key(monkeypatch):
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client_post_sequence(
            get_resp=_FakeResp(200, {"data": []}),
            post_resps=[
                _FakeResp(200, _responses_output("red")),
                _FakeResp(200, _responses_output("blue")),
                _FakeResp(200, _responses_structured_output()),
            ],
            calls=calls,
        ),
    )

    result = await probe.probe_omni(
        "local-vlm",
        "https://vlm.example/v1",
        "sk-secret",
        api_protocol="openai_responses",
    )

    assert result["ok"] is True
    assert calls[0][2]["headers"]["Authorization"] == "Bearer sk-secret"
    assert calls[1][2]["headers"]["Authorization"] == "Bearer sk-secret"
    assert calls[2][2]["headers"]["Authorization"] == "Bearer sk-secret"
    assert calls[3][2]["headers"]["Authorization"] == "Bearer sk-secret"


async def test_responses_visual_probe_models_not_supported_still_proves_vision(
    monkeypatch,
):
    for status in (404, 405):
        calls: list[tuple[str, str, dict]] = []
        monkeypatch.setattr(
            probe.httpx,
            "AsyncClient",
            _fake_async_client_post_sequence(
                get_resp=_FakeResp(status),
                post_resps=[
                    _FakeResp(200, _responses_output("red.")),
                    _FakeResp(200, _responses_output("blue.")),
                    _FakeResp(200, _responses_structured_output()),
                ],
                calls=calls,
            ),
        )

        result = await probe.probe_omni(
            "local-vlm",
            "https://vlm.example/v1",
            "",
            api_protocol="openai_responses",
        )

        assert result["ok"] is True
        assert [call[0] for call in calls] == ["GET", "POST", "POST", "POST"]


@pytest.mark.parametrize(
    "answer",
    [
        "Request acknowledged.",
        "The request was colored.",
        "redacted",
        "already done",
        "red is the dominant color",
        "the dominant color is red",
    ],
)
async def test_responses_visual_probe_rejects_non_exact_color_answer(
    monkeypatch,
    answer,
):
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            get_resp=_FakeResp(404),
            post_resp=_FakeResp(200, _responses_output(answer)),
        ),
    )

    result = await probe.probe_omni(
        "local-vlm",
        "https://vlm.example/v1",
        "",
        api_protocol="openai_responses",
    )

    assert result["ok"] is False
    assert result["code"] == "bad_response"
    assert answer.casefold().strip() not in result["message"].casefold()


@pytest.mark.parametrize("answer", ["red", "RED", "  red\n", "red.", " RED. "])
async def test_responses_visual_probe_accepts_exact_color_answers(monkeypatch, answer):
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client_post_sequence(
            get_resp=_FakeResp(405),
                post_resps=[
                    _FakeResp(200, _responses_output(answer)),
                    _FakeResp(200, _responses_output(" blue.\n")),
                    _FakeResp(200, _responses_structured_output()),
                ],
            ),
    )

    result = await probe.probe_omni(
        "local-vlm",
        "https://vlm.example/v1",
        "",
        api_protocol="openai_responses",
    )

    assert result["ok"] is True
    assert result["code"] == "ok"


async def test_responses_visual_probe_rejects_missing_output_text(monkeypatch):
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            get_resp=_FakeResp(404),
            post_resp=_FakeResp(
                200,
                {"output": [{"content": [{"type": "refusal", "text": "red"}]}]},
            ),
        ),
    )

    result = await probe.probe_omni(
        "local-vlm",
        "https://vlm.example/v1",
        "",
        api_protocol="openai_responses",
    )

    assert result["ok"] is False
    assert result["code"] == "bad_response"


async def test_responses_visual_probe_warns_when_usage_is_missing(monkeypatch):
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client_post_sequence(
            get_resp=_FakeResp(404),
                post_resps=[
                    _FakeResp(200, _responses_output("red", usage=None)),
                    _FakeResp(200, _responses_output("blue")),
                    _FakeResp(200, _responses_structured_output()),
                ],
            ),
    )

    result = await probe.probe_omni(
        "local-vlm",
        "https://vlm.example/v1",
        "",
        api_protocol="openai_responses",
    )

    assert result["ok"] is True
    assert result["warning"] == "usage_unavailable"


async def test_responses_visual_probe_warns_when_usage_is_malformed(monkeypatch):
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client_post_sequence(
            get_resp=_FakeResp(404),
                post_resps=[
                    _FakeResp(200, _responses_output("red", usage="not-an-object")),
                    _FakeResp(200, _responses_output("blue")),
                    _FakeResp(200, _responses_structured_output()),
                ],
            ),
    )

    result = await probe.probe_omni(
        "local-vlm",
        "https://vlm.example/v1",
        "",
        api_protocol="openai_responses",
    )

    assert result["ok"] is True
    assert result["warning"] == "usage_unavailable"


async def test_responses_visual_probe_classifies_response_auth_failure(monkeypatch):
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(get_resp=_FakeResp(404), post_resp=_FakeResp(403)),
    )

    result = await probe.probe_omni(
        "local-vlm",
        "https://vlm.example/v1",
        "bad-key",
        api_protocol="openai_responses",
    )

    assert result["ok"] is False
    assert result["code"] == "bad_key"
    assert result["status"] == 403


async def test_responses_visual_probe_classifies_response_timeout(monkeypatch):
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            get_resp=_FakeResp(404),
            post_exc=httpx.ReadTimeout("slow"),
        ),
    )

    result = await probe.probe_omni(
        "local-vlm",
        "https://vlm.example/v1",
        "",
        api_protocol="openai_responses",
    )

    assert result["ok"] is False
    assert result["code"] == "unreachable"
    assert "slow" not in result["message"]


async def test_responses_visual_probe_accepts_slow_response_with_120_second_timeout(
    monkeypatch,
):
    """A slow valid visual response must not inherit the shared 15s read budget."""
    effective_timeouts: list[httpx.Timeout] = []

    class _LatencyAwareAsyncClient:
        def __init__(self, *args, timeout: httpx.Timeout, **kwargs):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            return _FakeResp(404)

        async def post(self, *args, **kwargs):
            effective_timeout = kwargs.get("timeout", self.timeout)
            effective_timeouts.append(effective_timeout)
            if effective_timeout.read <= 15.5:
                raise httpx.ReadTimeout("simulated 15.5 second response")
            return _FakeResp(
                200,
                _responses_output("red")
                if len(effective_timeouts) == 1
                else (
                    _responses_output("blue")
                    if len(effective_timeouts) == 2
                    else _responses_structured_output()
                ),
            )

    monkeypatch.setattr(probe.httpx, "AsyncClient", _LatencyAwareAsyncClient)

    result = await probe.probe_omni(
        "local-vlm",
        "https://vlm.example/v1",
        "",
        api_protocol="openai_responses",
    )

    effective_timeout = effective_timeouts[0]
    assert result["code"] == "ok", (
        f"15.5s response was {result['code']} with "
        f"read_timeout={effective_timeout.read}, "
        f"connect_timeout={effective_timeout.connect}"
    )
    assert result["ok"] is True
    assert effective_timeout.read == 120.0
    assert effective_timeout.connect == 10.0


async def test_responses_visual_probe_preserves_rate_limit_retry_after(monkeypatch):
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            get_resp=_FakeResp(404),
            post_resp=_FakeResp(429, headers={"Retry-After": "17"}),
        ),
    )

    result = await probe.probe_omni(
        "local-vlm",
        "https://vlm.example/v1",
        "",
        api_protocol="openai_responses",
    )

    assert result["ok"] is False
    assert result["code"] == "rate_limited"
    assert result["retry_after_seconds"] == 17.0


async def test_responses_models_400_is_non_visual_rejected_and_skips_post(monkeypatch):
    """A discovery failure cannot prove that the provider rejected image input."""
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            get_resp=_FakeResp(
                400,
                {"error": {"message": "RAW_PROVIDER_SECRET data:image"}},
            ),
            post_resp=_FakeResp(200, _responses_output("red")),
            calls=calls,
        ),
    )

    result = await probe.probe_omni(
        "local-vlm",
        "https://vlm.example/v1",
        "responses-key",
        api_protocol="openai_responses",
    )

    assert result["ok"] is False
    assert result["code"] == "rejected_authed"
    assert result["code"] != "visual_payload_rejected"
    assert [call[0] for call in calls] == ["GET"]
    assert "RAW_PROVIDER_SECRET" not in result["message"]
    assert "data:image" not in result["message"]


async def test_responses_visual_400_is_visual_payload_rejected(monkeypatch):
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            get_resp=_FakeResp(404),
            post_resp=_FakeResp(400),
        ),
    )

    result = await probe.probe_omni(
        "local-vlm",
        "https://vlm.example/v1",
        "",
        api_protocol="openai_responses",
    )

    assert result["code"] == "visual_payload_rejected"
    assert "API Key" not in result["message"]


@pytest.mark.parametrize(
    ("provider_code", "expected"),
    [
        ("invalid_api_key", "bad_key"),
        ("model_not_found", "not_found"),
    ],
)
async def test_responses_visual_structured_config_code_overrides_fallback(
    monkeypatch, provider_code, expected
):
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            get_resp=_FakeResp(404),
            post_resp=_FakeResp(
                400,
                {
                    "error": {
                        "code": provider_code,
                        "message": "RAW_PROVIDER_SECRET data:image",
                    }
                },
            ),
        ),
    )

    result = await probe.probe_omni(
        "local-vlm",
        "https://vlm.example/v1",
        "responses-key",
        api_protocol="openai_responses",
    )

    assert result["code"] == expected
    assert "RAW_PROVIDER_SECRET" not in result["message"]
    assert "data:image" not in result["message"]


async def test_explicit_responses_protocol_never_falls_back_by_model_name(monkeypatch):
    for model in ("qwen3.5-omni-plus", "gemini-vision"):
        calls: list[tuple[str, str, dict]] = []
        monkeypatch.setattr(
            probe.httpx,
            "AsyncClient",
            _fake_async_client_post_sequence(
                get_resp=_FakeResp(404),
                post_resps=[
                    _FakeResp(200, _responses_output("red")),
                    _FakeResp(200, _responses_output("blue")),
                    _FakeResp(200, _responses_structured_output()),
                ],
                calls=calls,
            ),
        )

        result = await probe.probe_omni(
            model,
            "https://vlm.example/v1",
            "",
            api_protocol="openai_responses",
        )

        assert result["ok"] is True
        assert calls[1][1] == "https://vlm.example/v1/responses"
        assert calls[1][2]["json"]["stream"] is False
        assert calls[2][1] == "https://vlm.example/v1/responses"
        assert calls[2][2]["json"]["stream"] is False
        assert calls[3][1] == "https://vlm.example/v1/responses"
        assert calls[3][2]["json"]["stream"] is False


async def test_explicit_gemini_probe_protocol_overrides_model_name(monkeypatch):
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            post_resp=_FakeResp(
                200,
                {"candidates": [{"content": {"parts": [{"text": "red"}]}}]},
            ),
            calls=calls,
        ),
    )

    result = await probe.probe_omni(
        "plain-model-name",
        "https://generativelanguage.googleapis.com/v1beta",
        "gemini-key",
        api_protocol="gemini_native",
    )

    assert result["ok"] is True
    assert [call[:2] for call in calls] == [
        (
            "POST",
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "plain-model-name:generateContent",
        )
    ]
    assert calls[0][2]["headers"]["x-goog-api-key"] == "gemini-key"
    assert "Authorization" not in calls[0][2]["headers"]


async def test_explicit_chat_protocol_survives_real_http_probe_for_gemini_named_model(
    recording_http_server,
):
    base_url, requests = recording_http_server

    result = await probe.probe_omni(
        "gemini-named-local-model",
        base_url,
        "chat-key",
        api_protocol="openai_chat_completions",
    )

    assert result["ok"] is True
    assert [(request["method"], request["path"]) for request in requests] == [
        ("GET", "/v1/models"),
        ("POST", "/v1/chat/completions"),
    ]
    for request in requests:
        assert request["headers"]["Authorization"] == "Bearer chat-key"
        assert "x-goog-api-key" not in request["headers"]




async def test_probe_chat_visual_payload_rejected_on_400(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(400))
    )
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x", api_protocol="openai_chat_completions")
    assert r["code"] == "visual_payload_rejected"


async def test_probe_chat_visual_payload_rejected_on_422(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(422))
    )
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x", api_protocol="openai_chat_completions")
    assert r["code"] == "visual_payload_rejected"


def test_is_gemini_native_endpoint_covers_both_access_paths():
    f = probe._is_gemini_native_endpoint
    assert f("https://generativelanguage.googleapis.com/v1beta")
    # Vertex 原生：express 形态与项目级形态，都经 /publishers/
    assert f("https://aiplatform.googleapis.com/v1/publishers/google")
    assert f(
        "https://us-central1-aiplatform.googleapis.com"
        "/v1/projects/p/locations/us-central1/publishers/google"
    )
    # 按主机名判而非 URL 子串：把域名塞进路径不算命中
    assert not f("https://evil.com/aiplatform.googleapis.com")
    assert not f("https://evil.com/generativelanguage.googleapis.com")
    assert not f("https://api.xiaomimimo.com/v1")


def test_parse_model_ids_tries_all_shapes():
    """认识的几种形态都试：prefer_native 只决定先试哪一种，不是唯一依据。

    绑死单一形态时，端点形态一旦判错（经代理转发、或没验证过的路径）就解出空列表
    并被报成成功，下拉恒空且用户拿不到提示。
    """
    f = probe._parse_model_ids
    native = {"models": [{"name": "models/gemini-3.6-flash"}]}
    compat = {"data": [{"id": "m1"}]}
    # 判对时按各自形态解
    assert f(native, prefer_native=True) == ["gemini-3.6-flash"]
    assert f(compat, prefer_native=False) == ["m1"]
    # 判错时回退到另一种形态，而不是解出空列表
    assert f(compat, prefer_native=True) == ["m1"]
    assert f(native, prefer_native=False) == ["gemini-3.6-flash"]
    # Vertex 的 publisherModels 形态（name 带 publishers/…/models/ 前缀）也认
    assert f(
        {"publisherModels": [{"name": "publishers/google/models/gemini-3.6-flash"}]},
        prefer_native=True,
    ) == ["gemini-3.6-flash"]
    # 兼容形态的 id 原样透传：含 /models/ 是合法的（如 accounts/<org>/models/<name>），
    # 套用原生形态的剥前缀规则会把它截断成 provider 认不出的名字
    assert f(
        {"data": [{"id": "accounts/fireworks/models/llama-v3-70b"}]}, prefer_native=False
    ) == ["accounts/fireworks/models/llama-v3-70b"]
    # 三种都不认 / 非 dict → 空
    assert f({"someOtherShape": [{"name": "x"}]}, prefer_native=True) == []
    assert f(["not", "a", "dict"], prefer_native=True) == []
    # 条目不是 dict、列表位不是 list 都只跳过，不抛
    assert f({"models": ["bad", {"name": "models/ok"}]}, prefer_native=True) == ["ok"]
    assert f({"models": "notalist", "data": [{"id": "m1"}]}, prefer_native=True) == ["m1"]
    # 产出不含空串：剥完前缀为空的条目要被丢掉，且不能因此短路掉另一种形态的回退
    assert f({"models": [{"name": "models/"}]}, prefer_native=True) == []
    assert f(
        {"models": [{"name": "models/"}], "data": [{"id": "real"}]}, prefer_native=True
    ) == ["real"]
    # 标识是数字等标量时归一而非抛（旧实现会让调用方的 sorted 混排类型报 TypeError）
    assert f({"data": [{"id": "a"}, {"id": 1}]}, prefer_native=False) == ["a", "1"]
    # 但嵌套结构不是合法标识：跳过，而不是 str() 成一串垃圾文本混进下拉
    assert f({"data": [{"id": {"name": "x"}}, {"id": "ok"}]}, prefer_native=False) == ["ok"]
    assert f({"data": [{"id": ["a"]}]}, prefer_native=False) == []
    assert f({"models": [{"name": {"k": "v"}}], "data": [{"id": "fallback"}]},
             prefer_native=True) == ["fallback"]


async def test_fetch_models_mixed_id_types_do_not_500(monkeypatch):
    """条目 id 类型混杂时不能让异常逃出本接口。"""
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(_FakeResp(200, {"data": [{"id": "b"}, {"id": 1}]})),
    )
    r = await probe.fetch_models("https://aiplatform.googleapis.com/v1/publishers/google", "k", "gemini_native")
    assert r["ok"] is True and r["models"] == ["1", "b"]


async def test_fetch_models_recovers_when_shape_mismatches(monkeypatch):
    """原生端点回了兼容形态时仍解得出模型，而不是静默返回空列表。"""
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(_FakeResp(200, {"data": [{"id": "gemini-3.6-flash"}]})),
    )
    r = await probe.fetch_models(
        "https://aiplatform.googleapis.com/v1/publishers/google", "k"
    , api_protocol="gemini_native")
    assert r == {"ok": True, "models": ["gemini-3.6-flash"]}


def test_gemini_api_openai_compat_path_stays_on_openai_branch():
    """Gemini API 主机下的 OpenAI 兼容 base_url 必须留在 Bearer 分支。

    官方兼容 base_url 是 https://generativelanguage.googleapis.com/v1beta/openai/，
    走 Authorization: Bearer + {data:[{id}]}。判成原生有两种落点、都坏：发 x-goog-api-key
    被拒 → 401 误报 bad_key；或被收下 → 按 {models:[{name}]} 解析 → 空列表且 ok=true。
    """
    f = probe._is_gemini_native_endpoint
    assert not f("https://generativelanguage.googleapis.com/v1beta/openai")
    assert not f("https://generativelanguage.googleapis.com/v1beta/openai/")
    # 不少工具要求 base 以 /v1 结尾——尾锚定判定会漏掉这一形态
    assert not f("https://generativelanguage.googleapis.com/v1beta/openai/v1")
    assert f("https://generativelanguage.googleapis.com/v1beta")
    # 按路径段比对，不误伤名字里恰好含 openai 的段
    assert f("https://generativelanguage.googleapis.com/v1beta/myopenai")
    # 路径段与主机名一样做大小写归一：URL 路径大小写敏感，但判定不该因此漏判
    assert not f("https://generativelanguage.googleapis.com/v1beta/OpenAI")
    assert not f("https://generativelanguage.googleapis.com/v1beta/OPENAI/v1")


async def test_fetch_models_gemini_openai_compat_uses_bearer(monkeypatch):
    """兼容路径走到发请求这一层也要是 Bearer，且按 {data:[{id}]} 解析出模型。"""
    seen: dict = {}
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(_FakeResp(200, {"data": [{"id": "gemini-3.6-flash"}]}), seen=seen),
    )
    r = await probe.fetch_models(
        "https://generativelanguage.googleapis.com/v1beta/openai", "k"
    , api_protocol="openai_chat_completions")
    assert r == {"ok": True, "models": ["gemini-3.6-flash"]}
    assert "Authorization" in seen["headers"]
    assert "x-goog-api-key" not in seen["headers"]


def test_vertex_openai_compat_path_stays_on_openai_branch():
    """Vertex 同主机下的 OpenAI 兼容路径必须留在 Bearer 分支。

    该端点鉴权走 ``Authorization: Bearer``；只按主机名判会给它发 x-goog-api-key
    → 401 → 误报 bad_key，正是本模块要消除的那类误报。
    """
    assert not probe._is_gemini_native_endpoint(
        "https://us-central1-aiplatform.googleapis.com"
        "/v1beta1/projects/p/locations/us-central1/endpoints/openapi"
    )


def test_vertex_non_native_path_not_treated_as_native():
    """Vertex 主机上**任何**不经 publishers 段的路径都不按原生处理。

    这条锁的是「白名单」而非「黑名单」语义：**若改成**只排除已知的 OpenAI 兼容路径，
    该主机上其余非原生路径就会被误判成原生、发错鉴权头——本用例即为拦住那种改法。
    """
    assert not probe._is_gemini_native_endpoint(
        "https://us-central1-aiplatform.googleapis.com/v1beta1/projects/p/locations/l/datasets"
    )
    assert not probe._is_gemini_native_endpoint(
        "https://aiplatform.googleapis.com/v1"
    )


async def test_fetch_models_vertex_openai_compat_uses_bearer(monkeypatch):
    """走到发请求这一层也要是 Bearer —— 判定改错时这条会连带变红。"""
    seen: dict = {}
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(_FakeResp(200, {"data": [{"id": "m1"}]}), seen=seen)
    )
    r = await probe.fetch_models(
        "https://us-central1-aiplatform.googleapis.com"
        "/v1beta1/projects/p/locations/us-central1/endpoints/openapi",
        "k",
    api_protocol="openai_chat_completions")
    assert r == {"ok": True, "models": ["m1"]}
    assert "Authorization" in seen["headers"]
    assert "x-goog-api-key" not in seen["headers"]


async def test_fetch_models_vertex_uses_goog_key_header(monkeypatch):
    """Vertex 主机按 Gemini 家族处理：走 x-goog-api-key，不发 Bearer。"""
    seen: dict = {}
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            _FakeResp(200, {"models": [{"name": "models/gemini-3.6-flash"}]}), seen=seen
        ),
    )
    r = await probe.fetch_models("https://aiplatform.googleapis.com/v1/publishers/google", "k", "gemini_native")
    assert r == {"ok": True, "models": ["gemini-3.6-flash"]}
    assert "x-goog-api-key" in seen["headers"]
    assert "Authorization" not in seen["headers"]


async def test_fetch_models_gemini_404_is_list_unsupported(monkeypatch):
    """该端点没有 models.list（Vertex 实测回 404）→ 提示手填模型名，不报地址错。"""
    monkeypatch.setattr(probe.httpx, "AsyncClient", _fake_async_client(_FakeResp(404)))
    r = await probe.fetch_models("https://aiplatform.googleapis.com/v1/publishers/google", "k", "gemini_native")
    assert r["ok"] is False and r["code"] == "list_unsupported" and r["models"] == []


async def test_fetch_models_404_gate_is_endpoint_level_not_host_level(monkeypatch):
    """404 归「列不出来」的判据是**端点**级，不是主机级。

    同主机下的 OpenAI 兼容路径本身就有 /models 接口，那里的 404 更可能是路径写错，
    应落 http_error；把判据放宽到「同族主机」会让它被误报成「该端点没有列模型接口」。
    """
    monkeypatch.setattr(probe.httpx, "AsyncClient", _fake_async_client(_FakeResp(404)))
    native = await probe.fetch_models(
        "https://generativelanguage.googleapis.com/v1beta", "k"
    , api_protocol="gemini_native")
    assert native["code"] == "list_unsupported"

    monkeypatch.setattr(probe.httpx, "AsyncClient", _fake_async_client(_FakeResp(404)))
    compat = await probe.fetch_models(
        "https://generativelanguage.googleapis.com/v1beta/openai", "k"
    , api_protocol="openai_chat_completions")
    assert compat["code"] == "http_error", "同主机的兼容路径 404 不该归 list_unsupported"


async def test_fetch_models_gemini_401_403_still_bad_key(monkeypatch):
    """坏 key / 被限制的 key 正是回 401/403 —— 不能被「列不出来」吞掉。"""
    for status in (401, 403):
        monkeypatch.setattr(
            probe.httpx, "AsyncClient", _fake_async_client(_FakeResp(status))
        )
        r = await probe.fetch_models("https://generativelanguage.googleapis.com/v1beta", "k", "gemini_native")
        assert r["code"] == "bad_key", f"HTTP {status} 应仍判 bad_key"


async def test_fetch_models_non_gemini_unchanged(monkeypatch):
    """非 Gemini 主机行为不变：401 → bad_key，404 → http_error。"""
    monkeypatch.setattr(probe.httpx, "AsyncClient", _fake_async_client(_FakeResp(401)))
    assert (await probe.fetch_models("https://api.example.com/v1", "k", "openai_chat_completions"))["code"] == "bad_key"
    monkeypatch.setattr(probe.httpx, "AsyncClient", _fake_async_client(_FakeResp(404)))
    assert (await probe.fetch_models("https://api.example.com/v1", "k", "openai_chat_completions"))["code"] == "http_error"


async def test_probe_reachable_404_gate_is_endpoint_level_not_host_level(monkeypatch):
    """预检侧的 404 判据同样是**端点**级，与拉模型列表那侧对称。

    同主机下的 OpenAI 兼容路径 404 仍应报出来；放宽到「同族主机」会让它被当作
    「该端点没有列模型接口」而放行，把地址错盖成「缺 key」。
    """
    monkeypatch.setattr(probe.httpx, "AsyncClient", _fake_async_client(_FakeResp(404)))
    assert (
        await probe.probe_reachable("https://generativelanguage.googleapis.com/v1beta")
        is None
    )
    monkeypatch.setattr(probe.httpx, "AsyncClient", _fake_async_client(_FakeResp(404)))
    compat = await probe.probe_reachable(
        "https://generativelanguage.googleapis.com/v1beta/openai"
    )
    assert compat is not None and compat["code"] == "http_error"


async def test_probe_reachable_gemini_404_not_reported_as_url_error(monkeypatch):
    """Vertex 没有 models.list，404 不该在「缺 key」之前先被报成地址错。"""
    monkeypatch.setattr(probe.httpx, "AsyncClient", _fake_async_client(_FakeResp(404)))
    assert (
        await probe.probe_reachable("https://aiplatform.googleapis.com/v1/publishers/google")
        is None
    )
    # 非 Gemini 主机的 404 仍然要报出来
    monkeypatch.setattr(probe.httpx, "AsyncClient", _fake_async_client(_FakeResp(404)))
    r = await probe.probe_reachable("https://api.example.com/v1")
    assert r is not None and r["code"] == "http_error"


async def test_probe_chat_sends_same_headers_as_runtime_path(monkeypatch):
    """probe 上线的 headers 逐项等于 build_request_headers() 的产物。

    断言的是**相等**而非「含有 session 头」:相等才能同时守住两头的漂移 ——
    probe 少发一个头(修复前的形态)或多发一个头(未来加头时忘了 probe)都会红。
    """
    sent: list[dict] = []
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(resp=_FakeResp(200, {"choices": [{"message": {"content": "red"}}]}), sent=sent),
    )
    base_url = "https://opencode.ai/zen/go/v1"
    model = "xiaomi/mimo-v2.5"

    r = await probe.probe_chat(model, base_url, "sk-x", api_protocol="openai_chat_completions")

    assert r["ok"] is True
    assert len(sent) == 1
    runtime_headers = build_request_headers(
        get_adapter(None, model), base_url, "sk-x"
    )
    assert sent[0]["headers"] == runtime_headers
    # 显式钉住这个 case 的意义:OpenCode Go 缺此头即拒probe,不分叉的前提。
    assert sent[0]["headers"]["x-opencode-session"].startswith("ses_")


async def test_probe_chat_headers_match_runtime_for_non_opencode_endpoint(monkeypatch):
    """非 OpenCode Go endpoint:probe 与运行时同样不发 session 头。

    反向守「别顺手给所有 endpoint 都加头」—— 一致性的正确形态是「跟运行时一样」,
    不是「总是带session」;运行时对普通 endpoint 不发,probe 发了就是新的分叉。
    """
    sent: list[dict] = []
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(resp=_FakeResp(200, {"choices": [{"message": {"content": "red"}}]}), sent=sent),
    )
    base_url = "https://api.openai.com/v1"
    model = "xiaomi/mimo-v2.5"

    r = await probe.probe_chat(model, base_url, "sk-x", api_protocol="openai_chat_completions")

    assert r["ok"] is True
    assert len(sent) == 1
    assert "x-opencode-session" not in sent[0]["headers"]
    assert sent[0]["headers"] == build_request_headers(
        get_adapter(None, model), base_url, "sk-x"
    )


async def test_probe_chat_sends_session_header_for_trailing_slash_base_url(monkeypatch):
    """base_url 带尾斜杠时(probe 内部 ``_normalize_base_url`` 会 rstrip)仍要发session 头。

    probe 在调 build_request_headers 前先归一化 base_url,归一化与「是否 OpenCode Go
    判定」耦合:若归一化顺序变了导致 ``/zen/go/v1/`` 判定落空,probe 就会静默不发
    session 头,只在用户手填尾斜杠时复现。故在传输层钉住这一条。
    """
    sent: list[dict] = []
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(resp=_FakeResp(200, {"choices": [{"message": {"content": "red"}}]}), sent=sent),
    )

    r = await probe.probe_chat(
        "xiaomi/mimo-v2.5", "https://opencode.ai/zen/go/v1/", "sk-x"
    , api_protocol="openai_chat_completions")

    assert r["ok"] is True
    assert len(sent) == 1
    assert sent[0]["headers"]["x-opencode-session"].startswith("ses_")
    assert sent[0]["headers"] == build_request_headers(
        get_adapter(None, "xiaomi/mimo-v2.5"),
        "https://opencode.ai/zen/go/v1",
        "sk-x",
    )


async def test_fetch_models_sends_only_auth_for_opencode_endpoint(monkeypatch):
    """列模型仍是鉴权探测，不携带推理请求的会话头。"""
    seen: dict = {}
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            _FakeResp(200, {"data": [{"id": "xiaomi/mimo-v2.5"}]}), seen=seen
        ),
    )

    r = await probe.fetch_models("https://opencode.ai/zen/go/v1", "sk-x", "openai_chat_completions")

    assert r == {"ok": True, "models": ["xiaomi/mimo-v2.5"]}
    assert seen["url"] == "https://opencode.ai/zen/go/v1/models"
    assert seen["headers"] == {"Authorization": "Bearer sk-x"}


async def test_probe_omni_keeps_get_auth_and_chat_runtime_headers(monkeypatch):
    """同次两阶段探测：GET 仅鉴权，POST 与运行时同头。"""
    seen: dict = {}
    sent: list[dict] = []
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(_FakeResp(200, {"choices": [{"message": {"content": "red"}}]}), seen=seen, sent=sent),
    )
    base = "https://opencode.ai/zen/go/v1"
    model = "xiaomi/mimo-v2.5"

    r = await probe.probe_omni(model, base, "sk-x", api_protocol="openai_chat_completions")

    assert r["ok"] is True
    assert seen["url"] == f"{base}/models"
    assert seen["headers"] == {"Authorization": "Bearer sk-x"}
    assert len(sent) == 1
    assert sent[0]["headers"] == build_request_headers(get_adapter(None, model), base, "sk-x")
    assert sent[0]["headers"]["x-opencode-session"].startswith("ses_")
