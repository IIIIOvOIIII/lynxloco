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
            if calls is not None:
                calls.append(("GET", a[0], k))
            raised = get_exc if get_exc is not None else exc
            if raised is not None:
                raise raised
            return g

        async def post(self, *a, **k):
            if calls is not None:
                calls.append(("POST", a[0], k))
            raised = post_exc if post_exc is not None else exc
            if raised is not None:
                raise raised
            return p

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
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x")
    assert r["ok"] is True and r["code"] == "ok" and r["status"] == 200
    assert "latency_ms" in r


async def test_probe_chat_bad_key(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(401))
    )
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x")
    assert r["ok"] is False and r["code"] == "bad_key" and r["status"] == 401


async def test_probe_chat_not_found(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(404))
    )
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x")
    assert r["code"] == "not_found" and r["status"] == 404


async def test_probe_chat_visual_400_is_visual_payload_rejected(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(400))
    )
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x")
    assert r["code"] == "visual_payload_rejected"
    assert "API Key" not in r["message"]


async def test_probe_chat_visual_422_is_visual_payload_rejected(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(422))
    )
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x")
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

    result = await probe.probe_chat("m1", "https://ok/v1", "sk-x")

    assert result["code"] == expected
    assert "RAW_PROVIDER_SECRET" not in result["message"]
    assert "data:image" not in result["message"]


async def test_probe_chat_http_error_on_500(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(resp=_FakeResp(500))
    )
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x")
    assert r["code"] == "http_error"


async def test_probe_chat_unreachable_on_exception(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(exc=httpx.ReadTimeout("slow"))
    )
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x")
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
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x")
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
    r = await probe.probe_chat("m1", "https://ok/v1", "sk-x")
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
    r = await probe.probe_chat("m", "file:///etc/passwd", "sk-x")
    assert r["ok"] is False and r["code"] == "unreachable"


async def test_probe_omni_rejects_file_scheme():
    r = await probe.probe_omni("m", "file:///etc/passwd", "sk-x")
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
    r = await probe.probe_omni("m1", "https://ok/v1", "sk-x")
    assert r["code"] == "bad_key"


async def test_probe_omni_get_500_short_circuits_to_http_error(monkeypatch):
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(get_resp=_FakeResp(500), post_resp=_FakeResp(200)),
    )
    r = await probe.probe_omni("m1", "https://ok/v1", "sk-x")
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
    r = await probe.probe_omni("m1", "https://ok/v1", "sk-x")
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
    r = await probe.probe_omni("m1", "https://ok/v1", "sk-x")
    assert r["code"] == "not_found"


async def test_probe_omni_connect_error(monkeypatch):
    monkeypatch.setattr(
        probe.httpx, "AsyncClient", _fake_async_client(exc=httpx.ConnectError("nope"))
    )
    r = await probe.probe_omni("m1", "https://nope/v1", "sk-x")
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
    r = await probe.probe_omni("qwen3.5-omni-plus", "https://qwen.example/v1", "sk-x")
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
    r = await probe.probe_omni("qwen3.5-omni-plus", "https://qwen.example/v1", "sk-x")
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
    r = await probe.probe_omni("xiaomi/mimo-v2.5", "https://mimo.example/v1", "sk-x")
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
    r = await probe.probe_omni("qwen3.5-omni-plus", "https://qwen.example/v1", "sk-x")
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


async def test_responses_visual_probe_without_key_sends_valid_red_jpeg(monkeypatch):
    """缺失 Responses 视觉分支会让显式协议无法调用 /responses。"""
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            get_resp=_FakeResp(200, {"data": [{"id": "local-vlm"}]}),
            post_resp=_FakeResp(200, _responses_output("RED")),
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
    ]
    assert "Authorization" not in calls[0][2]["headers"]
    assert "Authorization" not in calls[1][2]["headers"]

    body = calls[1][2]["json"]
    assert body["model"] == "local-vlm"
    assert body["max_output_tokens"] == 16
    assert body["stream"] is False
    assert "temperature" not in body
    assert "top_p" not in body
    content = body["input"][0]["content"]
    assert [block["type"] for block in content] == ["input_text", "input_image"]
    assert "dominant color" in content[0]["text"].lower()
    data_url = content[1]["image_url"]
    assert data_url.startswith("data:image/jpeg;base64,")
    image_bytes = base64.b64decode(data_url.partition(",")[2], validate=True)
    with Image.open(io.BytesIO(image_bytes)) as image:
        image.load()
        assert image.format == "JPEG"
        assert image.size == (32, 32)
        pixel = image.convert("RGB").resize((1, 1)).getpixel((0, 0))
        assert isinstance(pixel, tuple)
        red, green, blue = pixel
    assert red > 240 and green < 20 and blue < 20


async def test_responses_visual_probe_sends_bearer_key(monkeypatch):
    calls: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            get_resp=_FakeResp(200, {"data": []}),
            post_resp=_FakeResp(200, _responses_output("red")),
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


async def test_responses_visual_probe_models_not_supported_still_proves_vision(
    monkeypatch,
):
    for status in (404, 405):
        calls: list[tuple[str, str, dict]] = []
        monkeypatch.setattr(
            probe.httpx,
            "AsyncClient",
            _fake_async_client(
                get_resp=_FakeResp(status),
                post_resp=_FakeResp(200, _responses_output("red.")),
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
        assert [call[0] for call in calls] == ["GET", "POST"]


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
async def test_responses_visual_probe_accepts_exact_red_answer(monkeypatch, answer):
    monkeypatch.setattr(
        probe.httpx,
        "AsyncClient",
        _fake_async_client(
            get_resp=_FakeResp(405),
            post_resp=_FakeResp(200, _responses_output(answer)),
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
        _fake_async_client(
            get_resp=_FakeResp(404),
            post_resp=_FakeResp(200, _responses_output("red", usage=None)),
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
        _fake_async_client(
            get_resp=_FakeResp(404),
            post_resp=_FakeResp(200, _responses_output("red", usage="not-an-object")),
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


async def test_responses_visual_probe_accepts_15_5_second_response_with_30_second_timeout(
    monkeypatch,
):
    """A valid 15.5s visual response must not inherit the shared 15s read budget."""
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
            return _FakeResp(200, _responses_output("red"))

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
    assert effective_timeout.read == 30.0
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
            _fake_async_client(
                get_resp=_FakeResp(404),
                post_resp=_FakeResp(200, _responses_output("red")),
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
