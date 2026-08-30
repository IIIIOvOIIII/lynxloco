# Copyright (C) 2025 Xiaomi Corporation
# This software may be used and distributed according to the terms of the Xiaomi Miloco License Agreement.

"""F4 修复回归测试:SSE endpoint 鉴权(query or header 双路径).

策略:不实际握手 SSE(generator 不会主动结束,TestClient 会卡住),只验:
1. verify_token_query_fallback 函数本身(单元测试,直接构造 Request)
2. /stream endpoint 在错误鉴权下被立即 401 拒绝(401 不返 generator 故不卡)
3. /api/events 普通 endpoint 仍只接受 header(F4 修复不影响其他 endpoint)
"""

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

# ─── 单元测试 verify_token_query_fallback ─────────────────


def _mk_request(headers=None, query=None) -> Request:
    """造一个最小可用的 Request 对象."""
    scope = {
        "type": "http",
        "headers": [
            (k.lower().encode(), v.encode()) for k, v in (headers or {}).items()
        ],
        "query_string": (
            "&".join(f"{k}={v}" for k, v in (query or {}).items())
        ).encode(),
        "path": "/api/events/stream",
    }
    return Request(scope)


class TestVerifyTokenQueryFallback:
    def test_no_token_configured_rejects(self, monkeypatch):
        """server.token="" 时必须 fail closed."""
        monkeypatch.setenv("MILOCO_SERVER__TOKEN", "")
        from miloco.config import reset_settings

        reset_settings()
        from miloco.middleware.auth_middleware import verify_token_query_fallback
        from miloco.middleware.exceptions import AuthenticationException

        req = _mk_request()
        with pytest.raises(AuthenticationException):
            verify_token_query_fallback(req)

    def test_correct_header_passes(self, monkeypatch):
        monkeypatch.setenv("MILOCO_SERVER__TOKEN", "secret-123")
        from miloco.config import reset_settings

        reset_settings()
        from miloco.middleware.auth_middleware import verify_token_query_fallback

        req = _mk_request(headers={"Authorization": "Bearer secret-123"})
        verify_token_query_fallback(req)

    def test_correct_query_passes(self, monkeypatch):
        """EventSource 路径:?token=... query 参数."""
        monkeypatch.setenv("MILOCO_SERVER__TOKEN", "secret-123")
        from miloco.config import reset_settings

        reset_settings()
        from miloco.middleware.auth_middleware import verify_token_query_fallback

        req = _mk_request(query={"token": "secret-123"})
        verify_token_query_fallback(req)

    def test_missing_token_raises(self, monkeypatch):
        monkeypatch.setenv("MILOCO_SERVER__TOKEN", "secret-123")
        from miloco.config import reset_settings

        reset_settings()
        from miloco.middleware.auth_middleware import verify_token_query_fallback
        from miloco.middleware.exceptions import AuthenticationException

        req = _mk_request()
        with pytest.raises(AuthenticationException):
            verify_token_query_fallback(req)

    def test_wrong_header_raises(self, monkeypatch):
        monkeypatch.setenv("MILOCO_SERVER__TOKEN", "secret-123")
        from miloco.config import reset_settings

        reset_settings()
        from miloco.middleware.auth_middleware import verify_token_query_fallback
        from miloco.middleware.exceptions import AuthenticationException

        req = _mk_request(headers={"Authorization": "Bearer wrong"})
        with pytest.raises(AuthenticationException):
            verify_token_query_fallback(req)

    def test_wrong_query_raises(self, monkeypatch):
        monkeypatch.setenv("MILOCO_SERVER__TOKEN", "secret-123")
        from miloco.config import reset_settings

        reset_settings()
        from miloco.middleware.auth_middleware import verify_token_query_fallback
        from miloco.middleware.exceptions import AuthenticationException

        req = _mk_request(query={"token": "wrong"})
        with pytest.raises(AuthenticationException):
            verify_token_query_fallback(req)


