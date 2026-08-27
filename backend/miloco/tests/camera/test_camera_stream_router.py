from __future__ import annotations

import asyncio
import struct
import warnings
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest
from fastapi import FastAPI, Request
from starlette.exceptions import StarletteDeprecationWarning
from starlette.websockets import WebSocketDisconnect

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="Using `httpx` with `starlette.testclient` is deprecated.*",
        category=StarletteDeprecationWarning,
    )
    from fastapi.testclient import TestClient

from miloco.camera.router import (
    _get_camera_service,
    _get_live_stream_hub,
    _watch_for_disconnect,
    camera_stream_websocket,
    router,
)
from miloco.camera.service import CameraConflictError, CameraNotFoundError
from miloco.camera.stream import LiveStreamSource, LiveStreamState
from miloco.middleware.exception_handler import handle_exception


class _Service:
    def __init__(self, source_type: Literal["miot", "rtsp"] = "rtsp") -> None:
        self.source_type = source_type
        self.error: BaseException | None = None
        self.resolved: list[str] = []

    async def resolve_live_stream(self, camera_id: str) -> LiveStreamSource:
        self.resolved.append(camera_id)
        if self.error is not None:
            raise self.error
        return LiveStreamSource(
            camera_id=camera_id.split(":ch", 1)[0],
            source_type=self.source_type,
            backend=object(),
            channel=1 if ":ch1" in camera_id else 0,
            input_codec="h264" if self.source_type == "rtsp" else None,
        )


class _Hub:
    def __init__(self, chunks: tuple[bytes, ...] = (b"\x00\x00\x00\x01\x65",)) -> None:
        self.chunks = chunks
        self.closed = asyncio.Event()
        self.error: BaseException | None = None
        self.error_code: str | None = None
        self.subscriptions: list[str] = []

    async def subscribe(self, camera_id: str) -> AsyncGenerator[bytes, None]:
        self.subscriptions.append(camera_id)
        try:
            if self.error is not None:
                raise self.error
            for chunk in self.chunks:
                yield chunk
        finally:
            self.closed.set()

    def state(self, _camera_id: str) -> LiveStreamState:
        return LiveStreamState(
            viewer_count=0,
            mode="error" if self.error_code else "idle",
            input_codec="h264",
            output_codec=None,
            queue_depth=0,
            dropped_packets=0,
            error_code=self.error_code,
        )


@pytest.fixture
def service() -> _Service:
    return _Service()


@pytest.fixture
def hub() -> _Hub:
    return _Hub()


