from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from miloco.camera.schema import RtspSourceUpsert
from miloco.camera.service import (
    CameraConflictError,
    CameraNotFoundError,
    CameraService,
)
from miloco.config.settings import RtspSourceSettings
from miloco.perception.collect.camera_source import CameraSourceState
from miloco.perception.collect.rtsp_probe import RtspProbeResult, RtspSourceError

SOURCE_ID = "rtsp:00000000-0000-0000-0000-000000000001"
SECOND_SOURCE_ID = "rtsp:00000000-0000-0000-0000-000000000002"


def _source(
    source_id: str = SOURCE_ID,
    *,
    name: str = "Living Room",
    uri: str = "rtsp://camera.local/live",
    password: str = "stored-secret",
    enabled: bool = False,
) -> RtspSourceSettings:
    return RtspSourceSettings(
        id=source_id,
        name=name,
        room_name="Living Room",
        uri=uri,
        username="camera-user",
        password=password,
        transport="tcp",
        audio_enabled=True,
        enabled=enabled,
    )


def _upsert(
    *,
    name: str = "Living Room",
    uri: str = "rtsp://camera.local/live",
    password: str = "",
) -> RtspSourceUpsert:
    return RtspSourceUpsert(
        name=name,
        room_name="Living Room",
        uri=uri,
        username="camera-user",
        password=password,
        transport="tcp",
        audio_enabled=True,
    )


class _ConfigStore:
    def __init__(self, sources: list[RtspSourceSettings] | None = None) -> None:
        self.sources = list(sources or [])
        self.write_count = 0
        self.fail_write = False
        self.events: list[str] = []

    def load(self):
        return SimpleNamespace(camera=SimpleNamespace(rtsp_sources=list(self.sources)))

    def write(self, **updates):
        self.events.append("persist")
        self.write_count += 1
        if self.fail_write:
            raise OSError("disk private detail")
        raw_sources = updates["camera"]["rtsp_sources"]
        self.sources = [RtspSourceSettings.model_validate(item) for item in raw_sources]
        return updates


class _Perception:
    def __init__(
        self,
        *,
        sync_results: list[bool | BaseException] | None = None,
        states: dict[str, CameraSourceState] | None = None,
    ) -> None:
        self.sync_results = list(sync_results or [True])
        self.sync_count = 0
        self.events: list[str] = []
        state_map = states or {}
        self._rtsp_camera_source = SimpleNamespace(
            get_state=lambda did: state_map.get(did, CameraSourceState(False))
        )

    async def sync_camera_sources(self) -> bool:
        self.events.append("sync")
        self.sync_count += 1
        result = self.sync_results.pop(0) if self.sync_results else True
        if isinstance(result, BaseException):
            raise result
        return result


class _Miot:
    def __init__(self, cameras: list[dict] | None = None) -> None:
        self.cameras = cameras or []

    async def list_cameras_with_state(self) -> list[dict]:
        return list(self.cameras)


def _probe_result() -> RtspProbeResult:
    return RtspProbeResult(
        video_codec="h264",
        width=1920,
        height=1080,
        fps=25.0,
        time_base="1/90000",
        audio_codec="aac",
        audio_sample_rate=48000,
    )


def _service(
    store: _ConfigStore,
    perception: _Perception | None = None,
    *,
    miot: _Miot | None = None,
    probe: Callable | None = None,
) -> CameraService:
    async def successful_probe(_source: RtspSourceSettings) -> RtspProbeResult:
        return _probe_result()

    return CameraService(
        miot or _Miot(),
        perception or _Perception(),
        settings_loader=store.load,
        config_writer=store.write,
        probe=probe or successful_probe,
    )


@pytest.mark.asyncio
async def test_list_aggregates_miot_and_redacted_rtsp_state() -> None:
    store = _ConfigStore([_source(enabled=True)])
    perception = _Perception(
        states={
            SOURCE_ID: CameraSourceState(
                connected=True,
                video_codec="h264",
                audio_codec="aac",
                error_code=None,
                error_message=None,
            )
        }
    )
    miot = _Miot(
        [
            {
                "did": "miot-device",
                "channel": 0,
                "channel_count": 1,
                "name": "MIoT Camera",
                "room_name": "Kitchen",
                "in_use": True,
                "connected": True,
            }
        ]
    )

    summaries = await _service(store, perception, miot=miot).list_cameras()

    assert [item.id for item in summaries] == ["miot-device", SOURCE_ID]
    assert summaries[0].source_type == "miot"
    assert summaries[1].model_dump() == {
        "id": SOURCE_ID,
        "source_type": "rtsp",
        "name": "Living Room",
        "room_name": "Living Room",
        "enabled": True,
        "connected": True,
        "video_codec": "h264",
        "audio_codec": "aac",
        "has_password": True,
        "error_code": None,
        "error_message": None,
    }
    assert "stored-secret" not in repr(summaries)
    assert "camera-user" not in repr(summaries)


@pytest.mark.asyncio
async def test_create_generates_immutable_id_and_always_saves_disabled() -> None:
    store = _ConfigStore()
    perception = _Perception()
    body = RtspSourceUpsert.model_validate(
        {**_upsert(password="new-secret").model_dump(), "id": "rtsp:client-controlled"}
    )

    created = await _service(store, perception).create_rtsp(body)

    assert created.id.startswith("rtsp:")
    assert created.id != "rtsp:client-controlled"
    assert store.sources[0].id == created.id
    assert store.sources[0].enabled is False
    with pytest.raises(Exception):
        store.sources[0].id = SECOND_SOURCE_ID
    assert perception.sync_count == 1


