from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import signal
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import av
import pytest
import uvicorn
from fastapi import FastAPI, Header, WebSocket
from miloco.camera.router import (
    _get_camera_service,
    _get_live_jpeg_stream_hub,
    _get_live_stream_hub,
    router,
)
from miloco.camera.service import CameraService
from miloco.camera.stream import LiveJpegStreamHub, LiveStreamHub
from miloco.config.settings import RtspSourceSettings
from miloco.perception.collect import rtsp_session as session_module
from miloco.perception.collect.camera_adapter import CameraDeviceAdapter
from miloco.perception.collect.collector import MultimodalCollector
from miloco.perception.collect.rtsp_camera_source import RtspCameraSource
from miloco.perception.schema import DeviceData
from websockets.asyncio.client import connect
from websockets.typing import Subprotocol

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "rtsp"
_FIXED_PROTOCOL = Subprotocol("miloco.camera.v1")


def _auth_protocol(token: str) -> Subprotocol:
    encoded = base64.urlsafe_b64encode(token.encode()).decode().rstrip("=")
    return Subprotocol(f"miloco.auth.{encoded}")


class _ControlledFixture:
    """Pace a real PyAV container at viewer-lifecycle checkpoints."""

    def __init__(
        self,
        container: av.container.InputContainer,
        *,
        first_pause_packet: int,
    ) -> None:
        self._container = container
        self._first_pause_packet = first_pause_packet
        self.release_start = threading.Event()
        self.release_after_first = threading.Event()
        self.release_after_six = threading.Event()
        self.after_first = threading.Event()
        self.after_six = threading.Event()
        self.eof = threading.Event()
        self.closed = threading.Event()

    @property
    def streams(self) -> Any:
        return self._container.streams

    def demux(self) -> Iterator[av.Packet]:
        self.release_start.wait(timeout=5)
        video_packets = 0
        for packet in self._container.demux():
            yield packet
            if packet.stream.type != "video" or not bytes(packet):
                continue
            video_packets += 1
            time.sleep(0.04)
            if video_packets == self._first_pause_packet:
                self.after_first.set()
                self.release_after_first.wait(timeout=5)
            elif video_packets == 6:
                self.after_six.set()
                self.release_after_six.wait(timeout=5)
        self.eof.set()
        self.closed.wait(timeout=5)
        self._container.close()

    def close(self) -> None:
        self.closed.set()


class _CountingFixtureOpener:
    def __init__(
        self,
        fixture: Path,
        open_fixture: Any,
        *,
        first_pause_packet: int,
    ) -> None:
        self._fixture = fixture
        self._open_fixture = open_fixture
        self._first_pause_packet = first_pause_packet
        self.open_count = 0
        self.controlled: _ControlledFixture | None = None

    def __call__(self, _url: str, **_options: object) -> Any:
        self.open_count += 1
        controlled = _ControlledFixture(
            self._open_fixture(str(self._fixture)),
            first_pause_packet=self._first_pause_packet,
        )
        self.controlled = controlled
        return controlled


class _MiotService:
    async def list_cameras_with_state(self) -> list[dict[str, object]]:
        return []


class _PerceptionRegistry:
    def __init__(self, source: RtspCameraSource) -> None:
        self._rtsp_camera_source = source


