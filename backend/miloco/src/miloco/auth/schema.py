from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

MAX_USERNAME_LENGTH = 128
MAX_DISPLAY_NAME_LENGTH = 256
MAX_PASSWORD_LENGTH = 256


class DashboardUserPublic(BaseModel):
    id: str
    username: str
    display_name: str = ""
    role: Literal["admin"] = "admin"
    enabled: bool
    created_at: int
    updated_at: int
    last_login_at: int | None = None


class DashboardUserRecord(DashboardUserPublic):
    username_norm: str
    password_hash: str

    def to_public(self) -> DashboardUserPublic:
        return DashboardUserPublic(
            id=self.id,
            username=self.username,
            display_name=self.display_name,
            role=self.role,
            enabled=self.enabled,
            created_at=self.created_at,
            updated_at=self.updated_at,
            last_login_at=self.last_login_at,
        )


class DashboardSessionRecord(BaseModel):
    id: str
    user_id: str
    session_hash: str
    csrf_hash: str
    created_at: int
    expires_at: int
    last_seen_at: int
    user_agent_hash: str
    client_ip_hint: str | None = None


class SetupRequest(BaseModel):
    username: str = Field(min_length=1, max_length=MAX_USERNAME_LENGTH)
    display_name: str = Field(default="", max_length=MAX_DISPLAY_NAME_LENGTH)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    password_confirm: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)

    @field_validator("username")
    @classmethod
    def _clean_username(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("username_required")
        return cleaned


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=MAX_USERNAME_LENGTH)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class UserCreateRequest(SetupRequest):
    pass


class UserUpdateRequest(BaseModel):
    username: str | None = Field(default=None, max_length=MAX_USERNAME_LENGTH)
    display_name: str | None = Field(default=None, max_length=MAX_DISPLAY_NAME_LENGTH)
    enabled: bool | None = None


class PasswordChangeRequest(BaseModel):
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    password_confirm: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class AuthStatusData(BaseModel):
    needs_setup: bool
    authenticated: bool
    user: DashboardUserPublic | None = None
    csrf_token: str | None = None


class UserListData(BaseModel):
    users: list[DashboardUserPublic]
