from __future__ import annotations

import logging
from typing import Literal

import numpy as np
import pytest
from miloco.config.settings import RtspSourceSettings
from miloco.perception.collect.camera_source import CameraSourceState
from miloco.perception.collect.rtsp_camera_source import RtspCameraSource


def _source(
    suffix: int,
    *,
    name: str | None = None,
    room_name: str = "living-room",
    uri: str | None = None,
    username: str = "",
    password: str = "",
    transport: Literal["tcp", "udp"] = "tcp",
    audio_enabled: bool = True,
    enabled: bool = True,
) -> RtspSourceSettings:
    return RtspSourceSettings(
        id=f"rtsp:00000000-0000-0000-0000-{suffix:012d}",
        name=name or f"camera-{suffix}",
        room_name=room_name,
        uri=uri or f"rtsp://camera-{suffix}.example/stream",
        username=username,
        password=password,
        transport=transport,
        audio_enabled=audio_enabled,
        enabled=enabled,
    )


class _RecordingSession:
    instances: list[_RecordingSession] = []
    fail_start_hosts: set[str] = set()
    fail_stop_ids: set[str] = set()
    fail_stop_once_ids: set[str] = set()

    def __init__(self, source: RtspSourceSettings) -> None:
        self.source = source
        self.start_count = 0
        self.stop_count = 0
        self.connected = False
        self.active = False
        self.state_override: CameraSourceState | None = None
        self.video_cb = None
        self.audio_cb = None
        self.instances.append(self)

    async def start(self, video_cb, audio_cb) -> None:
        self.start_count += 1
        self.video_cb = video_cb
        self.audio_cb = audio_cb
        if any(host in self.source.uri for host in self.fail_start_hosts):
            raise RuntimeError("secret start failure")
        self.active = True
        self.connected = True

    async def stop(self) -> None:
        self.stop_count += 1
        if self.source.id in self.fail_stop_once_ids and self.stop_count == 1:
            raise RuntimeError("secret transient stop failure")
        if self.source.id in self.fail_stop_ids:
            raise RuntimeError("secret stop failure")
        self.active = False
        self.connected = False

    def state(self) -> CameraSourceState:
        return self.state_override or CameraSourceState(connected=self.connected)

    def is_active(self) -> bool:
        return self.active


@pytest.fixture(autouse=True)
def _reset_fake_session(monkeypatch: pytest.MonkeyPatch) -> None:
    _RecordingSession.instances = []
    _RecordingSession.fail_start_hosts = set()
    _RecordingSession.fail_stop_ids = set()
    _RecordingSession.fail_stop_once_ids = set()
    monkeypatch.setattr(
        "miloco.perception.collect.rtsp_camera_source.RtspSession",
        _RecordingSession,
    )


async def _video_cb(
    did: str,
    frame: np.ndarray,
    stream_ts: int,
    channel: int,
    recv_unix_ms: int,
    decoded_unix_ms: int,
) -> None:
    return None


async def _audio_cb(
    did: str,
    frame: np.ndarray,
    stream_ts: int,
    channel: int,
    recv_unix_ms: int,
    decoded_unix_ms: int,
) -> None:
    return None


@pytest.mark.asyncio
async def test_discovery_exposes_only_enabled_sources_with_stable_metadata() -> None:
    enabled = _source(1, name="front door", room_name="entry")
    disabled = _source(2, enabled=False)
    configured = [enabled, disabled]
    source = RtspCameraSource(lambda: configured)

    discovered = await source.discover_devices()

    assert discovered == {
        enabled.id: discovered[enabled.id],
    }
    assert discovered[enabled.id].did == enabled.id
    assert discovered[enabled.id].name == "front door"
    assert discovered[enabled.id].room_id == "entry"
    assert discovered[enabled.id].room_name == "entry"
    assert discovered[enabled.id].online is True
    assert source.get_session(disabled.id) is None


