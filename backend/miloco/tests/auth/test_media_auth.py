from types import SimpleNamespace

from fastapi import APIRouter, Depends, FastAPI, Request, WebSocket
from fastapi.testclient import TestClient
from miloco.auth.router import router as auth_router
from miloco.camera import router as camera_router_module
from miloco.camera.router import router as camera_router
from miloco.config import reset_settings
from miloco.database.connector import init_database
from miloco.middleware import verify_token_query_fallback, verify_websocket_token
from miloco.middleware.exception_handler import handle_exception


def _app() -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def _catch(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001
            return handle_exception(request, exc)

    media = APIRouter()

    @media.get("/media", dependencies=[Depends(verify_token_query_fallback)])
    def get_media():
        return {"ok": True}

    @media.websocket("/ws")
    async def websocket_media(
        websocket: WebSocket, _auth=Depends(verify_websocket_token)
    ) -> None:
        await websocket.accept()
        await websocket.send_text("ok")
        await websocket.close()

    app.include_router(auth_router, prefix="/api")
    app.include_router(media, prefix="/api")
    return app


def _reset_database_connector() -> None:
    import miloco.database.connector as connector_module

    connector_module.db_connector = None


def _logged_in_client(tmp_path, monkeypatch, application: FastAPI | None = None) -> TestClient:
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(tmp_path / "miloco.db"))
    monkeypatch.setenv("MILOCO_SERVER__TOKEN", "service-token")
    _reset_database_connector()
    reset_settings()
    init_database()
    client = TestClient(application or _app())
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
    return client


def test_cookie_session_can_load_media_without_query_token(tmp_path, monkeypatch) -> None:
    client = _logged_in_client(tmp_path, monkeypatch)

    response = client.get("/api/media")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_cookie_session_can_open_websocket_without_query_token(
    tmp_path, monkeypatch
) -> None:
    client = _logged_in_client(tmp_path, monkeypatch)

    with client.websocket_connect("/api/ws") as websocket:
        assert websocket.receive_text() == "ok"


class _CameraService:
    async def resolve_live_stream(self, _camera_id: str) -> None:
        return None


class _StreamHub:
    def subscribe(self, _camera_id: str):
        async def stream():
            yield b"frame"

        return stream()

    def state(self, _camera_id: str):
        return SimpleNamespace(error_code=None)


def test_cookie_session_can_open_camera_websocket_without_query_token(
    tmp_path, monkeypatch
) -> None:
    """Generic camera streams must use the same dashboard cookie boundary."""
    application = FastAPI()
    application.include_router(auth_router, prefix="/api")
    application.include_router(camera_router, prefix="/api")
    stream_hub = _StreamHub()
    application.dependency_overrides[camera_router_module._get_camera_service] = (
        _CameraService
    )
    application.dependency_overrides[camera_router_module._get_live_stream_hub] = (
        lambda: stream_hub
    )
    application.dependency_overrides[camera_router_module._get_live_jpeg_stream_hub] = (
        lambda: stream_hub
    )
    client = _logged_in_client(tmp_path, monkeypatch, application)

    with client.websocket_connect("/api/cameras/camera-1/stream") as websocket:
        assert websocket.receive_bytes() == b"frame"
