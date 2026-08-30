from __future__ import annotations

import hashlib
import secrets

from fastapi import Request, Response, status

from miloco.auth.passwords import PasswordPolicyError, hash_password, verify_password
from miloco.auth.repo import DashboardAuthRepo
from miloco.auth.schema import (
    AuthStatusData,
    DashboardSessionRecord,
    DashboardUserPublic,
    LoginRequest,
    PasswordChangeRequest,
    SetupRequest,
    UserCreateRequest,
    UserUpdateRequest,
)
from miloco.middleware.exceptions import (
    AuthenticationException,
    BadRequestException,
    HTTPException,
    ResourceNotFoundException,
)
from miloco.utils.time_utils import now_ms

SESSION_COOKIE_NAME = "miloco_dashboard_session"
CSRF_HEADER_NAME = "X-Miloco-CSRF"
SESSION_TTL_MS = 7 * 24 * 60 * 60 * 1000
_DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$LHdr2q4abf/Wzh2Y0gd/qQ$1rPUk/6L/nUe/j0J6BVejxbsKRSFJxnjnFEwkYe4OxI"
)


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _client_ip_hint(request: Request) -> str | None:
    if request.client is None:
        return None
    return request.client.host[:64]


def _user_agent_hash(request: Request) -> str:
    return hash_secret(request.headers.get("user-agent", "")[:512])


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_TTL_MS // 1000,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        samesite="lax",
        secure=False,
        path="/",
    )


def _conflict(message: str) -> None:
    raise HTTPException(message, status.HTTP_409_CONFLICT, code=2002)