async def _wait_until(predicate: Any, *, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("Timed out waiting for integration state")


async def _wait_for_device_data(
    collector: MultimodalCollector,
    camera_id: str,
    *,
    minimum_frames: int,
    timeout: float = 3.0,
) -> DeviceData:
    result: DeviceData | None = None

    def ready() -> bool:
        nonlocal result
        result = collector.collect(camera_id, drain=False)
        return result is not None and len(result.video) >= minimum_frames

    await _wait_until(ready, timeout=timeout)
    assert result is not None
    return result


def _latest_video_stream_ts(data: DeviceData) -> int:
    assert data.video
    return max(frame.stream_ts for frame in data.video)


def _smoke_script_root() -> Path:
    current = Path(__file__).resolve()
    while True:
        if (current / "scripts" / "rtsp-view-smoke.sh").is_file():
            return current
        if current == current.parent:
            raise FileNotFoundError("could not locate scripts/rtsp-view-smoke.sh")
        current = current.parent


async def _wait_for_newer_video(
    collector: MultimodalCollector,
    camera_id: str,
    *,
    previous_stream_ts: int,
) -> DeviceData:
    result: DeviceData | None = None

    def ready() -> bool:
        nonlocal result
        result = collector.collect(camera_id, drain=False)
        return (
            result is not None
            and bool(result.video)
            and _latest_video_stream_ts(result) > previous_stream_ts
        )

    await _wait_until(ready)
    assert result is not None
    return result


async def _run_view_smoke(
    *,
    miloco_home: Path,
    camera_id: str,
    backend_url: str,
    duration_sec: int,
    environment_overrides: dict[str, str] | None = None,
    timeout: float = 5.0,
) -> tuple[int, str, str]:
    environment = os.environ.copy()
    environment.pop("MILOCO_CONFIG_SEARCH_PATH", None)
    environment.pop("MILOCO_SERVER__TOKEN", None)
    environment["MILOCO_HOME"] = str(miloco_home)
    environment["MILOCO_RTSP_VIEW_SMOKE_DURATION_SEC"] = str(duration_sec)
    if environment_overrides is not None:
        environment.update(environment_overrides)
    smoke_root = _smoke_script_root()
    process = await asyncio.create_subprocess_exec(
        str(smoke_root / "scripts" / "rtsp-view-smoke.sh"),
        camera_id,
        backend_url,
        cwd=smoke_root,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await asyncio.wait_for(
        process.communicate(), timeout=timeout
    )
    assert process.returncode is not None
    return process.returncode, stdout_bytes.decode(), stderr_bytes.decode()


def _write_smoke_config(home: Path, token: str, *, mode: int = 0o600) -> Path:
    home.mkdir(mode=0o700, exist_ok=True)
    config_path = home / "config.json"
    config_path.write_text(
        json.dumps({"server": {"token": token}}),
        encoding="utf-8",
    )
    config_path.chmod(mode)
    return config_path


def _decode_h264(chunk: bytes) -> list[av.VideoFrame]:
    decoder = av.CodecContext.create("h264", "r")
    frames = list(decoder.decode(av.Packet(chunk)))
    frames.extend(decoder.decode(None))
    return frames


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "fixture_name",
        "camera_id",
        "expected_input_codec",
        "expected_mode",
        "first_pause_packet",
    ),
    [
        (
            "h264_video_audio.mkv",
            "rtsp:00000000-0000-0000-0000-000000000901",
            "h264",
            "transcoding",
            1,
        ),
        (
            "h265_video_only.mkv",
            "rtsp:00000000-0000-0000-0000-000000000902",
            "hevc",
            "transcoding",
            3,
        ),
    ],
)
async def test_fixture_perception_and_uvicorn_live_view_share_one_rtsp_session(
    monkeypatch: pytest.MonkeyPatch,
    fixture_name: str,
    camera_id: str,
    expected_input_codec: str,
    expected_mode: str,
    first_pause_packet: int,
) -> None:
    token = f"fixture /+?\u6c49-secret-{expected_input_codec}"
    setting = RtspSourceSettings(
        id=camera_id,
        name=f"fixture-{expected_input_codec}",
        room_name="live-view-integration",
        uri="rtsp://fixture.invalid/stream",
        username="fixture-user",
        password="fixture-password",
        audio_enabled=True,
        enabled=True,
    )
    source = RtspCameraSource(lambda: [setting])
    adapter = CameraDeviceAdapter(sources=[source], perception_fps_provider=lambda: 25)
    collector = MultimodalCollector([adapter])
    settings = SimpleNamespace(camera=SimpleNamespace(rtsp_sources=[setting]))
    service = CameraService(
        cast(Any, _MiotService()),
        cast(Any, _PerceptionRegistry(source)),
        settings_loader=lambda: settings,
    )
    hub = LiveStreamHub(service.resolve_live_stream, queue_size=2)

    original_open = session_module.av.open
    opener = _CountingFixtureOpener(
        _FIXTURES / fixture_name,
        original_open,
        first_pause_packet=first_pause_packet,
    )
    monkeypatch.setattr(session_module.av, "open", opener)
    monkeypatch.setattr(
        "miloco.auth.dependencies.get_settings",
        lambda: SimpleNamespace(server=SimpleNamespace(token=token)),
    )

    app = FastAPI()
    app.include_router(router, prefix="/api")
    app.dependency_overrides[_get_camera_service] = lambda: service
    app.dependency_overrides[_get_live_stream_hub] = lambda: hub
    app.dependency_overrides[_get_live_jpeg_stream_hub] = lambda: LiveJpegStreamHub(
        service.resolve_live_stream
    )

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
    slow_viewer = None
    slow_first: asyncio.Task[bytes] | None = None
    try:
        await collector.sync_all_devices()
        await _wait_until(
            lambda: source.get_state(camera_id).video_codec == expected_input_codec
        )
        assert opener.controlled is not None
        controlled = opener.controlled

        await _wait_until(lambda: server.started)
        slow_viewer = hub.subscribe(camera_id)
        slow_first = asyncio.create_task(anext(slow_viewer))
        await _wait_until(lambda: hub.state(camera_id).viewer_count == 1)

        url = f"ws://127.0.0.1:{port}/api/cameras/{camera_id}/stream"
        async with connect(
            url,
            subprotocols=[_FIXED_PROTOCOL, _auth_protocol(token)],
        ) as websocket:
            assert websocket.subprotocol == _FIXED_PROTOCOL
            await _wait_until(lambda: hub.state(camera_id).viewer_count == 2)
            controlled.release_start.set()
            live_chunk = cast(bytes, await asyncio.wait_for(websocket.recv(), 3))
            slow_chunk = await asyncio.wait_for(slow_first, 3)
            assert live_chunk == slow_chunk
            await asyncio.to_thread(controlled.after_first.wait, 3)
            first_data = await _wait_for_device_data(
                collector, camera_id, minimum_frames=1
            )
            assert first_data.meta.did == camera_id
            decoded = _decode_h264(live_chunk)
            assert decoded
            assert {(frame.width, frame.height) for frame in decoded} == {(64, 48)}
            active_state = hub.state(camera_id)
            assert active_state.input_codec == expected_input_codec
            assert active_state.output_codec == "h264"
            assert active_state.mode == expected_mode

        await _wait_until(lambda: hub.state(camera_id).viewer_count == 1)
        controlled.release_after_first.set()
        await asyncio.to_thread(controlled.after_six.wait, 3)
        first_latest_stream_ts = _latest_video_stream_ts(first_data)
        mid_data = await _wait_for_newer_video(
            collector,
            camera_id,
            previous_stream_ts=first_latest_stream_ts,
        )
        await _wait_until(lambda: hub.state(camera_id).dropped_packets > 0)
        slow_state = hub.state(camera_id)
        assert slow_state.viewer_count == 1
        assert slow_state.queue_depth <= 2
        assert slow_state.dropped_packets > 0
        mid_latest_stream_ts = _latest_video_stream_ts(mid_data)
        assert mid_latest_stream_ts > first_latest_stream_ts

        await slow_viewer.aclose()
        slow_viewer = None
        idle_state = hub.state(camera_id)
        assert idle_state.viewer_count == 0
        assert idle_state.mode == "idle"
        assert idle_state.queue_depth == 0

        controlled.release_after_six.set()
        await asyncio.to_thread(controlled.eof.wait, 3)
        final_data = await _wait_for_newer_video(
            collector,
            camera_id,
            previous_stream_ts=mid_latest_stream_ts,
        )
        assert _latest_video_stream_ts(final_data) > mid_latest_stream_ts
        assert opener.open_count == 1
    finally:
        if slow_first is not None and not slow_first.done():
            slow_first.cancel()
            await asyncio.gather(slow_first, return_exceptions=True)
        if slow_viewer is not None:
            await slow_viewer.aclose()
        if opener.controlled is not None:
            opener.controlled.release_start.set()
            opener.controlled.release_after_first.set()
            opener.controlled.release_after_six.set()
            opener.controlled.closed.set()
        await hub.close_camera(camera_id)
        await collector.shutdown()
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=2)
        server_socket.close()
        access_logger.removeHandler(access_handler)
        access_logger.setLevel(previous_level)

    rendered_log = access_log.getvalue()
    assert token not in rendered_log
    assert str(_auth_protocol(token)) not in rendered_log
    assert setting.uri not in rendered_log
    assert setting.password not in rendered_log