@pytest.mark.asyncio
async def test_connect_ignores_disabled_source_and_registers_enabled_session() -> None:
    enabled = _source(1)
    disabled = _source(2, enabled=False)
    source = RtspCameraSource(lambda: [enabled, disabled])

    await source.connect_device(disabled.id, _video_cb, _audio_cb)
    await source.connect_device(enabled.id, _video_cb, _audio_cb)

    session = source.get_session(enabled.id)
    assert isinstance(session, _RecordingSession)
    assert session.start_count == 1
    assert source.get_state(enabled.id).connected is True
    assert source.get_session(disabled.id) is None
    assert len(_RecordingSession.instances) == 1


@pytest.mark.asyncio
async def test_get_state_returns_the_session_network_state_without_forcing_online() -> (
    None
):
    enabled = _source(1)
    source = RtspCameraSource(lambda: [enabled])
    await source.connect_device(enabled.id, _video_cb, _audio_cb)
    session = source.get_session(enabled.id)
    assert isinstance(session, _RecordingSession)
    session.state_override = CameraSourceState(
        connected=False,
        video_codec="h265",
        reconnect_attempt=3,
        error_code="authentication_failed",
        error_message="Authentication failed",
    )

    assert source.get_state(enabled.id) == session.state_override
    assert source.retain_pending_connection(enabled.id) is True

    current = [enabled.model_copy(update={"enabled": False})]
    source._settings_loader = lambda: current
    await source.apply_settings()
    assert source.retain_pending_connection(enabled.id) is False


@pytest.mark.asyncio
async def test_terminal_session_is_not_a_retainable_pending_registration() -> None:
    enabled = _source(1)
    source = RtspCameraSource(lambda: [enabled])
    await source.connect_device(enabled.id, _video_cb, _audio_cb)
    session = source.get_session(enabled.id)
    assert isinstance(session, _RecordingSession)
    session.connected = False
    session.active = False

    assert source.get_state(enabled.id).connected is False
    assert source.retain_pending_connection(enabled.id) is False


@pytest.mark.asyncio
async def test_apply_settings_restarts_only_connection_changes() -> None:
    current = [_source(1), _source(2)]
    source = RtspCameraSource(lambda: current)
    await source.connect_device(current[0].id, _video_cb, _audio_cb)
    await source.connect_device(current[1].id, _video_cb, _audio_cb)
    first_session = source.get_session(current[0].id)
    second_session = source.get_session(current[1].id)

    await source.apply_settings()
    assert source.get_session(current[0].id) is first_session
    assert source.get_session(current[1].id) is second_session

    current = [
        current[0].model_copy(update={"name": "renamed", "room_name": "office"}),
        current[1],
    ]
    await source.apply_settings()
    assert source.get_session(current[0].id) is first_session
    assert (await source.discover_devices())[current[0].id].name == "renamed"
    assert (await source.discover_devices())[current[0].id].room_name == "office"

    current = [
        current[0].model_copy(update={"uri": "rtsp://replacement.example/stream"}),
        current[1],
    ]
    await source.apply_settings()
    replacement = source.get_session(current[0].id)
    assert replacement is not first_session
    assert isinstance(first_session, _RecordingSession)
    assert first_session.stop_count == 1
    assert isinstance(replacement, _RecordingSession)
    assert replacement.start_count == 1
    assert source.get_session(current[1].id) is second_session


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("changed_field", "changed_value"),
    [
        ("username", "operator"),
        ("password", "new-password"),
        ("transport", "udp"),
        ("audio_enabled", False),
    ],
)
async def test_each_connection_field_restarts_the_session(
    changed_field: str, changed_value: object
) -> None:
    current = [_source(1)]
    source = RtspCameraSource(lambda: current)
    await source.connect_device(current[0].id, _video_cb, _audio_cb)
    original = source.get_session(current[0].id)

    current = [current[0].model_copy(update={changed_field: changed_value})]
    await source.apply_settings()

    assert source.get_session(current[0].id) is not original
    assert isinstance(original, _RecordingSession)
    assert original.stop_count == 1