@pytest.fixture
def client(service: _Service, hub: _Hub, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(
        "miloco.middleware.auth_middleware.get_settings",
        lambda: SimpleNamespace(server=SimpleNamespace(token="service-token")),
    )
    monkeypatch.setattr(
        "miloco.camera.router.get_settings",
        lambda: SimpleNamespace(
            directories=SimpleNamespace(
                static_dir=(
                    __import__("pathlib").Path(__file__).parents[4] / "web" / "public"
                )
            )
        ),
    )
    app = FastAPI()

    @app.middleware("http")
    async def catch_all_exceptions(request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as error:
            return handle_exception(request, error)

    app.include_router(router, prefix="/api")
    app.dependency_overrides[_get_camera_service] = lambda: service
    app.dependency_overrides[_get_live_stream_hub] = lambda: hub
    return TestClient(app)


def test_authenticated_rtsp_upgrade_sends_binary_annexb_and_detaches(
    client: TestClient, hub: _Hub
) -> None:
    with client.websocket_connect(
        "/api/cameras/rtsp%3Acamera/stream?token=service-token"
    ) as websocket:
        assert websocket.receive_bytes() == b"\x00\x00\x00\x01\x65"

    assert hub.closed.is_set()
    assert hub.subscriptions == ["rtsp:camera"]


def test_unauthenticated_upgrade_is_rejected(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/cameras/rtsp%3Acamera/stream"):
            pass


@pytest.mark.parametrize("camera_id", ["miot-camera", "miot-camera:ch1"])
def test_miot_camera_ids_route_through_the_unified_stream(
    client: TestClient, service: _Service, hub: _Hub, camera_id: str
) -> None:
    service.source_type = "miot"
    with client.websocket_connect(
        f"/api/cameras/{camera_id}/stream?token=service-token"
    ) as websocket:
        assert websocket.receive_bytes().startswith(b"\x00\x00\x00\x01")

    assert service.resolved[-1] == camera_id


@pytest.mark.parametrize(
    ("error", "code", "reason"),
    [
        (CameraNotFoundError(), 4404, "camera_not_found"),
        (
            CameraConflictError("camera_disabled", "Camera is disabled"),
            4403,
            "camera_disabled",
        ),
        (
            CameraConflictError("camera_unavailable", "private rtsp://secret"),
            1013,
            "camera_unavailable",
        ),
    ],
)
def test_resolution_failures_close_with_stable_safe_codes(
    client: TestClient,
    service: _Service,
    error: BaseException,
    code: int,
    reason: str,
) -> None:
    service.error = error

    with pytest.raises(WebSocketDisconnect) as raised:
        with client.websocket_connect(
            "/api/cameras/rtsp%3Acamera/stream?token=service-token"
        ):
            pass

    assert raised.value.code == code
    assert raised.value.reason == reason
    assert "secret" not in raised.value.reason
    assert "rtsp://" not in raised.value.reason


def test_live_failure_closes_safely_and_detaches(client: TestClient, hub: _Hub) -> None:
    hub.error = RuntimeError("rtsp://user:password@camera/live")
    hub.error_code = "transcode_failed"

    with pytest.raises(WebSocketDisconnect) as raised:
        with client.websocket_connect(
            "/api/cameras/rtsp%3Acamera/stream?token=service-token"
        ) as websocket:
            websocket.receive_bytes()

    assert raised.value.code == 1011
    assert raised.value.reason == "transcode_failed"
    assert hub.closed.is_set()


@pytest.mark.asyncio
async def test_abrupt_client_disconnect_closes_the_stream_iterator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "miloco.middleware.auth_middleware.get_settings",
        lambda: SimpleNamespace(server=SimpleNamespace(token="service-token")),
    )

    class _DisconnectingWebSocket:
        headers: dict[str, str] = {}
        query_params = {"token": "service-token"}

        def __init__(self) -> None:
            self.accepted = False
            self.closed: list[tuple[int, str]] = []

        async def accept(self) -> None:
            self.accepted = True

        async def receive(self) -> dict[str, str]:
            return {"type": "websocket.disconnect"}

        async def send_bytes(self, _chunk: bytes) -> None:
            await asyncio.Event().wait()

        async def close(self, *, code: int, reason: str) -> None:
            self.closed.append((code, reason))

    service = _Service()
    hub = _Hub(chunks=(b"\x00\x00\x00\x01\x65",))
    websocket = _DisconnectingWebSocket()

    await camera_stream_websocket(
        cast(Any, websocket),
        "rtsp:camera",
        cast(Any, service),
        cast(Any, hub),
    )

    assert websocket.accepted is True
    assert hub.closed.is_set()
    assert websocket.closed == [(1000, "")]


@pytest.mark.asyncio
async def test_disconnect_watcher_does_not_swallow_unexpected_receive_errors() -> None:
    class _BrokenWebSocket:
        async def receive(self) -> None:
            raise RuntimeError("receive failed")

    with pytest.raises(RuntimeError, match="receive failed"):
        await _watch_for_disconnect(cast(Any, _BrokenWebSocket()))


def test_watch_page_requires_auth_resolves_camera_and_never_injects_token(
    client: TestClient, service: _Service
) -> None:
    unauthorized = client.get("/api/cameras/rtsp%3Acamera/watch")
    assert unauthorized.status_code == 401

    response = client.get("/api/cameras/rtsp%3Acamera/watch?token=service-token")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert service.resolved[-1] == "rtsp:camera"
    assert "service-token" not in response.text


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (CameraNotFoundError(), 404, "camera_not_found"),
        (
            CameraConflictError("camera_disabled", "Camera is disabled"),
            409,
            "camera_disabled",
        ),
    ],
)
def test_watch_page_uses_existing_safe_http_error_contract(
    client: TestClient,
    service: _Service,
    error: BaseException,
    status: int,
    code: str,
) -> None:
    service.error = error
    response = client.get("/api/cameras/rtsp%3Acamera/watch?token=service-token")
    assert response.status_code == status
    assert response.json()["detail"]["code"] == code