# ─── 集成测试 /stream endpoint 鉴权 ────────────────────────


@pytest.fixture
def client(tmp_path, monkeypatch):
    """配合 FastAPI app + middleware,只用于验"被拒绝"的 401 路径(不实际握手 SSE)."""
    db_file = tmp_path / "test.db"
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(db_file))
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))

    from miloco.config import reset_settings

    reset_settings()
    import miloco.database.connector as connector_module
    import miloco.manager as manager_module

    connector_module.db_connector = None
    connector_module.init_database()
    manager_module.Manager._instance = None
    manager_module.manager_instance = None

    from miloco.middleware.exception_handler import handle_exception
    from miloco.perception.events_router import router as events_router

    app = FastAPI()

    @app.middleware("http")
    async def _catch_all(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001
            return handle_exception(request, exc)

    app.include_router(events_router, prefix="/api")
    yield TestClient(app)

    manager_module.Manager._instance = None
    manager_module.manager_instance = None
    connector_module.db_connector = None
    reset_settings()


class TestSSEEndpointAuth:
    def test_stream_rejects_when_token_configured_and_missing(
        self, client, monkeypatch
    ):
        """server.token 已配 + 不带任何 token → /stream 401."""
        monkeypatch.setenv("MILOCO_SERVER__TOKEN", "secret-123")
        from miloco.config import reset_settings

        reset_settings()
        resp = client.get("/api/events/stream")
        assert resp.status_code == 401

    def test_stream_rejects_wrong_token(self, client, monkeypatch):
        monkeypatch.setenv("MILOCO_SERVER__TOKEN", "secret-123")
        from miloco.config import reset_settings

        reset_settings()
        resp = client.get("/api/events/stream?token=wrong")
        assert resp.status_code == 401
        resp = client.get(
            "/api/events/stream", headers={"Authorization": "Bearer wrong"}
        )
        assert resp.status_code == 401

    def test_list_events_does_not_accept_query_token(self, client, monkeypatch):
        """普通 endpoint 仍只接受 header,不接受 query token(verify_token 严格 header-only)."""
        monkeypatch.setenv("MILOCO_SERVER__TOKEN", "secret-123")
        from miloco.config import reset_settings

        reset_settings()
        # query token 不被 verify_token 接受 → 401
        resp = client.get("/api/events?token=secret-123")
        assert resp.status_code == 401
        # header 通过
        resp = client.get(
            "/api/events", headers={"Authorization": "Bearer secret-123"}
        )
        assert resp.status_code == 200

    def test_session_cookie_can_authorize_query_fallback_dependency(
        self, tmp_path, monkeypatch
    ):
        """SSE/media dependencies accept a same-origin dashboard session."""
        from miloco.auth.router import router as auth_router
        from miloco.database.connector import init_database

        monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
        monkeypatch.setenv("MILOCO_DATABASE__PATH", str(tmp_path / "miloco.db"))
        monkeypatch.setenv("MILOCO_SERVER__TOKEN", "secret-123")
        import miloco.database.connector as connector_module
        from miloco.config import reset_settings

        connector_module.db_connector = None
        reset_settings()
        init_database()
        app = FastAPI()
        app.include_router(auth_router, prefix="/api")

        from miloco.middleware import verify_token_query_fallback

        @app.get(
            "/api/sse-auth",
            dependencies=[Depends(verify_token_query_fallback)],
        )
        def sse_auth_probe():
            return {"ok": True}

        client = TestClient(app)
        setup = client.post(
            "/api/auth/setup",
            json={
                "username": "lynx",
                "display_name": "Lynx",
                "password": "correct horse battery",
                "password_confirm": "correct horse battery",
            },
        )
        assert setup.status_code == 200

        # The concrete /events/stream generator is intentionally not opened here:
        # it has no terminating event under TestClient. This finite endpoint
        # exercises the identical SSE/media dependency without a query token.
        assert client.get("/api/sse-auth").status_code == 200