@pytest.mark.asyncio
async def test_view_smoke_measures_real_websocket_and_safe_state(
    tmp_path: Path,
) -> None:
    token = "smoke /+?-secret"
    camera_id = "rtsp:00000000-0000-0000-0000-000000000903"
    websocket_active = False
    cpu_samples = iter((12.5, 18.0))
    app = FastAPI()

    @app.get("/api/monitor/resources")
    async def resources(authorization: str | None = Header(default=None)) -> dict:
        assert authorization == f"Bearer {token}"
        return {"cpu_pct": next(cpu_samples)}

    @app.get("/api/cameras/{requested_camera_id}/stream/state")
    async def stream_state(
        requested_camera_id: str,
        authorization: str | None = Header(default=None),
    ) -> dict:
        assert authorization == f"Bearer {token}"
        assert requested_camera_id == camera_id
        return {
            "code": 0,
            "data": {
                "viewer_count": 1 if websocket_active else 0,
                "mode": "passthrough",
                "input_codec": "h264",
                "output_codec": "h264",
                "queue_depth": 2,
                "dropped_packets": 3,
                "error_code": None,
            },
        }

    @app.websocket("/api/cameras/{requested_camera_id}/stream")
    async def stream(websocket: WebSocket, requested_camera_id: str) -> None:
        nonlocal websocket_active
        assert requested_camera_id == camera_id
        offered = websocket.headers.get("sec-websocket-protocol", "")
        assert _FIXED_PROTOCOL in offered
        assert _auth_protocol(token) in offered
        await websocket.accept(subprotocol=_FIXED_PROTOCOL)
        websocket_active = True
        try:
            for index in range(20):
                await websocket.send_bytes(b"\x00\x00\x00\x01\x65" + bytes([index]))
                await asyncio.sleep(0.05)
        finally:
            websocket_active = False

    server_socket = socket.socket()
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen(128)
    server_socket.setblocking(False)
    port = server_socket.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_config=None, access_log=False, lifespan="off")
    )
    server_task = asyncio.create_task(server.serve(sockets=[server_socket]))
    try:
        await _wait_until(lambda: server.started)
        _write_smoke_config(tmp_path, token)
        tmp_path.chmod(0o755)
        returncode, stdout, stderr = await _run_view_smoke(
            miloco_home=tmp_path,
            camera_id=camera_id,
            backend_url=f"http://127.0.0.1:{port}",
            duration_sec=1,
            environment_overrides={"MILOCO_SERVER__TOKEN": "ignored-env-secret"},
        )
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=2)
        server_socket.close()

    assert returncode == 0, stderr
    assert "first_frame_latency_ms=" in stdout
    sample_field = next(
        field for field in stdout.split() if field.startswith("sample_seconds=")
    )
    sample_seconds = float(sample_field.partition("=")[2])
    assert 0.8 <= sample_seconds <= 1.5
    assert "output_fps=" in stdout
    assert "process_cpu_pct_delta=5.5" in stdout
    assert "viewer_count=1" in stdout
    assert "queue_depth=2" in stdout
    assert "queue_drops=3" in stdout
    assert token not in stdout + stderr
    assert "ignored-env-secret" not in stdout + stderr
    assert "fixture-password" not in stdout + stderr


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "env_only",
        "world_readable",
        "symlink",
        "unsafe_parent",
        "invalid_json",
        "invalid_schema",
        "missing_token",
        "wrong_owner",
    ],
)
async def test_view_smoke_rejects_unsafe_or_missing_persisted_token(
    tmp_path: Path,
    case: str,
) -> None:
    token = f"private-{case}-token"
    home = tmp_path / "miloco-home"
    home.mkdir(mode=0o700)
    overrides: dict[str, str] | None = None
    if case == "env_only":
        overrides = {"MILOCO_SERVER__TOKEN": token}
    elif case == "world_readable":
        _write_smoke_config(home, token, mode=0o644)
    elif case == "symlink":
        target = tmp_path / "real-config.json"
        target.write_text(json.dumps({"server": {"token": token}}), encoding="utf-8")
        target.chmod(0o600)
        (home / "config.json").symlink_to(target)
    elif case == "unsafe_parent":
        _write_smoke_config(home, token)
        home.chmod(0o770)
    elif case == "invalid_json":
        config_path = home / "config.json"
        config_path.write_text("{invalid", encoding="utf-8")
        config_path.chmod(0o600)
    elif case == "invalid_schema":
        config_path = home / "config.json"
        config_path.write_text(
            json.dumps({"server": {"token": token, "tls_verify": "false"}}),
            encoding="utf-8",
        )
        config_path.chmod(0o600)
    elif case == "wrong_owner":
        if os.geteuid() != 0:
            pytest.skip("wrong-owner config requires root to simulate")
        config_path = _write_smoke_config(home, token)
        os.chown(config_path, 1, -1)
    else:
        config_path = home / "config.json"
        config_path.write_text('{"server":{}}', encoding="utf-8")
        config_path.chmod(0o600)

    returncode, stdout, stderr = await _run_view_smoke(
        miloco_home=home,
        camera_id="rtsp:unsafe-config",
        backend_url="http://127.0.0.1:1",
        duration_sec=1,
        environment_overrides=overrides,
    )

    assert returncode == 2
    assert stdout == ""
    assert stderr.strip() == "persisted backend configuration is unavailable"
    assert token not in stdout + stderr
    assert str(home) not in stdout + stderr


