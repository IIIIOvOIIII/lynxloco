from __future__ import annotations

import asyncio
import importlib
import logging
import socket
import threading
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


class _DecodeFailureContainer(_FakeContainer):
    def __init__(self, failure: Exception) -> None:
        super().__init__(video=[_video_stream()])
        self._failure = failure

    def decode(self, _stream: object) -> list[object]:
        self.decode_calls += 1
        raise self._failure


class _SignalingContainer(_FakeContainer):
    def __init__(self, closed: threading.Event) -> None:
        super().__init__()
        self._closed_event = closed

    def close(self) -> None:
        super().close()
        self._closed_event.set()


class _BlockingOpener:
    def __init__(self, *, libav_log: bool = False) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.closed = threading.Event()
        self.calls = 0
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()
        self._libav_log = libav_log

    def __call__(self, url: str, **_kwargs: object) -> _SignalingContainer:
        with self._lock:
            self.calls += 1
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        self.started.set()
        self.release.wait(timeout=1.0)
        if self._libav_log:
            av.logging.log(av.logging.ERROR, "rtsp", f"late worker log {url}")
        with self._lock:
            self.active -= 1
        return _SignalingContainer(self.closed)


class _NoisyContainer(_FakeContainer):
    def __init__(self, sensitive: str) -> None:
        super().__init__(
            video=[_video_stream()],
            frames=[SimpleNamespace(width=64, height=48)],
        )
        self._sensitive = sensitive

    def decode(self, _stream: object) -> list[object]:
        av.logging.log(av.logging.ERROR, "rtsp", f"decode {self._sensitive}")
        return super().decode(_stream)

    def close(self) -> None:
        av.logging.log(av.logging.ERROR, "rtsp", f"close {self._sensitive}")
        super().close()


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
    source = _source(id="rtsp:00000000-0000-0000-0000-000000000005")
    blocking = _BlockingOpener()

    with pytest.raises(probe.RtspSourceError) as raised:
        await asyncio.wait_for(
            probe.probe_rtsp_source(source, timeout_sec=0.01, open_input=blocking),
            timeout=0.5,
        )

    assert raised.value.code == "timeout"
    assert raised.value.recoverable is True
    blocking.release.set()
    assert await asyncio.to_thread(blocking.closed.wait, 0.5)


@pytest.mark.asyncio
async def test_timed_out_probe_keeps_same_source_exclusive_until_worker_closes() -> (
    None
):
    probe = _rtsp_probe()
    source = _source(id="rtsp:00000000-0000-0000-0000-000000000002")
    blocking = _BlockingOpener()

    with pytest.raises(probe.RtspSourceError) as timed_out:
        await probe.probe_rtsp_source(source, timeout_sec=0.01, open_input=blocking)

    assert timed_out.value.code == "timeout"
    assert blocking.started.is_set()

    for _ in range(3):
        with pytest.raises(probe.RtspSourceError) as overlapping:
            await probe.probe_rtsp_source(
                source,
                timeout_sec=0.01,
                open_input=lambda *_a, **_kw: pytest.fail("overlapping worker started"),
            )

        assert overlapping.value.code == "probe_in_progress"
        assert overlapping.value.recoverable is True
    assert blocking.calls == 1
    assert blocking.max_active == 1

    blocking.release.set()
    assert await asyncio.to_thread(blocking.closed.wait, 0.5)
    await asyncio.sleep(0.01)

    result = await probe.probe_rtsp_source(
        source, open_input=_fixture_opener(_H264_AUDIO)
    )
    assert result.video_codec == "h264"


