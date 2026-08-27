from __future__ import annotations

import logging
import warnings
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from starlette.exceptions import StarletteDeprecationWarning

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="Using `httpx` with `starlette.testclient` is deprecated.*",
        category=StarletteDeprecationWarning,
    )
    from fastapi.testclient import TestClient
from miloco.camera.router import _get_camera_service, router
from miloco.camera.schema import CameraSummary
from miloco.camera.service import CameraConflictError, CameraNotFoundError
from miloco.middleware.exception_handler import handle_exception
from miloco.perception.collect.rtsp_probe import (
    RtspProbeResult,
    RtspSourceError,
)

SOURCE_ID = "rtsp:00000000-0000-0000-0000-000000000001"
VALID_BODY = {
    "name": "Living Room",
    "room_name": "Living Room",
    "uri": "rtsp://camera.local/live",
    "username": "camera-user",
    "password": "stored-secret",
    "transport": "tcp",
    "audio_enabled": True,
}


def _summary(*, enabled: bool = False) -> CameraSummary:
    return CameraSummary(
        id=SOURCE_ID,
        source_type="rtsp",
        name="Living Room",
        room_name="Living Room",
        enabled=enabled,
        connected=False,
        video_codec=None,
        audio_codec=None,
        has_password=True,
    )


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.error: BaseException | None = None

    def _raise(self) -> None:
        if self.error is not None:
            raise self.error

    async def list_cameras(self):
        self.calls.append(("list",))
        self._raise()
        return [_summary()]

    async def test_rtsp(self, body):
        self.calls.append(("test", body))
        self._raise()
        return RtspProbeResult("h264", 1920, 1080, 25.0, "1/90000", "aac", 48000)

    async def create_rtsp(self, body):
        self.calls.append(("create", body))
        self._raise()
        return _summary()

    async def edit_rtsp(self, camera_id, body):
        self.calls.append(("edit", camera_id, body))
        self._raise()
        return _summary()

    async def enable(self, camera_id):
        self.calls.append(("enable", camera_id))
        self._raise()
        return _summary(enabled=True)

    async def disable(self, camera_id):
        self.calls.append(("disable", camera_id))
        self._raise()
        return _summary()

    async def delete(self, camera_id):
        self.calls.append(("delete", camera_id))
        self._raise()


@pytest.fixture
def service() -> _Service:
    return _Service()


@pytest.fixture
def client(service: _Service, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        "miloco.middleware.auth_middleware.get_settings",
        lambda: SimpleNamespace(server=SimpleNamespace(token="service-token")),
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
    with TestClient(app) as test_client:
        yield test_client


def _auth() -> dict[str, str]:
    return {"Authorization": "Bearer service-token"}


@pytest.mark.parametrize(
    ("method", "path", "json_body", "call_name"),
    [
        ("get", "/api/cameras", None, "list"),
        ("post", "/api/cameras/rtsp/test", VALID_BODY, "test"),
        ("post", "/api/cameras/rtsp", VALID_BODY, "create"),
        ("put", f"/api/cameras/rtsp/{SOURCE_ID}", VALID_BODY, "edit"),
        ("post", f"/api/cameras/{SOURCE_ID}/enable", None, "enable"),
        ("post", f"/api/cameras/{SOURCE_ID}/disable", None, "disable"),
        ("delete", f"/api/cameras/{SOURCE_ID}", None, "delete"),
    ],
)
def test_all_management_endpoints_are_authenticated_and_routed(
    client: TestClient,
    service: _Service,
    method: str,
    path: str,
    json_body: dict | None,
    call_name: str,
) -> None:
    unauthorized = client.request(method, path, json=json_body)
    assert unauthorized.status_code == 401

    response = client.request(method, path, headers=_auth(), json=json_body)

    assert response.status_code == 200
    assert response.json()["code"] == 0
    assert service.calls[-1][0] == call_name
    if call_name == "list":
        assert response.json()["data"][0]["last_frame_unix_ms"] is None
    serialized = response.text
    assert "stored-secret" not in serialized
    assert "camera-user" not in serialized


def test_not_found_response_has_stable_safe_code(
    client: TestClient, service: _Service
) -> None:
    service.error = CameraNotFoundError()

    response = client.post(f"/api/cameras/{SOURCE_ID}/disable", headers=_auth())

    assert response.status_code == 404
    assert response.json() == {
        "detail": {"code": "camera_not_found", "message": "Camera was not found"}
    }


def test_conflict_response_has_stable_safe_code(
    client: TestClient, service: _Service
) -> None:
    service.error = CameraConflictError(
        "hot_apply_failed", "Camera update could not be applied"
    )

    response = client.post(f"/api/cameras/{SOURCE_ID}/enable", headers=_auth())

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "hot_apply_failed",
            "message": "Camera update could not be applied",
        }
    }


def test_probe_error_response_uses_safe_error_only(
    client: TestClient, service: _Service, caplog: pytest.LogCaptureFixture
) -> None:
    service.error = RtspSourceError(
        "authentication_failed", "RTSP authentication failed", recoverable=False
    )
    caplog.set_level(logging.WARNING)

    response = client.post("/api/cameras/rtsp/test", headers=_auth(), json=VALID_BODY)

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "authentication_failed",
            "message": "RTSP authentication failed",
        }
    }
    combined = response.text + caplog.text
    assert "stored-secret" not in combined
    assert "camera-user" not in combined
    assert "rtsp://camera.local/live" not in combined


def test_validation_failure_never_echoes_body_or_uri(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "validation-secret"
    credential_uri = f"rtsp://private-user:{secret}@camera.local/live"
    caplog.set_level(logging.WARNING)

    response = client.post(
        "/api/cameras/rtsp",
        headers=_auth(),
        json={**VALID_BODY, "uri": credential_uri, "password": secret},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "invalid_camera_request",
            "message": "RTSP camera request is invalid",
        }
    }
    combined = response.text + caplog.text
    assert secret not in combined
    assert "private-user" not in combined
    assert credential_uri not in combined


def test_client_supplied_id_is_ignored(client: TestClient, service: _Service) -> None:
    response = client.post(
        "/api/cameras/rtsp",
        headers=_auth(),
        json={**VALID_BODY, "id": "rtsp:client-controlled", "enabled": True},
    )

    assert response.status_code == 200
    submitted = service.calls[-1][1]
    assert not hasattr(submitted, "id")
    assert not hasattr(submitted, "enabled")


def test_persistence_failure_response_and_logs_are_redacted(
    client: TestClient, service: _Service, caplog: pytest.LogCaptureFixture
) -> None:
    secret = "router-persistence-secret"
    service.error = CameraConflictError(
        "persistence_failed", "Camera configuration could not be saved"
    )
    caplog.set_level(logging.WARNING)

    response = client.post(
        f"/api/cameras/{SOURCE_ID}/disable",
        headers=_auth(),
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "persistence_failed",
            "message": "Camera configuration could not be saved",
        }
    }
    combined = response.text + caplog.text
    assert secret not in combined
    assert "synthetic-user" not in combined
    assert "rtsp://" not in combined


def test_list_persistence_failure_is_a_stable_conflict(
    client: TestClient, service: _Service
) -> None:
    service.error = CameraConflictError(
        "persistence_failed", "Camera configuration could not be loaded"
    )

    response = client.get("/api/cameras", headers=_auth())

    assert response.status_code == 409
    assert response.json() == {
        "detail": {
            "code": "persistence_failed",
            "message": "Camera configuration could not be loaded",
        }
    }