class AuthService:
    def __init__(self, repo: DashboardAuthRepo | None = None) -> None:
        self.repo = repo or DashboardAuthRepo()

    def _issue_session(
        self,
        user: DashboardUserPublic,
        request: Request,
        response: Response,
    ) -> str:
        session_token = secrets.token_urlsafe(48)
        csrf_token = secrets.token_urlsafe(32)
        timestamp = now_ms()
        self.repo.create_session(
            user_id=user.id,
            session_hash=hash_secret(session_token),
            csrf_hash=hash_secret(csrf_token),
            expires_at=timestamp + SESSION_TTL_MS,
            user_agent_hash=_user_agent_hash(request),
            client_ip_hint=_client_ip_hint(request),
        )
        self.repo.touch_user_login(user.id, timestamp)
        _set_session_cookie(response, session_token)
        return csrf_token

    def authenticate_session_token_hash(
        self, session_hash: str
    ) -> tuple[DashboardUserPublic, DashboardSessionRecord] | None:
        timestamp = now_ms()
        session = self.repo.get_session_by_hash(session_hash, timestamp)
        if session is None:
            return None
        user = self.repo.get_user_by_id(session.user_id)
        if user is None or not user.enabled:
            return None
        self.repo.touch_session(session.id, timestamp)
        return user.to_public(), session

    def authenticate_request_session(
        self, request: Request
    ) -> tuple[DashboardUserPublic, DashboardSessionRecord] | None:
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if not token:
            return None
        return self.authenticate_session_token_hash(hash_secret(token))

    def rotate_csrf(self, request: Request) -> tuple[DashboardUserPublic, str] | None:
        authenticated = self.authenticate_request_session(request)
        if authenticated is None:
            return None
        user, session = authenticated
        csrf_token = secrets.token_urlsafe(32)
        self.repo.update_session_csrf(session.id, hash_secret(csrf_token), now_ms())
        return user, csrf_token

    def status(self, request: Request) -> AuthStatusData:
        rotated = self.rotate_csrf(request)
        return AuthStatusData(
            needs_setup=not self.repo.any_enabled_user(),
            authenticated=rotated is not None,
            user=rotated[0] if rotated else None,
            csrf_token=rotated[1] if rotated else None,
        )

    def setup_first_admin(
        self,
        body: SetupRequest,
        request: Request,
        response: Response,
    ) -> AuthStatusData:
        if body.password != body.password_confirm:
            raise BadRequestException("Passwords do not match")
        try:
            password_hash = hash_password(body.password)
        except PasswordPolicyError as exc:
            raise BadRequestException(str(exc)) from exc
        try:
            user = self.repo.create_first_admin(
                username=body.username,
                display_name=body.display_name,
                password_hash=password_hash,
            )
        except ValueError as exc:
            if str(exc) == "setup_completed":
                _conflict("Dashboard setup already completed")
            raise BadRequestException(str(exc)) from exc
        public_user = user.to_public()
        csrf_token = self._issue_session(public_user, request, response)
        return AuthStatusData(
            needs_setup=False,
            authenticated=True,
            user=public_user,
            csrf_token=csrf_token,
        )

    def login(
        self,
        body: LoginRequest,
        request: Request,
        response: Response,
    ) -> AuthStatusData:
        user = self.repo.get_user_by_username(body.username)
        password_hash = (
            user.password_hash if user is not None and user.enabled else _DUMMY_PASSWORD_HASH
        )
        password_valid = verify_password(body.password, password_hash)
        if user is None or not user.enabled or not password_valid:
            raise AuthenticationException("Invalid username or password")
        public_user = user.to_public()
        csrf_token = self._issue_session(public_user, request, response)
        return AuthStatusData(
            needs_setup=False,
            authenticated=True,
            user=public_user,
            csrf_token=csrf_token,
        )

    def logout(self, request: Request, response: Response) -> None:
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if token:
            session = self.repo.get_session_by_hash(hash_secret(token), now_ms())
            if session is not None:
                self.repo.delete_session(session.id)
        _clear_session_cookie(response)

    def list_users(self) -> list[DashboardUserPublic]:
        return [user.to_public() for user in self.repo.list_users()]

    def create_user(self, body: UserCreateRequest) -> DashboardUserPublic:
        if body.password != body.password_confirm:
            raise BadRequestException("Passwords do not match")
        try:
            user = self.repo.create_user(
                username=body.username,
                display_name=body.display_name,
                password_hash=hash_password(body.password),
            )
        except PasswordPolicyError as exc:
            raise BadRequestException(str(exc)) from exc
        except ValueError as exc:
            if str(exc) == "username_exists":
                _conflict("Username already exists")
            raise BadRequestException(str(exc)) from exc
        return user.to_public()

    def update_user(
        self,
        user_id: str,
        body: UserUpdateRequest,
        current_session_user_id: str | None,
    ) -> DashboardUserPublic:
        try:
            if body.enabled is False:
                user = self.repo.update_user_guarded(
                    user_id,
                    username=body.username,
                    display_name=body.display_name,
                    enabled=False,
                )
            else:
                user = self.repo.update_user(
                    user_id,
                    username=body.username,
                    display_name=body.display_name,
                    enabled=body.enabled,
                )
        except ValueError as exc:
            if str(exc) == "username_exists":
                _conflict("Username already exists")
            if str(exc) == "last_admin":
                _conflict("Last administrator cannot be disabled")
            if str(exc) == "user_not_found":
                raise ResourceNotFoundException("Dashboard user not found") from exc
            raise BadRequestException(str(exc)) from exc
        return user.to_public()

    def change_password(
        self, user_id: str, body: PasswordChangeRequest
    ) -> DashboardUserPublic:
        if body.password != body.password_confirm:
            raise BadRequestException("Passwords do not match")
        try:
            user = self.repo.update_password_and_revoke_sessions(
                user_id, hash_password(body.password)
            )
        except PasswordPolicyError as exc:
            raise BadRequestException(str(exc)) from exc
        except ValueError as exc:
            if str(exc) == "user_not_found":
                raise ResourceNotFoundException("Dashboard user not found") from exc
            raise BadRequestException(str(exc)) from exc
        return user.to_public()

    def delete_user(self, user_id: str, current_session_user_id: str | None) -> None:
        if current_session_user_id == user_id:
            _conflict("Current user cannot be deleted")
        try:
            self.repo.delete_user_guarded(user_id)
        except ValueError as exc:
            if str(exc) == "last_admin":
                _conflict("Last administrator cannot be deleted")
            if str(exc) == "user_not_found":
                raise ResourceNotFoundException("Dashboard user not found") from exc
            raise BadRequestException(str(exc)) from exc
