from __future__ import annotations

import hmac
from typing import Literal

from fastapi import Request
from fastapi.websockets import WebSocket
from pydantic import BaseModel

from miloco.auth.schema import DashboardUserPublic
from miloco.auth.service import (
    CSRF_HEADER_NAME,
    SESSION_COOKIE_NAME,
    AuthService,
    hash_secret,
)
from miloco.config import get_settings
from miloco.middleware.exceptions import AuthenticationException, AuthorizationException

BEARER_PREFIX = "Bearer "
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
PUBLIC_AUTH_PATHS = {
    "/api/auth/status",
    "/api/auth/setup",
    "/api/auth/login",
    "/api/auth/logout",
}


class AuthContext(BaseModel):
    kind: Literal["service", "dashboard"]
    subject: str
    user: DashboardUserPublic | None = None


def extract_bearer_token(authorization: str | None) -> str | None:
    if authorization and authorization.startswith(BEARER_PREFIX):
        return authorization[len(BEARER_PREFIX) :]
    return None


def valid_service_token(token: str | None) -> bool:
    expected = get_settings().server.token
    if not expected:
        return False
    return token is not None and hmac.compare_digest(token, expected)


def verify_service_token(request: Request) -> AuthContext:
    if valid_service_token(extract_bearer_token(request.headers.get("Authorization"))):
        return AuthContext(kind="service", subject="service")
    raise AuthenticationException("Invalid or missing service token")


def _dashboard_context(request: Request) -> AuthContext | None:
    auth = AuthService().authenticate_request_session(request)
    if auth is None:
        return None
    user, _session = auth
    return AuthContext(kind="dashboard", subject=user.id, user=user)


def verify_dashboard_or_service_auth(request: Request) -> AuthContext:
    token = extract_bearer_token(request.headers.get("Authorization"))
    if valid_service_token(token):
        return AuthContext(kind="service", subject="service")
    dashboard = _dashboard_context(request)
    if dashboard:
        require_csrf_for_cookie_writes(request)
        return dashboard
    raise AuthenticationException("Authentication required")


def verify_dashboard_or_service_query_fallback(request: Request) -> AuthContext:
    token = (
        extract_bearer_token(request.headers.get("Authorization"))
        or request.query_params.get("token")
    )
    if valid_service_token(token):
        return AuthContext(kind="service", subject="service")
    dashboard = _dashboard_context(request)
    if dashboard:
        require_csrf_for_cookie_writes(request)
        return dashboard
    raise AuthenticationException("Authentication required")


def verify_websocket_dashboard_or_service(websocket: WebSocket) -> AuthContext:
    token = (
        extract_bearer_token(websocket.headers.get("Authorization"))
        or websocket.query_params.get("token")
    )
    if valid_service_token(token):
        return AuthContext(kind="service", subject="service")
    cookie_token = websocket.cookies.get(SESSION_COOKIE_NAME)
    if cookie_token:
        auth = AuthService().authenticate_session_token_hash(hash_secret(cookie_token))
        if auth is not None:
            user, _session = auth
            return AuthContext(kind="dashboard", subject=user.id, user=user)
    raise AuthenticationException("Authentication required")


def require_csrf_for_cookie_writes(request: Request) -> None:
    if request.method.upper() not in UNSAFE_METHODS:
        return
    if request.url.path in PUBLIC_AUTH_PATHS:
        return
    if valid_service_token(extract_bearer_token(request.headers.get("Authorization"))):
        return
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie_token:
        return
    authenticated = AuthService().authenticate_session_token_hash(
        hash_secret(cookie_token)
    )
    if authenticated is None:
        return
    _user, session = authenticated
    csrf = request.headers.get(CSRF_HEADER_NAME)
    if not csrf or not hmac.compare_digest(hash_secret(csrf), session.csrf_hash):
        raise AuthorizationException("CSRF token missing or invalid")
