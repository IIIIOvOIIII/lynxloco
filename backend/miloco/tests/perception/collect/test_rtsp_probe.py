from __future__ import annotations

import asyncio
import importlib
import logging
import socket
import time
from collections.abc import Callable
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from urllib.parse import quote

import av
import pytest
from miloco.config.settings import RtspSourceSettings

_FIXTURE_DIR = Path(__file__).parents[2] / "fixtures" / "rtsp"
_H264_AUDIO = _FIXTURE_DIR / "h264_video_audio.mkv"
_H265_VIDEO = _FIXTURE_DIR / "h265_video_only.mkv"


def _rtsp_probe() -> ModuleType:
    """Load the wished-for module while keeping the initial RED a test failure."""
    try:
        return importlib.import_module("miloco.perception.collect.rtsp_probe")
    except ModuleNotFoundError:
        pytest.fail("RTSP probe implementation is missing", pytrace=False)


def _source(**overrides: object) -> RtspSourceSettings:
    values: dict[str, object] = {
        "id": "rtsp:00000000-0000-0000-0000-000000000001",
        "name": "test-camera",
        "uri": "rtsp://camera.example:8554/live/main?quality=high",
        "username": "",
        "password": "",
        "transport": "tcp",
        "audio_enabled": True,
        "enabled": False,
    }
    values.update(overrides)
    return RtspSourceSettings.model_validate(values)


def _fixture_opener(path: Path) -> Callable[..., av.container.InputContainer]:
    def _open(_url: str, **_kwargs: object) -> av.container.InputContainer:
        return av.open(path)

    return _open


class _FakeContainer:
    def __init__(
        self,
        *,
        video: list[object] | None = None,
        audio: list[object] | None = None,
        frames: list[object] | None = None,
    ) -> None:
        self.streams = SimpleNamespace(video=video or [], audio=audio or [])
        self._frames = frames or []
        self.closed = False
        self.decode_calls = 0

    def decode(self, _stream: object) -> list[object]:
        self.decode_calls += 1
        return self._frames

    def close(self) -> None:
        self.closed = True


class _FailingCloseContainer(_FakeContainer):
    def close(self) -> None:
        self.closed = True
        raise RuntimeError("close exposed p@ss:word and rtsp://private.example")


def _video_stream(codec: str = "h264") -> object:
    return SimpleNamespace(
        codec_context=SimpleNamespace(name=codec, width=64, height=48),
        average_rate=10,
        time_base=SimpleNamespace(numerator=1, denominator=1000),
    )


@pytest.mark.asyncio
async def test_probe_reads_h264_video_and_optional_audio_from_real_fixture() -> None:
    probe = _rtsp_probe()

    result = await probe.probe_rtsp_source(
        _source(), open_input=_fixture_opener(_H264_AUDIO)
    )

    assert result == probe.RtspProbeResult(
        video_codec="h264",
        width=64,
        height=48,
        fps=10.0,
        time_base="1/1000",
        audio_codec="aac",
        audio_sample_rate=8000,
    )


@pytest.mark.asyncio
async def test_probe_reads_hevc_video_only_from_real_fixture() -> None:
    probe = _rtsp_probe()

    result = await probe.probe_rtsp_source(
        _source(), open_input=_fixture_opener(_H265_VIDEO)
    )

    assert result == probe.RtspProbeResult(
        video_codec="hevc",
        width=64,
        height=48,
        fps=10.0,
        time_base="1/1000",
        audio_codec=None,
        audio_sample_rate=None,
    )


@pytest.mark.asyncio
async def test_probe_rejects_input_without_video_stream_and_closes_it() -> None:
    probe = _rtsp_probe()
    container = _FakeContainer()

    with pytest.raises(probe.RtspSourceError) as raised:
        await probe.probe_rtsp_source(
            _source(), open_input=lambda *_a, **_kw: container
        )

    assert raised.value.code == "no_video_stream"
    assert raised.value.recoverable is False
    assert container.closed is True


@pytest.mark.asyncio
async def test_probe_decodes_at_least_one_video_frame() -> None:
    probe = _rtsp_probe()
    container = _FakeContainer(video=[_video_stream()])

    with pytest.raises(probe.RtspSourceError) as raised:
        await probe.probe_rtsp_source(
            _source(), open_input=lambda *_a, **_kw: container
        )

    assert raised.value.code == "no_video_stream"
    assert raised.value.recoverable is False
    assert container.decode_calls == 1
    assert container.closed is True


@pytest.mark.asyncio
async def test_probe_classifies_authentication_failure_without_raw_detail() -> None:
    probe = _rtsp_probe()

    def _denied(*_args: object, **_kwargs: object) -> Any:
        raise av.error.HTTPUnauthorizedError(1, "server included private detail")

    with pytest.raises(probe.RtspSourceError) as raised:
        await probe.probe_rtsp_source(_source(), open_input=_denied)

    assert raised.value.code == "authentication_failed"
    assert raised.value.safe_message == "RTSP authentication failed"
    assert raised.value.recoverable is False
    assert "private detail" not in str(raised.value)
    assert "private detail" not in repr(raised.value)