@pytest.mark.asyncio
async def test_edit_preserves_id_and_blank_password() -> None:
    store = _ConfigStore([_source()])

    edited = await _service(store).edit_rtsp(
        SOURCE_ID,
        _upsert(name="Renamed", uri="rtsps://new-camera.local/stream", password=""),
    )

    assert edited.id == SOURCE_ID
    assert store.sources[0].id == SOURCE_ID
    assert store.sources[0].name == "Renamed"
    assert store.sources[0].password == "stored-secret"


@pytest.mark.asyncio
async def test_edit_replaces_password_only_when_non_empty() -> None:
    store = _ConfigStore([_source()])

    await _service(store).edit_rtsp(SOURCE_ID, _upsert(password="replacement"))

    assert store.sources[0].password == "replacement"


@pytest.mark.asyncio
async def test_test_source_does_not_write_or_hot_apply() -> None:
    store = _ConfigStore([_source()])
    perception = _Perception()
    seen: list[RtspSourceSettings] = []

    async def probe(source: RtspSourceSettings) -> RtspProbeResult:
        seen.append(source)
        return _probe_result()

    result = await _service(store, perception, probe=probe).test_rtsp(_upsert())

    assert result == _probe_result()
    assert seen[0].enabled is False
    assert seen[0].id.startswith("rtsp:")
    assert store.write_count == 0
    assert perception.sync_count == 0


@pytest.mark.asyncio
async def test_enable_probes_then_persists_then_hot_applies() -> None:
    store = _ConfigStore([_source()])
    perception = _Perception()
    events: list[str] = []

    async def probe(_source: RtspSourceSettings) -> RtspProbeResult:
        events.append("probe")
        return _probe_result()

    original_write = store.write

    def write(**updates):
        events.append("persist")
        return original_write(**updates)

    async def sync() -> bool:
        events.append("sync")
        perception.sync_count += 1
        return True

    store.write = write
    perception.sync_camera_sources = sync

    enabled = await _service(store, perception, probe=probe).enable(SOURCE_ID)

    assert events == ["probe", "persist", "sync"]
    assert enabled.enabled is True
    assert store.sources[0].enabled is True


@pytest.mark.asyncio
async def test_enable_probe_failure_has_zero_mutation() -> None:
    store = _ConfigStore([_source()])
    perception = _Perception()

    async def probe(_source: RtspSourceSettings) -> RtspProbeResult:
        raise RtspSourceError(
            "authentication_failed", "RTSP authentication failed", recoverable=False
        )

    with pytest.raises(RtspSourceError, match="RTSP authentication failed"):
        await _service(store, perception, probe=probe).enable(SOURCE_ID)

    assert store.write_count == 0
    assert store.sources[0].enabled is False
    assert perception.sync_count == 0


@pytest.mark.asyncio
async def test_persist_failure_never_hot_applies() -> None:
    store = _ConfigStore([_source()])
    store.fail_write = True
    perception = _Perception()

    with pytest.raises(CameraConflictError) as caught:
        await _service(store, perception).disable(SOURCE_ID)

    assert caught.value.code == "persistence_failed"
    assert perception.sync_count == 0


@pytest.mark.asyncio
async def test_enable_hot_apply_failure_compensates_disabled_and_cleans_up() -> None:
    store = _ConfigStore([_source()])
    perception = _Perception(sync_results=[False, True])

    with pytest.raises(CameraConflictError) as caught:
        await _service(store, perception).enable(SOURCE_ID)

    assert caught.value.code == "hot_apply_failed"
    assert store.write_count == 2
    assert store.sources[0].enabled is False
    assert perception.sync_count == 2


@pytest.mark.asyncio
async def test_disable_persists_then_releases_runtime_source() -> None:
    store = _ConfigStore([_source(enabled=True)])
    perception = _Perception()

    disabled = await _service(store, perception).disable(SOURCE_ID)

    assert disabled.enabled is False
    assert store.sources[0].enabled is False
    assert perception.sync_count == 1


@pytest.mark.asyncio
async def test_delete_persists_removal_then_releases_runtime_source() -> None:
    store = _ConfigStore([_source(enabled=True)])
    perception = _Perception()

    await _service(store, perception).delete(SOURCE_ID)

    assert store.sources == []
    assert perception.sync_count == 1


@pytest.mark.asyncio
async def test_missing_source_raises_stable_not_found() -> None:
    store = _ConfigStore()

    with pytest.raises(CameraNotFoundError) as caught:
        await _service(store).disable(SOURCE_ID)

    assert caught.value.code == "camera_not_found"
    assert SOURCE_ID not in caught.value.safe_message


@pytest.mark.asyncio
async def test_concurrent_enables_do_not_lose_another_source_update() -> None:
    store = _ConfigStore([_source(), _source(SECOND_SOURCE_ID, name="Bedroom")])
    first_probe_started = asyncio.Event()
    release_first_probe = asyncio.Event()

    async def probe(source: RtspSourceSettings) -> RtspProbeResult:
        if source.id == SOURCE_ID:
            first_probe_started.set()
            await release_first_probe.wait()
        return _probe_result()

    service = _service(store, probe=probe)
    first = asyncio.create_task(service.enable(SOURCE_ID))
    await first_probe_started.wait()
    second = asyncio.create_task(service.enable(SECOND_SOURCE_ID))
    await asyncio.sleep(0)
    release_first_probe.set()
    await asyncio.gather(first, second)

    assert {source.id for source in store.sources if source.enabled} == {
        SOURCE_ID,
        SECOND_SOURCE_ID,
    }
