"""Credential-safe schemas for the generic camera API."""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CameraSummary(BaseModel):
    """Unified public representation of a MIoT or RTSP camera."""

    id: str
    source_type: Literal["miot", "rtsp"]
    name: str
    room_name: str
    enabled: bool
    connected: bool
    video_codec: str | None
    audio_codec: str | None
    last_frame_unix_ms: int | None = None
    has_password: bool = False
    error_code: str | None = None
    error_message: str | None = None


class RtspSourceUpsert(BaseModel):
    """Create/edit payload; transport credentials never appear in repr/errors."""

    model_config = ConfigDict(hide_input_in_errors=True, extra="ignore")

    name: str
    room_name: str = ""
    uri: str = Field(repr=False)
    username: str = Field(default="", repr=False)
    password: str = Field(default="", repr=False)
    transport: Literal["tcp", "udp"] = "tcp"
    audio_enabled: bool = True

    @field_validator("uri")
    @classmethod
    def _validate_uri(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError("RTSP URI is invalid")
        try:
            parts = urlsplit(value)
        except ValueError as exc:
            raise ValueError("RTSP URI is invalid") from exc
        if (
            parts.scheme not in {"rtsp", "rtsps"}
            or parts.hostname is None
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
        ):
            raise ValueError("RTSP URI is invalid")
        try:
            port = parts.port
        except ValueError as exc:
            raise ValueError("RTSP URI is invalid") from exc
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("RTSP URI is invalid")
        return value
