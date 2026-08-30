"""Shared authentication defaults for legacy endpoint tests.

The production API now requires a dashboard session or service Bearer token.
Most pre-auth route tests exercise endpoint behavior rather than authorization,
so their TestClient instances receive a synthetic service credential by default.
Auth, SSE-auth, and camera boundary modules remain anonymous by default because
they explicitly verify rejection behavior.
"""

from __future__ import annotations

import os
from collections.abc import Generator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from miloco.config import get_settings


_DEFAULT_AUTH_ENV = "MILOCO_TEST_DEFAULT_SERVICE_AUTH"
_AUTH_BOUNDARY_TESTS = ("/tests/auth/", "/tests/camera/", "/test_sse_auth.py")
_original_test_client_init = TestClient.__init__


def _authenticated_test_client_init(self: TestClient, *args: Any, **kwargs: Any) -> None:
    if os.environ.get(_DEFAULT_AUTH_ENV) == "1":
        token = get_settings().server.token
        if token:
            headers = dict(kwargs.get("headers") or {})
            headers.setdefault("Authorization", f"Bearer {token}")
            kwargs["headers"] = headers
    _original_test_client_init(self, *args, **kwargs)


TestClient.__init__ = _authenticated_test_client_init


@pytest.fixture(autouse=True)
def _authenticate_legacy_endpoint_clients(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> Generator[None]:
    """Authenticate legacy fixtures without weakening explicit auth tests."""
    path = str(request.fspath)
    if not any(marker in path for marker in _AUTH_BOUNDARY_TESTS):
        monkeypatch.setenv(_DEFAULT_AUTH_ENV, "1")
    yield