@pytest.mark.asyncio
async def test_view_smoke_fails_when_websocket_closes_after_first_frame(
    tmp_path: Path,
) -> None:
    token = "early-close-secret"
    camera_id = "rtsp:early-close"
    app = FastAPI()

    @app.get("/api/monitor/resources")
    async def resources(authorization: str | None = Header(default=None)) -> dict:
        assert authorization == f"Bearer {token}"
        return {"cpu_pct": 10.0}

    @app.websocket("/api/cameras/{requested_camera_id}/stream")
    async def stream(websocket: WebSocket, requested_camera_id: str) -> None:
        assert requested_camera_id == camera_id
        await websocket.accept(subprotocol=_FIXED_PROTOCOL)
        await websocket.send_bytes(b"\x00\x00\x00\x01\x65\x00")

    server_socket = socket.socket()
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen(128)
    server_socket.setblocking(False)
    port = server_socket.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_config=None, access_log=False, lifespan="off")
    )
    server_task = asyncio.create_task(server.serve(sockets=[server_socket]))
    try:
        await _wait_until(lambda: server.started)
        _write_smoke_config(tmp_path, token)
        returncode, stdout, stderr = await _run_view_smoke(
            miloco_home=tmp_path,
            camera_id=camera_id,
            backend_url=f"http://127.0.0.1:{port}",
            duration_sec=30,
        )
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=2)
        server_socket.close()

    assert returncode == 4
    assert stdout == ""
    assert stderr.strip() == "live WebSocket closed before measurement completed"
    assert token not in stdout + stderr