@pytest.mark.asyncio
async def test_probe_classifies_missing_resource_as_terminal() -> None:
    probe = _rtsp_probe()

    def _missing(*_args: object, **_kwargs: object) -> Any:
        raise av.error.HTTPNotFoundError(1, "private path")

    with pytest.raises(probe.RtspSourceError) as raised:
        await probe.probe_rtsp_source(_source(), open_input=_missing)

    assert raised.value.code == "resource_not_found"
    assert raised.value.safe_message == "RTSP resource was not found"
    assert raised.value.recoverable is False
    assert "private path" not in repr(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (socket.gaierror(-2, "DNS included private host"), "dns_failed"),
        (av.error.TimeoutError(1, "timeout included private URL"), "timeout"),
        (av.error.EOFError(1, "EOF included private URL"), "end_of_stream"),
        (ConnectionResetError("reset included private URL"), "connection_reset"),
    ],
)
async def test_probe_classifies_transient_failures_as_recoverable(
    failure: BaseException, expected_code: str
) -> None:
    probe = _rtsp_probe()

    def _fail(*_args: object, **_kwargs: object) -> Any:
        raise failure

    with pytest.raises(probe.RtspSourceError) as raised:
        await probe.probe_rtsp_source(_source(), open_input=_fail)

    assert raised.value.code == expected_code
    assert raised.value.recoverable is True
    assert "private" not in str(raised.value)
    assert "private" not in repr(raised.value)


@pytest.mark.asyncio
async def test_probe_enforces_one_total_timeout_around_open_and_decode() -> None:
    probe = _rtsp_probe()

    def _slow_open(*_args: object, **_kwargs: object) -> Any:
        time.sleep(0.2)
        return _FakeContainer()

    with pytest.raises(probe.RtspSourceError) as raised:
        await asyncio.wait_for(
            probe.probe_rtsp_source(_source(), timeout_sec=0.01, open_input=_slow_open),
            timeout=0.5,
        )

    assert raised.value.code == "timeout"
    assert raised.value.recoverable is True


@pytest.mark.asyncio
async def test_probe_rejects_unsupported_video_codec_before_returning_metadata() -> (
    None
):
    probe = _rtsp_probe()
    container = _FakeContainer(video=[_video_stream("vp9")], frames=[object()])

    with pytest.raises(probe.RtspSourceError) as raised:
        await probe.probe_rtsp_source(
            _source(), open_input=lambda *_a, **_kw: container
        )

    assert raised.value.code == "unsupported_video_codec"
    assert raised.value.recoverable is False
    assert container.closed is True


@pytest.mark.asyncio
async def test_probe_preserves_safe_primary_error_when_container_close_also_fails() -> (
    None
):
    probe = _rtsp_probe()
    container = _FailingCloseContainer(video=[_video_stream("vp9")], frames=[object()])

    with pytest.raises(probe.RtspSourceError) as raised:
        await probe.probe_rtsp_source(
            _source(), open_input=lambda *_a, **_kw: container
        )

    assert raised.value.code == "unsupported_video_codec"
    assert "p@ss:word" not in str(raised.value)
    assert "private.example" not in repr(raised.value)
    assert container.closed is True


@pytest.mark.asyncio
async def test_probe_rejects_invalid_uri_even_if_settings_validation_is_bypassed() -> (
    None
):
    probe = _rtsp_probe()
    invalid = RtspSourceSettings.model_construct(
        id="rtsp:00000000-0000-0000-0000-000000000001",
        name="invalid",
        uri="http://camera.example/live",
    )

    with pytest.raises(probe.RtspSourceError) as raised:
        await probe.probe_rtsp_source(invalid, open_input=lambda *_a, **_kw: None)

    assert raised.value.code == "invalid_uri"
    assert raised.value.recoverable is False


@pytest.mark.asyncio
async def test_probe_quotes_ephemeral_credentials_and_never_leaks_them(
    caplog: pytest.LogCaptureFixture,
) -> None:
    probe = _rtsp_probe()
    username = "user@example.com"
    password = "p@ss:word?token=x&next=#frag"
    captured: dict[str, object] = {}

    def _denied(url: str, **kwargs: object) -> Any:
        captured["url"] = url
        captured["kwargs"] = kwargs
        raise av.error.HTTPForbiddenError(
            1, f"denied {url}; user={username}; password={password}"
        )

    with caplog.at_level(logging.DEBUG, logger="miloco.perception.collect.rtsp_probe"):
        with pytest.raises(probe.RtspSourceError) as raised:
            await probe.probe_rtsp_source(
                _source(username=username, password=password, transport="udp"),
                timeout_sec=2.5,
                open_input=_denied,
            )

    expected_userinfo = f"{quote(username, safe='')}:{quote(password, safe='')}"
    assert captured["url"] == (
        f"rtsp://{expected_userinfo}@camera.example:8554/live/main?quality=high"
    )
    assert captured["kwargs"] == {
        "options": {"rtsp_transport": "udp", "stimeout": "2500000"},
        "timeout": (2.5, 2.5),
        "metadata_errors": "ignore",
    }

    leaked_values = (username, password, expected_userinfo, str(captured["url"]))
    surfaces = (str(raised.value), repr(raised.value), caplog.text)
    for leaked in leaked_values:
        assert all(leaked not in surface for surface in surfaces)
    assert raised.value.code == "authentication_failed"
    assert raised.value.recoverable is False
