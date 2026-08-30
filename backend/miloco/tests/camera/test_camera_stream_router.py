from __future__ import annotations

import asyncio
import base64
import io
import logging
import socket
import struct
import warnings
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest
import uvicorn
from fastapi import FastAPI, Request
from starlette.exceptions import StarletteDeprecationWarning
from starlette.websockets import WebSocketDisconnect
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.typing import Subprotocol

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="Using `httpx` with `starlette.testclient` is deprecated.*",
        category=StarletteDeprecationWarning,
    )
    from fastapi.testclient import TestClient

from miloco.camera.router import (
    _get_camera_service,
    _get_live_jpeg_stream_hub,
    _get_live_stream_hub,
    _watch_for_disconnect,
    camera_stream_websocket,
    router,
)
from miloco.camera.service import CameraConflictError, CameraNotFoundError
from miloco.camera.stream import LiveStreamHub, LiveStreamSource, LiveStreamState
from miloco.middleware.exception_handler import handle_exception

_FIXED_PROTOCOL = Subprotocol("miloco.camera.v1")


def _auth_protocol(token: str) -> Subprotocol:
    encoded = base64.urlsafe_b64encode(token.encode()).decode().rstrip("=")
    return Subprotocol(f"miloco.auth.{encoded}")


class _Service:
    def __init__(self, source_type: Literal["miot", "rtsp"] = "rtsp") -> None:
        self.source_type = source_type
        self.error: BaseException | None = None
        self.error_on_call: int | None = None
        self.resolved: list[str] = []

    async def resolve_live_stream(self, camera_id: str) -> LiveStreamSource:
        self.resolved.append(camera_id)
        if self.error is not None and (
            self.error_on_call is None or len(self.resolved) == self.error_on_call
        ):
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
def jpeg_hub() -> _Hub:
    return _Hub(chunks=(b"\xff\xd8jpeg-frame\xff\xd9",))


