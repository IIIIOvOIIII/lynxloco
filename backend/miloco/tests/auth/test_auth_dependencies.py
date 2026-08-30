from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from miloco.auth.router import router as auth_router
from miloco.auth.service import CSRF_HEADER_NAME
from miloco.config import reset_settings
from miloco.database.connector import init_database
from miloco.middleware import verify_token
from miloco.middleware.exception_handler import handle_exception
from miloco.schema.common_schema import NormalResponse


def _app() -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _catch(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:
            return handle_exception(request, exc)

    app.include_router(auth_router, prefix="/api")

    @app.get("/api/protected")
    def protected(_auth=Depends(verify_token)):
        return NormalResponse(code=0, message="ok", data={"ok": True})

    @app.post("/api/protected-write")
    def protected_write(_auth=Depends(verify_token)):
        return NormalResponse(code=0, message="ok", data={"ok": True})

    return app


def _reset_database_connector() -> None:
    import miloco.database.connector as connector_module

    connector_module.db_connector = None


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(tmp_path / "miloco.db"))
    monkeypatch.setenv("MILOCO_SERVER__TOKEN", "service-token")
    _reset_database_connector()
    reset_settings()
    init_database()
    return TestClient(_app())


def test_service_token_still_accesses_protected_routes(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    response = client.get(
        "/api/protected", headers={"Authorization": "Bearer service-token"}
    )

    assert response.status_code == 200


def test_dashboard_session_accesses_safe_routes_and_needs_csrf_for_writes(
    tmp_path, monkeypatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    setup = client.post(
        "/api/auth/setup",
        json={
            "username": "lynx",
            "display_name": "Lynx",
            "password": "correct horse battery",
            "password_confirm": "correct horse battery",
        },
    )
    csrf = setup.json()["data"]["csrf_token"]

    assert client.get("/api/protected").status_code == 200
    assert client.post("/api/protected-write").status_code == 403
    assert (
        client.post(
            "/api/protected-write", headers={CSRF_HEADER_NAME: csrf}
        ).status_code
        == 200
    )


def test_unauthenticated_request_is_401(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)

    assert client.get("/api/protected").status_code == 401