@pytest.mark.asyncio
async def test_cancelled_probe_keeps_same_source_exclusive_until_worker_closes() -> (
    None
):
    probe = _rtsp_probe()
    source = _source(id="rtsp:00000000-0000-0000-0000-000000000003")
    blocking = _BlockingOpener()
    running = asyncio.create_task(
        probe.probe_rtsp_source(source, timeout_sec=1.0, open_input=blocking)
    )
    assert await asyncio.to_thread(blocking.started.wait, 0.5)

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    with pytest.raises(probe.RtspSourceError) as overlapping:
        await probe.probe_rtsp_source(
            source,
            open_input=lambda *_a, **_kw: pytest.fail("overlapping worker started"),
        )

    assert overlapping.value.code == "probe_in_progress"
    assert blocking.calls == 1
    assert blocking.max_active == 1

    blocking.release.set()
    assert await asyncio.to_thread(blocking.closed.wait, 0.5)
    await asyncio.sleep(0.01)

    result = await probe.probe_rtsp_source(
        source, open_input=_fixture_opener(_H265_VIDEO)
    )
    assert result.video_codec == "hevc"


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
@pytest.mark.parametrize(
    "decode_failure",
    [
        av.error.InvalidDataError(1, "decode exposed private URL"),
        av.error.DecoderNotFoundError(1, "decoder exposed private URL"),
    ],
)
async def test_probe_classifies_supported_codec_decode_failures_as_terminal(
    decode_failure: Exception,
) -> None:
    probe = _rtsp_probe()
    container = _DecodeFailureContainer(decode_failure)

    with pytest.raises(probe.RtspSourceError) as raised:
        await probe.probe_rtsp_source(
            _source(), open_input=lambda *_a, **_kw: container
        )

    assert raised.value.code == "unsupported_video_codec"
    assert raised.value.recoverable is False
    assert "private URL" not in str(raised.value)
    assert "private URL" not in repr(raised.value)
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
@pytest.mark.parametrize(
    "uri",
    [
        "rtsp://camera.example:notaport/live",
        "rtsp://camera.example:65536/live",
    ],
)
async def test_probe_rejects_invalid_port_without_opening_input(uri: str) -> None:
    probe = _rtsp_probe()
    invalid = RtspSourceSettings.model_construct(
        id="rtsp:00000000-0000-0000-0000-000000000001",
        name="invalid-port",
        uri=uri,
    )

    with pytest.raises(probe.RtspSourceError) as raised:
        await probe.probe_rtsp_source(
            invalid,
            open_input=lambda *_a, **_kw: pytest.fail("invalid URI was opened"),
        )

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


@pytest.mark.asyncio
async def test_probe_locally_captures_libav_logs_across_open_decode_and_close(
    caplog: pytest.LogCaptureFixture,
) -> None:
    probe = _rtsp_probe()
    username = "user@example.com"
    password = "p@ss:word?token=x&next=#frag"
    expected_userinfo = f"{quote(username, safe='')}:{quote(password, safe='')}"
    sensitive_url = (
        f"rtsp://{expected_userinfo}@camera.example:8554/live/main?quality=high"
    )

    def _noisy_open(url: str, **_kwargs: object) -> _NoisyContainer:
        av.logging.log(av.logging.ERROR, "rtsp", f"open {url}")
        return _NoisyContainer(url)

    previous_level = av.logging.get_level()
    av.logging.set_level(av.logging.DEBUG)
    try:
        with caplog.at_level(logging.DEBUG, logger="libav.rtsp"):
            result = await probe.probe_rtsp_source(
                _source(username=username, password=password),
                open_input=_noisy_open,
            )
    finally:
        av.logging.set_level(previous_level)

    assert result.video_codec == "h264"
    for leaked in (username, password, expected_userinfo, sensitive_url):
        assert leaked not in caplog.text


@pytest.mark.asyncio
async def test_timed_out_worker_keeps_local_libav_log_capture_until_cleanup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    probe = _rtsp_probe()
    username = "user@example.com"
    password = "p@ss:word?token=x&next=#frag"
    source = _source(
        id="rtsp:00000000-0000-0000-0000-000000000004",
        username=username,
        password=password,
    )
    blocking = _BlockingOpener(libav_log=True)
    previous_level = av.logging.get_level()
    av.logging.set_level(av.logging.DEBUG)
    try:
        with caplog.at_level(logging.DEBUG, logger="libav.rtsp"):
            with pytest.raises(probe.RtspSourceError) as raised:
                await probe.probe_rtsp_source(
                    source, timeout_sec=0.01, open_input=blocking
                )
            blocking.release.set()
            assert await asyncio.to_thread(blocking.closed.wait, 0.5)
            await asyncio.sleep(0.01)
    finally:
        av.logging.set_level(previous_level)

    assert raised.value.code == "timeout"
    assert username not in caplog.text
    assert password not in caplog.text
    assert "user%40example.com" not in caplog.text
