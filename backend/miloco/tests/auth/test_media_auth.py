import asyncio
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from miloco.admin import router as admin_router_module
from miloco.auth.router import router as auth_router
from miloco.camera import router as camera_router_module
from miloco.camera.router import router as camera_router
from miloco.config import reset_settings
from miloco.database.connector import init_database
from miloco.miot.router import router as miot_router
from miloco.perception import events_router as events_router_module
from miloco.perception import router as perception_router_module


def _reset_database_connector() -> None:
    import miloco.database.connector as connector_module

    connector_module.db_connector = None


def _logged_in_client(
    tmp_path: Path,
    monkeypatch,
    application: FastAPI,
    *,
    static_dir: Path | None = None,
) -> TestClient:
    monkeypatch.setenv("MILOCO_HOME", str(tmp_path))
    monkeypatch.setenv("MILOCO_DATABASE__PATH", str(tmp_path / "miloco.db"))
    monkeypatch.setenv("MILOCO_SERVER__TOKEN", "service-token")
    if static_dir is not None:
        monkeypatch.setenv("MILOCO_DIRECTORIES__STATIC", str(static_dir))
    _reset_database_connector()
    reset_settings()
    init_database()
    client = TestClient(application)
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


class _EventsService:
    def __init__(self, clip_path: Path, ref_path: Path) -> None:
        self.clip_path = clip_path
        self.ref_path = ref_path

    async def locate_clip(self, _event_id: str, _device_id: str):
        return "found", self.clip_path, "video/mp4", 1_700_000_000_000

    async def locate_ref(self, _event_id: str, _device_id: str):
        return "found", self.ref_path, 1_700_000_000_000


class _BoundedPipeline:
    def __init__(self) -> None:
        self.unsubscribed = False

    def subscribe_sse(self):
        return self

    async def get(self):
        raise asyncio.CancelledError

    def unsubscribe_sse(self, queue) -> None:
        assert queue is self
        self.unsubscribed = True


def test_cookie_session_can_load_miot_watch_without_query_token(
    tmp_path, monkeypatch
) -> None:
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "watch.html").write_text(
        '<script>const token = "__MILOCO_TOKEN__";</script>', encoding="utf-8"
    )
    application = FastAPI()
    application.include_router(auth_router, prefix="/api")
    application.include_router(miot_router, prefix="/api")
    client = _logged_in_client(
        tmp_path, monkeypatch, application, static_dir=static_dir
    )

    response = client.get("/api/miot/watch")

    assert response.status_code == 200
    assert "service-token" not in response.text
    assert "__MILOCO_TOKEN__" not in response.text


def test_cookie_session_can_load_event_clip_and_ref_without_query_token(
    tmp_path, monkeypatch
) -> None:
    clip_path = tmp_path / "clip.mp4"
    ref_path = tmp_path / "ref.jpg"
    clip_path.write_bytes(b"clip")
    ref_path.write_bytes(b"ref")
    application = FastAPI()
    application.include_router(auth_router, prefix="/api")
    application.include_router(events_router_module.router, prefix="/api")
    application.dependency_overrides[events_router_module.get_events_service] = (
        lambda: _EventsService(clip_path, ref_path)
    )
    client = _logged_in_client(tmp_path, monkeypatch, application)

    clip = client.get("/api/events/event-1/clip/device-1")
    ref = client.get("/api/events/event-1/ref/device-1")

    assert clip.status_code == 200
    assert clip.content == b"clip"
    assert ref.status_code == 200
    assert ref.content == b"ref"


def test_cookie_session_can_load_on_demand_clip_without_query_token(
    tmp_path, monkeypatch
) -> None:
    clip_path = tmp_path / "on-demand.mp4"
    clip_path.write_bytes(b"on-demand")
    application = FastAPI()
    application.include_router(auth_router, prefix="/api")
    application.include_router(perception_router_module.router, prefix="/api")
    monkeypatch.setattr(
        perception_router_module,
        "manager",
        SimpleNamespace(
            perception_service=SimpleNamespace(
                get_on_demand_log=lambda _log_id: {
                    "clip_dids": ["device-1"],
                    "timestamp": 1_700_000_000_000,
                }
            )
        ),
    )
    import miloco.perception.snapshot_writer as snapshot_writer

    monkeypatch.setattr(snapshot_writer, "get_snapshot_root", lambda: tmp_path)
    monkeypatch.setattr(
        snapshot_writer,
        "locate_clip_file",
        lambda _device_dir: (clip_path, "video/mp4"),
    )
    client = _logged_in_client(tmp_path, monkeypatch, application)

    response = client.get("/api/perception/on-demand-logs/log-1/clip/device-1")

    assert response.status_code == 200
    assert response.content == b"on-demand"


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


def test_cookie_session_can_open_admin_omni_stream_without_query_token(
    tmp_path, monkeypatch
) -> None:
    pipeline = _BoundedPipeline()
    application = FastAPI()
    application.include_router(auth_router, prefix="/api")
    application.include_router(admin_router_module.router, prefix="/api")
    monkeypatch.setattr(
        admin_router_module,
        "manager",
        SimpleNamespace(perception_service=SimpleNamespace(_pipeline=pipeline)),
    )
    client = _logged_in_client(tmp_path, monkeypatch, application)

    response = client.get("/api/admin/omni-config/stream")

    assert response.status_code == 200
    assert "event: omni_health" in response.text
    assert pipeline.unsubscribed is True
