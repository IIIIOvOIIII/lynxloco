from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from miloco.auth.schema import (
    LoginRequest,
    PasswordChangeRequest,
    SetupRequest,
    UserCreateRequest,
    UserUpdateRequest,
)
from miloco.auth.service import AuthService
from miloco.middleware import verify_token
from miloco.schema.common_schema import NormalResponse

router = APIRouter(tags=["Dashboard Auth"])


def get_auth_service() -> AuthService:
    return AuthService()


@router.get("/auth/status", response_model=NormalResponse)
def auth_status(
    request: Request, service: AuthService = Depends(get_auth_service)
) -> NormalResponse:
    return NormalResponse(
        code=0, message="ok", data=service.status(request).model_dump()
    )


@router.post("/auth/setup", response_model=NormalResponse)
def auth_setup(
    body: SetupRequest,
    request: Request,
    response: Response,
    service: AuthService = Depends(get_auth_service),
) -> NormalResponse:
    data = service.setup_first_admin(body, request, response)
    return NormalResponse(code=0, message="ok", data=data.model_dump())


@router.post("/auth/login", response_model=NormalResponse)
def auth_login(
    body: LoginRequest,
    request: Request,
    response: Response,
    service: AuthService = Depends(get_auth_service),
) -> NormalResponse:
    data = service.login(body, request, response)
    return NormalResponse(code=0, message="ok", data=data.model_dump())


@router.post("/auth/logout", response_model=NormalResponse)
def auth_logout(
    request: Request,
    response: Response,
    service: AuthService = Depends(get_auth_service),
) -> NormalResponse:
    service.logout(request, response)
    return NormalResponse(code=0, message="ok")


@router.get("/auth/me", response_model=NormalResponse)
def auth_me(
    request: Request, service: AuthService = Depends(get_auth_service)
) -> NormalResponse:
    data = service.status(request)
    if not data.authenticated:
        verify_token(request)
    return NormalResponse(code=0, message="ok", data=data.model_dump())


@router.get("/users", response_model=NormalResponse)
def list_users(
    service: AuthService = Depends(get_auth_service),
    _: None = Depends(verify_token),
) -> NormalResponse:
    return NormalResponse(
        code=0,
        message="ok",
        data={"users": [user.model_dump() for user in service.list_users()]},
    )


@router.post("/users", response_model=NormalResponse)
def create_user(
    body: UserCreateRequest,
    service: AuthService = Depends(get_auth_service),
    _: None = Depends(verify_token),
) -> NormalResponse:
    return NormalResponse(
        code=0, message="ok", data=service.create_user(body).model_dump()
    )


@router.patch("/users/{user_id}", response_model=NormalResponse)
def update_user(
    user_id: str,
    body: UserUpdateRequest,
    service: AuthService = Depends(get_auth_service),
    _: None = Depends(verify_token),
) -> NormalResponse:
    return NormalResponse(
        code=0,
        message="ok",
        data=service.update_user(user_id, body, None).model_dump(),
    )


@router.post("/users/{user_id}/password", response_model=NormalResponse)
def change_password(
    user_id: str,
    body: PasswordChangeRequest,
    service: AuthService = Depends(get_auth_service),
    _: None = Depends(verify_token),
) -> NormalResponse:
    return NormalResponse(
        code=0, message="ok", data=service.change_password(user_id, body).model_dump()
    )


@router.delete("/users/{user_id}", response_model=NormalResponse)
def delete_user(
    user_id: str,
    service: AuthService = Depends(get_auth_service),
    _: None = Depends(verify_token),
) -> NormalResponse:
    service.delete_user(user_id, None)
    return NormalResponse(code=0, message="ok")