@pytest.fixture
def client(
    service: _Service,
    hub: _Hub,
    jpeg_hub: _Hub,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    monkeypatch.setattr(
        "miloco.auth.dependencies.get_settings",
        lambda: SimpleNamespace(server=SimpleNamespace(token="service-token")),
    )
    monkeypatch.setattr(
        "miloco.camera.router.get_settings",
        lambda: SimpleNamespace(
            directories=SimpleNamespace(
                static_dir=(
                    __import__("pathlib").Path(__file__).parents[4] / "web" / "public"
                )
            ),
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
    app.dependency_overrides[_get_live_jpeg_stream_hub] = lambda: jpeg_hub
    return TestClient(app)


def test_authenticated_rtsp_upgrade_sends_binary_annexb_and_detaches(
    client: TestClient, hub: _Hub
) -> None:
    with client.websocket_connect(
        "/api/cameras/rtsp%3Acamera/stream",
        subprotocols=[_FIXED_PROTOCOL, _auth_protocol("service-token")],
    ) as websocket:
        assert websocket.accepted_subprotocol == _FIXED_PROTOCOL
        assert websocket.receive_bytes() == b"\x00\x00\x00\x01\x65"

    assert hub.closed.is_set()
    assert hub.subscriptions == ["rtsp:camera"]


def test_jpeg_format_upgrade_routes_to_canvas_fallback_hub(
    client: TestClient,
    hub: _Hub,
    jpeg_hub: _Hub,
) -> None:
    with client.websocket_connect(
        "/api/cameras/rtsp%3Acamera/stream?format=jpeg",
        subprotocols=[_FIXED_PROTOCOL, _auth_protocol("service-token")],
    ) as websocket:
        assert websocket.accepted_subprotocol == _FIXED_PROTOCOL
        assert websocket.receive_bytes().startswith(b"\xff\xd8")

    assert hub.subscriptions == []
    assert jpeg_hub.closed.is_set()
    assert jpeg_hub.subscriptions == ["rtsp:camera"]


def test_unauthenticated_upgrade_is_rejected(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/cameras/rtsp%3Acamera/stream"):
            pass


def test_generic_upgrade_accepts_service_query_token(client: TestClient) -> None:
    with client.websocket_connect(
        "/api/cameras/rtsp%3Acamera/stream?token=service-token"
    ) as websocket:
        assert websocket.receive_bytes() == b"\x00\x00\x00\x01\x65"


@pytest.mark.asyncio
async def test_uvicorn_handshake_contract_and_access_logs_never_expose_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    special_token = "sp ace/+?汉字-secret"
    rejected_token = f"{special_token}-wrong"
    monkeypatch.setattr(
        "miloco.auth.dependencies.get_settings",
        lambda: SimpleNamespace(server=SimpleNamespace(token=special_token)),
    )
    service = _Service()
    hub = _Hub()
    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[_get_camera_service] = lambda: service
    app.dependency_overrides[_get_live_stream_hub] = lambda: hub
    app.dependency_overrides[_get_live_jpeg_stream_hub] = lambda: _Hub()

    access_log = io.StringIO()
    access_handler = logging.StreamHandler(access_log)
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.addHandler(access_handler)
    previous_level = access_logger.level
    access_logger.setLevel(logging.INFO)

    server_socket = socket.socket()
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen(128)
    server_socket.setblocking(False)
    port = server_socket.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_config=None, access_log=True, lifespan="off")
    )
    server_task = asyncio.create_task(server.serve(sockets=[server_socket]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        url = f"ws://127.0.0.1:{port}/api/cameras/rtsp%3Acamera/stream"

        with pytest.raises(InvalidStatus) as unauthenticated:
            async with connect(url):
                pass
        assert unauthenticated.value.response.status_code == 403

        with pytest.raises(InvalidStatus) as rejected:
            async with connect(
                url,
                subprotocols=[_FIXED_PROTOCOL, _auth_protocol(rejected_token)],
            ):
                pass
        assert rejected.value.response.status_code == 403

        async with connect(
            url,
            subprotocols=[_FIXED_PROTOCOL, _auth_protocol(special_token)],
        ) as websocket:
            assert websocket.subprotocol == _FIXED_PROTOCOL
            assert await websocket.recv() == b"\x00\x00\x00\x01\x65"

        service.error = CameraNotFoundError()
        async with connect(
            url,
            subprotocols=[_FIXED_PROTOCOL, _auth_protocol(special_token)],
        ) as websocket:
            with pytest.raises(ConnectionClosedError) as missing:
                await websocket.recv()
        assert missing.value.rcvd is not None
        assert missing.value.rcvd.code == 4404
        assert missing.value.rcvd.reason == "camera_not_found"
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=2)
        server_socket.close()
        access_logger.removeHandler(access_handler)
        access_logger.setLevel(previous_level)

    rendered_log = access_log.getvalue()
    for secret in (
        special_token,
        rejected_token,
        _auth_protocol(special_token),
        _auth_protocol(rejected_token),
    ):
        assert secret not in rendered_log


@pytest.mark.parametrize("camera_id", ["miot-camera", "miot-camera:ch1"])
def test_miot_camera_ids_route_through_the_unified_stream(
    client: TestClient, service: _Service, hub: _Hub, camera_id: str
) -> None:
    service.source_type = "miot"
    with client.websocket_connect(
        f"/api/cameras/{camera_id}/stream",
        headers={"Authorization": "Bearer service-token"},
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
            "/api/cameras/rtsp%3Acamera/stream",
            headers={"Authorization": "Bearer service-token"},
        ) as websocket:
            websocket.receive_bytes()

    assert raised.value.code == code
    assert raised.value.reason == reason
    assert "secret" not in raised.value.reason
    assert "rtsp://" not in raised.value.reason


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
def test_second_hub_resolve_failure_uses_stable_camera_close(
    client: TestClient,
    service: _Service,
    error: BaseException,
    code: int,
    reason: str,
) -> None:
    service.error = error
    service.error_on_call = 2
    client.app.dependency_overrides[_get_live_stream_hub] = lambda: LiveStreamHub(
        service.resolve_live_stream
    )

    with pytest.raises(WebSocketDisconnect) as raised:
        with client.websocket_connect(
            "/api/cameras/rtsp%3Acamera/stream",
            headers={"Authorization": "Bearer service-token"},
        ) as websocket:
            websocket.receive_bytes()

    assert service.resolved == ["rtsp:camera", "rtsp:camera"]
    assert raised.value.code == code
    assert raised.value.reason == reason
    assert "secret" not in raised.value.reason


@pytest.mark.parametrize(
    ("error_code", "expected_code", "expected_reason"),
    [
        ("camera_unavailable", 1013, "camera_unavailable"),
        ("transcode_failed", 1011, "transcode_failed"),
        ("rtsp://private/unknown", 1011, "stream_failed"),
    ],
)
def test_live_failure_closes_safely_and_detaches(
    client: TestClient,
    hub: _Hub,
    error_code: str,
    expected_code: int,
    expected_reason: str,
) -> None:
    hub.error = RuntimeError("rtsp://user:password@camera/live")
    hub.error_code = error_code

    with pytest.raises(WebSocketDisconnect) as raised:
        with client.websocket_connect(
            "/api/cameras/rtsp%3Acamera/stream",
            headers={"Authorization": "Bearer service-token"},
        ) as websocket:
            websocket.receive_bytes()

    assert raised.value.code == expected_code
    assert raised.value.reason == expected_reason
    assert "private" not in raised.value.reason
    assert "password" not in raised.value.reason
    assert hub.closed.is_set()


@pytest.mark.parametrize(
    ("error_code", "expected_code", "expected_reason"),
    [
        ("camera_unavailable", 1013, "camera_unavailable"),
        ("stream_unavailable", 1013, "stream_unavailable"),
        ("transcode_failed", 1011, "transcode_failed"),
        ("stream_failed", 1011, "stream_failed"),
        ("rtsp://private/unknown", 1011, "stream_failed"),
    ],
)
def test_normal_stream_end_maps_runtime_state_to_safe_close(
    client: TestClient,
    hub: _Hub,
    error_code: str,
    expected_code: int,
    expected_reason: str,
) -> None:
    hub.error_code = error_code

    with pytest.raises(WebSocketDisconnect) as raised:
        with client.websocket_connect(
            "/api/cameras/rtsp%3Acamera/stream",
            headers={"Authorization": "Bearer service-token"},
        ) as websocket:
            assert websocket.receive_bytes().startswith(b"\x00\x00\x00\x01")
            websocket.receive_bytes()

    assert raised.value.code == expected_code
    assert raised.value.reason == expected_reason
    assert "private" not in raised.value.reason


@pytest.mark.asyncio
async def test_abrupt_client_disconnect_closes_the_stream_iterator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "miloco.auth.dependencies.get_settings",
        lambda: SimpleNamespace(server=SimpleNamespace(token="service-token")),
    )

    class _DisconnectingWebSocket:
        headers = {"Authorization": "Bearer service-token"}
        query_params: dict[str, str] = {}
        cookies: dict[str, str] = {}

        def __init__(self) -> None:
            self.accepted = False
            self.closed: list[tuple[int, str]] = []

        async def accept(self, *, subprotocol: str | None = None) -> None:
            self.accepted = True

        async def receive(self) -> dict[str, str]:
            return {"type": "websocket.disconnect"}

        async def send_bytes(self, _chunk: bytes) -> None:
            await asyncio.Event().wait()

        async def close(self, *, code: int, reason: str) -> None:
            self.closed.append((code, reason))

    service = _Service()
    hub = _Hub(chunks=(b"\x00\x00\x00\x01\x65",))
    jpeg_hub = _Hub(chunks=(b"\xff\xd8jpeg-frame\xff\xd9",))
    websocket = _DisconnectingWebSocket()

    await camera_stream_websocket(
        cast(Any, websocket),
        "rtsp:camera",
        cast(Any, service),
        cast(Any, hub),
        cast(Any, jpeg_hub),
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


def test_watch_page_is_public_static_asset_and_never_injects_token(
    client: TestClient, service: _Service
) -> None:
    response = client.get("/api/cameras/rtsp%3Acamera/watch")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert service.resolved == []
    assert "service-token" not in response.text


@pytest.mark.parametrize(
    "error",
    [
        CameraNotFoundError(),
        CameraConflictError("camera_disabled", "Camera is disabled"),
    ],
)
def test_watch_page_does_not_resolve_or_disclose_camera_state(
    client: TestClient,
    service: _Service,
    error: BaseException,
) -> None:
    service.error = error
    response = client.get("/api/cameras/rtsp%3Acamera/watch")
    assert response.status_code == 200
    assert service.resolved == []


def test_stream_state_is_authenticated_and_returns_only_safe_live_fields(
    client: TestClient,
    service: _Service,
    hub: _Hub,
) -> None:
    hub.error_code = "transcode_failed"

    unauthenticated = client.get("/api/cameras/rtsp%3Acamera/stream/state")
    response = client.get(
        "/api/cameras/rtsp%3Acamera/stream/state",
        headers={"Authorization": "Bearer service-token"},
    )

    assert unauthenticated.status_code == 401
    assert response.status_code == 200
    assert response.json()["data"] == {
        "viewer_count": 0,
        "mode": "error",
        "input_codec": "h264",
        "output_codec": None,
        "queue_depth": 0,
        "dropped_packets": 0,
        "error_code": "transcode_failed",
    }
    assert service.resolved == ["rtsp:camera"]
    rendered = response.text
    assert "rtsp://" not in rendered
    assert "password" not in rendered
    assert "username" not in rendered


@pytest.mark.parametrize(
    ("error", "status_code", "safe_code"),
    [
        (CameraNotFoundError(), 404, "camera_not_found"),
        (
            CameraConflictError("camera_disabled", "Camera is disabled"),
            409,
            "camera_disabled",
        ),
    ],
)
def test_stream_state_preserves_missing_and_disabled_semantics(
    client: TestClient,
    service: _Service,
    error: BaseException,
    status_code: int,
    safe_code: str,
) -> None:
    service.error = error

    response = client.get(
        "/api/cameras/rtsp%3Acamera/stream/state",
        headers={"Authorization": "Bearer service-token"},
    )

    assert response.status_code == status_code
    assert response.json()["detail"]["code"] == safe_code


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
async def test_miot_backend_reattaches_while_first_start_is_pending() -> None:
    from miloco.manager import _MiotLiveStreamBackend

    class _LegacyManager:
        def __init__(self) -> None:
            self.starts = 0
            self.closes: list[str] = []
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()
            self.second_started = asyncio.Event()

        async def new_connection(self, **_kwargs) -> str:
            self.starts += 1
            if self.starts == 1:
                self.first_started.set()
                await self.release_first.wait()
            else:
                self.second_started.set()
            return f"connection-{self.starts}"

        async def close_connection(self, *, cid: str, **_kwargs) -> None:
            self.closes.append(cid)

    legacy = _LegacyManager()
    backend = _MiotLiveStreamBackend(legacy, "miot-camera", 0)
    first_detach = backend.add_packet_listener(lambda _packet: None)
    await asyncio.wait_for(legacy.first_started.wait(), timeout=0.2)

    first_detach()
    second_detach = backend.add_packet_listener(lambda _packet: None)
    legacy.release_first.set()

    await asyncio.wait_for(legacy.second_started.wait(), timeout=0.2)
    assert backend._connection_id == "connection-2"
    assert legacy.closes == ["connection-1"]

    second_detach()
    await asyncio.wait_for(backend.aclose(), timeout=0.2)


@pytest.mark.asyncio
async def test_miot_backend_keeps_pending_start_when_second_listener_attaches() -> None:
    from miloco.manager import _MiotLiveStreamBackend

    class _LegacyManager:
        def __init__(self) -> None:
            self.starts = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def new_connection(self, **_kwargs) -> str:
            self.starts += 1
            self.started.set()
            await self.release.wait()
            return "connection-id"

        async def close_connection(self, **_kwargs) -> None:
            return

    legacy = _LegacyManager()
    backend = _MiotLiveStreamBackend(legacy, "miot-camera", 0)
    first_detach = backend.add_packet_listener(lambda _packet: None)
    await asyncio.wait_for(legacy.started.wait(), timeout=0.2)
    second_detach = backend.add_packet_listener(lambda _packet: None)
    legacy.release.set()
    await asyncio.sleep(0)

    assert legacy.starts == 1
    assert backend._connection_id == "connection-id"

    first_detach()
    second_detach()
    await asyncio.wait_for(backend.aclose(), timeout=0.2)


@pytest.mark.asyncio
async def test_miot_backend_reattaches_while_active_connection_is_closing() -> None:
    from miloco.manager import _MiotLiveStreamBackend

    class _LegacyManager:
        def __init__(self) -> None:
            self.starts = 0
            self.first_close_started = asyncio.Event()
            self.release_first_close = asyncio.Event()
            self.second_started = asyncio.Event()

        async def new_connection(self, **_kwargs) -> str:
            self.starts += 1
            if self.starts == 2:
                self.second_started.set()
            return f"connection-{self.starts}"

        async def close_connection(self, *, cid: str, **_kwargs) -> None:
            if cid == "connection-1":
                self.first_close_started.set()
                await self.release_first_close.wait()

    legacy = _LegacyManager()
    backend = _MiotLiveStreamBackend(legacy, "miot-camera", 0)
    first_detach = backend.add_packet_listener(lambda _packet: None)
    await asyncio.sleep(0)
    assert backend._connection_id == "connection-1"

    first_detach()
    await asyncio.wait_for(legacy.first_close_started.wait(), timeout=0.2)
    second_detach = backend.add_packet_listener(lambda _packet: None)
    legacy.release_first_close.set()

    await asyncio.wait_for(legacy.second_started.wait(), timeout=0.2)
    assert backend._connection_id == "connection-2"

    second_detach()
    await asyncio.wait_for(backend.aclose(), timeout=0.2)


@pytest.mark.asyncio
async def test_miot_backend_sync_reattach_before_close_task_runs() -> None:
    from miloco.manager import _MiotLiveStreamBackend

    class _LegacyManager:
        def __init__(self) -> None:
            self.starts = 0
            self.second_started = asyncio.Event()

        async def new_connection(self, **_kwargs) -> str:
            self.starts += 1
            if self.starts == 2:
                self.second_started.set()
            return f"connection-{self.starts}"

        async def close_connection(self, **_kwargs) -> None:
            return

    legacy = _LegacyManager()
    backend = _MiotLiveStreamBackend(legacy, "miot-camera", 0)
    first_detach = backend.add_packet_listener(lambda _packet: None)
    await asyncio.sleep(0)
    assert backend._connection_id == "connection-1"

    first_detach()
    second_detach = backend.add_packet_listener(lambda _packet: None)

    await asyncio.wait_for(legacy.second_started.wait(), timeout=0.2)
    assert backend._connection_id == "connection-2"

    second_detach()
    await asyncio.wait_for(backend.aclose(), timeout=0.2)


@pytest.mark.asyncio
async def test_miot_backend_shutdown_cancels_hanging_start_and_is_bounded() -> None:
    from miloco.manager import _MiotLiveStreamBackend

    class _LegacyManager:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def new_connection(self, **_kwargs) -> str:
            self.started.set()
            try:
                never: asyncio.Future[str] = asyncio.Future()
                return await never
            finally:
                self.cancelled.set()

        async def close_connection(self, **_kwargs) -> None:
            raise AssertionError("a pending start has no connection to close")

    legacy = _LegacyManager()
    backend = _MiotLiveStreamBackend(legacy, "miot-camera", 0)
    backend.add_packet_listener(lambda _packet: None)
    await asyncio.wait_for(legacy.started.wait(), timeout=0.2)

    await asyncio.wait_for(backend.aclose(), timeout=0.2)

    assert legacy.cancelled.is_set()
    assert backend._connection_id is None
    assert backend._listeners == {}


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