@pytest.mark.asyncio
async def test_apply_settings_handles_enable_disable_addition_and_deletion() -> None:
    first = _source(1)
    disabled = _source(2, enabled=False)
    current = [first, disabled]
    source = RtspCameraSource(lambda: current)
    await source.connect_device(first.id, _video_cb, _audio_cb)
    first_session = source.get_session(first.id)

    added = _source(3)
    current = [first, disabled.model_copy(update={"enabled": True}), added]
    await source.apply_settings()
    assert set(await source.discover_devices()) == {first.id, disabled.id, added.id}
    assert source.get_session(disabled.id) is None
    assert source.get_session(added.id) is None

    await source.connect_device(disabled.id, _video_cb, _audio_cb)
    await source.connect_device(added.id, _video_cb, _audio_cb)
    assert source.get_session(disabled.id) is not None
    assert source.get_session(added.id) is not None

    current = [first.model_copy(update={"enabled": False}), added]
    await source.apply_settings()
    assert source.get_session(first.id) is None
    assert isinstance(first_session, _RecordingSession)
    assert first_session.stop_count == 1
    assert source.get_session(disabled.id) is None
    assert set(await source.discover_devices()) == {added.id}


@pytest.mark.asyncio
async def test_apply_settings_isolates_restart_failures_and_redacts_details(
    caplog: pytest.LogCaptureFixture,
) -> None:
    failing = _source(1)
    healthy = _source(2)
    current = [failing, healthy]
    source = RtspCameraSource(lambda: current)
    await source.connect_device(failing.id, _video_cb, _audio_cb)
    await source.connect_device(healthy.id, _video_cb, _audio_cb)
    healthy_original = source.get_session(healthy.id)
    _RecordingSession.fail_start_hosts = {"private-host"}
    current = [
        failing.model_copy(update={"uri": "rtsp://private-host/secret-path"}),
        healthy.model_copy(update={"uri": "rtsp://healthy-replacement/stream"}),
    ]

    with caplog.at_level(
        logging.ERROR, logger="miloco.perception.collect.rtsp_camera_source"
    ):
        await source.apply_settings()

    assert source.get_session(failing.id) is None
    assert source.get_session(healthy.id) is not healthy_original
    failed_replacement = _RecordingSession.instances[-2]
    assert failed_replacement.source.uri == "rtsp://private-host/secret-path"
    assert failed_replacement.stop_count == 1
    assert "private-host" not in caplog.text
    assert "secret-path" not in caplog.text
    assert "secret start failure" not in caplog.text


@pytest.mark.asyncio
async def test_failed_stop_is_retained_and_retried_by_later_cleanup() -> None:
    configured = _source(1)
    source = RtspCameraSource(lambda: [configured])
    await source.connect_device(configured.id, _video_cb, _audio_cb)
    session = source.get_session(configured.id)
    assert isinstance(session, _RecordingSession)
    _RecordingSession.fail_stop_once_ids = {configured.id}

    await source.disconnect_device(configured.id)
    assert session.stop_count == 1
    assert session.connected is True
    assert configured.id in source._pending_cleanup

    await source.disconnect_device(configured.id)
    assert session.stop_count == 2
    assert session.connected is False
    assert configured.id not in source._pending_cleanup


@pytest.mark.asyncio
async def test_shutdown_releases_all_sessions_when_one_stop_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = _source(1)
    second = _source(2)
    source = RtspCameraSource(lambda: [first, second])
    await source.connect_device(first.id, _video_cb, _audio_cb)
    await source.connect_device(second.id, _video_cb, _audio_cb)
    sessions = list(_RecordingSession.instances)
    _RecordingSession.fail_stop_ids = {first.id}

    with caplog.at_level(
        logging.ERROR, logger="miloco.perception.collect.rtsp_camera_source"
    ):
        await source.shutdown()

    assert [session.stop_count for session in sessions] == [1, 1]
    assert source.get_session(first.id) is None
    assert source.get_session(second.id) is None
    assert "secret stop failure" not in caplog.text
