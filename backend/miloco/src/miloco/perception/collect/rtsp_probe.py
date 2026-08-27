"""Credential-safe RTSP source preflight."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast
from urllib.parse import quote, urlsplit, urlunsplit

import av

from miloco.config.settings import RtspSourceSettings


@dataclass(frozen=True)
class RtspProbeResult:
    video_codec: Literal["h264", "hevc"]
    width: int
    height: int
    fps: float
    time_base: str
    audio_codec: str | None
    audio_sample_rate: int | None


class RtspSourceError(RuntimeError):
    """Stable, credential-free RTSP failure exposed to callers."""

    def __init__(self, code: str, safe_message: str, *, recoverable: bool) -> None:
        self.code = code
        self.safe_message = safe_message
        self.recoverable = recoverable
        super().__init__(safe_message)


def _error(code: str, safe_message: str, *, recoverable: bool) -> RtspSourceError:
    return RtspSourceError(code, safe_message, recoverable=recoverable)


def _validate_uri(uri: str) -> None:
    if any(ord(character) < 32 or ord(character) == 127 for character in uri):
        raise _error("invalid_uri", "RTSP URI is invalid", recoverable=False)

    try:
        parts = urlsplit(uri)
        host = parts.hostname
    except ValueError:
        raise _error("invalid_uri", "RTSP URI is invalid", recoverable=False) from None

    if (
        parts.scheme not in {"rtsp", "rtsps"}
        or host is None
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
    ):
        raise _error("invalid_uri", "RTSP URI is invalid", recoverable=False)


def _authenticated_url(source: RtspSourceSettings) -> str:
    if not source.username and not source.password:
        return source.uri

    parts = urlsplit(source.uri)
    username = quote(source.username, safe="")
    password = quote(source.password, safe="")
    userinfo = f"{username}:{password}"
    return urlunsplit(
        (parts.scheme, f"{userinfo}@{parts.netloc}", parts.path, parts.query, "")
    )


def _classify_failure(failure: BaseException) -> RtspSourceError:
    if isinstance(
        failure,
        (av.error.HTTPUnauthorizedError, av.error.HTTPForbiddenError, PermissionError),
    ):
        return _error(
            "authentication_failed", "RTSP authentication failed", recoverable=False
        )
    if isinstance(
        failure,
        (av.error.HTTPNotFoundError, av.error.FileNotFoundError, FileNotFoundError),
    ):
        return _error(
            "resource_not_found", "RTSP resource was not found", recoverable=False
        )
    if isinstance(failure, socket.gaierror):
        return _error("dns_failed", "RTSP host lookup failed", recoverable=True)
    if isinstance(failure, (av.error.TimeoutError, TimeoutError)):
        return _error("timeout", "RTSP probe timed out", recoverable=True)
    if isinstance(failure, (av.error.EOFError, EOFError)):
        return _error(
            "end_of_stream", "RTSP stream ended unexpectedly", recoverable=True
        )
    if isinstance(failure, (av.error.ConnectionResetError, ConnectionResetError)):
        return _error("connection_reset", "RTSP connection was reset", recoverable=True)
    if isinstance(
        failure,
        (
            av.error.ConnectionAbortedError,
            av.error.ConnectionRefusedError,
            av.error.BrokenPipeError,
            ConnectionAbortedError,
            ConnectionRefusedError,
            BrokenPipeError,
            OSError,
        ),
    ):
        return _error("connection_failed", "RTSP connection failed", recoverable=True)
    if isinstance(failure, RtspSourceError):
        return failure
    return _error("connection_failed", "RTSP connection failed", recoverable=True)


def _open_container(
    source: RtspSourceSettings,
    timeout_sec: float,
    open_input: Callable[..., av.container.InputContainer],
) -> av.container.InputContainer:
    options = {
        "rtsp_transport": source.transport,
        "stimeout": str(int(timeout_sec * 1_000_000)),
    }
    input_url = _authenticated_url(source)
    caught: Exception | None = None
    container: av.container.InputContainer | None = None
    try:
        container = open_input(
            input_url,
            options=options,
            timeout=(timeout_sec, timeout_sec),
            metadata_errors="ignore",
        )
    except Exception as failure:
        caught = failure
    finally:
        input_url = ""

    if caught is not None:
        classified = _classify_failure(caught)
        caught = None
        raise classified
    assert container is not None
    return container


def _probe_sync(
    source: RtspSourceSettings,
    timeout_sec: float,
    open_input: Callable[..., av.container.InputContainer],
) -> RtspProbeResult:
    container = _open_container(source, timeout_sec, open_input)
    caught: Exception | None = None
    result: RtspProbeResult | None = None
    try:
        video_streams = container.streams.video
        if not video_streams:
            raise _error(
                "no_video_stream", "RTSP source has no video stream", recoverable=False
            )

        video_stream = video_streams[0]
        codec_name = str(video_stream.codec_context.name).lower()
        normalized_codec = "hevc" if codec_name in {"hevc", "h265"} else codec_name
        if normalized_codec not in {"h264", "hevc"}:
            raise _error(
                "unsupported_video_codec",
                "RTSP video codec is not supported",
                recoverable=False,
            )

        frame = next(iter(container.decode(video_stream)), None)
        if frame is None:
            raise _error(
                "no_video_stream",
                "RTSP source has no decodable video frames",
                recoverable=False,
            )

        codec_context = video_stream.codec_context
        width = int(codec_context.width or frame.width)
        height = int(codec_context.height or frame.height)
        average_rate = video_stream.average_rate
        fps = float(average_rate) if average_rate is not None else 0.0
        time_base = str(video_stream.time_base)

        audio_codec: str | None = None
        audio_sample_rate: int | None = None
        if container.streams.audio:
            audio_context = container.streams.audio[0].codec_context
            audio_codec = str(audio_context.name)
            if audio_context.sample_rate is not None:
                audio_sample_rate = int(audio_context.sample_rate)

        result = RtspProbeResult(
            video_codec=cast(Literal["h264", "hevc"], normalized_codec),
            width=width,
            height=height,
            fps=fps,
            time_base=time_base,
            audio_codec=audio_codec,
            audio_sample_rate=audio_sample_rate,
        )
    except Exception as failure:
        caught = failure
    finally:
        try:
            container.close()
        except Exception as close_failure:
            if caught is None:
                caught = close_failure

    if caught is not None:
        classified = _classify_failure(caught)
        caught = None
        raise classified
    assert result is not None
    return result


async def probe_rtsp_source(
    source: RtspSourceSettings,
    *,
    timeout_sec: float = 8.0,
    open_input: Callable[..., av.container.InputContainer] = av.open,
) -> RtspProbeResult:
    """Validate an RTSP source within one caller-visible total timeout."""
    _validate_uri(source.uri)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_probe_sync, source, timeout_sec, open_input),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        raise _error("timeout", "RTSP probe timed out", recoverable=True) from None