@pytest.mark.asyncio
async def test_view_smoke_signal_exits_nonzero_and_closes_websocket(
    tmp_path: Path,
) -> None:
    token = "signal-cleanup-secret"
    camera_id = "rtsp:signal-cleanup"
    websocket_active = False
    app = FastAPI()

    @app.get("/api/monitor/resources")
    async def resources(authorization: str | None = Header(default=None)) -> dict:
        assert authorization == f"Bearer {token}"
        return {"cpu_pct": 10.0}

    @app.websocket("/api/cameras/{requested_camera_id}/stream")
    async def stream(websocket: WebSocket, requested_camera_id: str) -> None:
        nonlocal websocket_active
        assert requested_camera_id == camera_id
        await websocket.accept(subprotocol=_FIXED_PROTOCOL)
        websocket_active = True
        try:
            while True:
                await websocket.send_bytes(b"\x00\x00\x00\x01\x65\x00")
                await asyncio.sleep(0.05)
        except Exception:
            pass
        finally:
            websocket_active = False

    server_socket = socket.socket()
    server_socket.bind(("127.0.0.1", 0))
    server_socket.listen(128)
    server_socket.setblocking(False)
    port = server_socket.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_config=None, access_log=False, lifespan="off")
    )
    server_task = asyncio.create_task(server.serve(sockets=[server_socket]))
    process: asyncio.subprocess.Process | None = None
    try:
        await _wait_until(lambda: server.started)
        _write_smoke_config(tmp_path, token)
        environment = os.environ.copy()
        environment.pop("MILOCO_CONFIG_SEARCH_PATH", None)
        environment.pop("MILOCO_SERVER__TOKEN", None)
        environment["MILOCO_HOME"] = str(tmp_path)
        environment["MILOCO_RTSP_VIEW_SMOKE_DURATION_SEC"] = "30"
        smoke_root = _smoke_script_root()
        process = await asyncio.create_subprocess_exec(
            str(smoke_root / "scripts" / "rtsp-view-smoke.sh"),
            camera_id,
            f"http://127.0.0.1:{port}",
            cwd=smoke_root,
            env=environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await _wait_until(lambda: websocket_active)
        process.send_signal(signal.SIGTERM)
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            process.communicate(), timeout=3
        )
        await _wait_until(lambda: not websocket_active)
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=2)
        server_socket.close()

    stdout = stdout_bytes.decode()
    stderr = stderr_bytes.decode()
    assert process is not None
    assert process.returncode == 143
    assert stdout == ""
    assert "first_frame_latency_ms=" not in stdout
    assert "output_fps=" not in stdout
    assert token not in stdout + stderr