@pytest.mark.asyncio
async def test_manager_owns_one_hub_and_shutdown_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from miloco.manager import Manager

    manager = object.__new__(Manager)
    service = SimpleNamespace(resolve_live_stream=lambda camera_id: camera_id)
    manager._camera_service = service
    manager._initialize_live_stream_hub()
    first = manager.live_stream_hub
    manager._initialize_live_stream_hub()

    closed: list[str] = []
    first._feeds["rtsp:camera"] = SimpleNamespace()  # type: ignore[assignment]

    async def close_camera(camera_id: str) -> None:
        closed.append(camera_id)
        first._feeds.pop(camera_id, None)

    monkeypatch.setattr(first, "close_camera", close_camera)
    await manager.shutdown_live_streams()
    await manager.shutdown_live_streams()

    assert manager.live_stream_hub is first
    assert closed == ["rtsp:camera"]


@pytest.mark.asyncio
async def test_miot_backend_strips_legacy_header_and_detaches() -> None:
    from miloco.manager import _MiotLiveStreamBackend

    class _LegacyManager:
        def __init__(self) -> None:
            self.websocket = None
            self.closed: list[tuple[str, int, str]] = []

        async def new_connection(
            self, *, websocket, camera_id: str, channel: int, **_kwargs
        ) -> str:
            self.websocket = websocket
            return "connection-id"

        async def close_connection(
            self, *, camera_id: str, channel: int, cid: str, **_kwargs
        ) -> None:
            self.closed.append((camera_id, channel, cid))

    legacy = _LegacyManager()
    backend = _MiotLiveStreamBackend(legacy, "miot-camera", 1)
    received = []
    detach = backend.add_packet_listener(received.append)
    await asyncio.sleep(0)

    payload = b"\x00\x00\x00\x01\x65\x88"
    assert legacy.websocket is not None
    await legacy.websocket.send_bytes(struct.pack(">B7xQ", 1, 42) + payload)
    detach()
    await backend.aclose()

    assert len(received) == 1
    assert received[0].codec == "h264"
    assert received[0].data == payload
    assert received[0].is_keyframe is True
    assert received[0].pts == 42
    assert legacy.closed == [("miot-camera", 1, "connection-id")]


@pytest.mark.asyncio
async def test_miot_backend_restarts_after_the_last_viewer_detaches() -> None:
    from miloco.manager import _MiotLiveStreamBackend

    class _LegacyManager:
        def __init__(self) -> None:
            self.starts = 0
            self.closes = 0

        async def new_connection(self, **_kwargs) -> str:
            self.starts += 1
            return f"connection-{self.starts}"

        async def close_connection(self, **_kwargs) -> None:
            self.closes += 1

    legacy = _LegacyManager()
    backend = _MiotLiveStreamBackend(legacy, "miot-camera", 0)
    first_detach = backend.add_packet_listener(lambda _packet: None)
    await asyncio.sleep(0)
    first_detach()
    await asyncio.sleep(0)

    second_detach = backend.add_packet_listener(lambda _packet: None)
    await asyncio.sleep(0)
    second_detach()
    await backend.aclose()

    assert legacy.starts == 2
    assert legacy.closes == 2


@pytest.mark.asyncio
async def test_miot_backend_restart_does_not_deadlock_pending_detach() -> None:
    from miloco.manager import _MiotLiveStreamBackend

    class _LegacyManager:
        async def new_connection(self, **_kwargs) -> str:
            return "connection-id"

        async def close_connection(self, **_kwargs) -> None:
            await asyncio.sleep(0)

    backend = _MiotLiveStreamBackend(_LegacyManager(), "miot-camera", 0)
    first_detach = backend.add_packet_listener(lambda _packet: None)
    await asyncio.sleep(0)
    first_detach()
    second_detach = backend.add_packet_listener(lambda _packet: None)
    await asyncio.sleep(0)
    second_detach()

    await asyncio.wait_for(backend.aclose(), timeout=0.2)


@pytest.mark.asyncio
async def test_lifespan_shutdown_logs_hub_failure_without_interrupting(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from miloco import main

    class _Manager:
        async def shutdown_live_streams(self) -> None:
            raise RuntimeError("safe synthetic failure")

    monkeypatch.setattr(main, "get_manager", lambda: _Manager())
    await main._shutdown_camera_live_streams()
    assert "Failed to stop camera live streams" in caplog.text
