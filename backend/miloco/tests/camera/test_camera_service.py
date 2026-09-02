from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from miloco.camera.schema import RtspSourceUpsert
from miloco.camera.service import (
    CameraConflictError,
    CameraInvalidPromptError,
    CameraNotFoundError,
    CameraService,
)
from miloco.config.settings import RtspSourceSettings
from miloco.database.kv_repo import ScopeConfigKeys
from miloco.miot.filter import MAX_CAMERA_PROMPT_LEN
from miloco.perception.collect.camera_adapter import CameraDeviceAdapter
from miloco.perception.collect.camera_source import CameraSourceState
from miloco.perception.collect.rtsp_camera_source import RtspCameraSource
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
        self._lock = threading.Lock()

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

    def mutate(self, mutation):
        with self._lock:
            self.events.append("persist")
            if self.fail_write:
                raise OSError("disk private detail")
            current = [source.model_dump() for source in self.sources]
            raw_sources = mutation(current)
            self.sources = [
                RtspSourceSettings.model_validate(item) for item in raw_sources
            ]
            self.write_count += 1
            return {"camera": {"rtsp_sources": raw_sources}}


class _FakeKV:
    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.store = dict(initial or {})
        self.write_count = 0

    def get(self, key: str, default_value: str | None = None) -> str | None:
        return self.store.get(key, default_value)

    def set(self, key: str, value: str) -> bool:
        self.store[key] = value
        self.write_count += 1
        return True


class _Perception:
    def __init__(
        self,
        *,
        sync_results: list[bool | BaseException] | None = None,
        states: dict[str, CameraSourceState] | None = None,
    ) -> None:
        self.sync_results = list(sync_results or [True])
        self.sync_count = 0
        self.retry_count = 0
        self.retry_ids: list[str] = []
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

    async def retry_camera_source(self, camera_id: str) -> bool:
        self.events.append("retry")
        self.retry_count += 1
        self.retry_ids.append(camera_id)
        return True


class _Miot:
    def __init__(self, cameras: list[dict] | None = None) -> None:
        self.cameras = cameras or []

    async def list_cameras_with_state(self) -> list[dict]:
        return list(self.cameras)


class _ApiTerminalSession:
    def __init__(self, source: RtspSourceSettings) -> None:
        self.source = source
        self.active = False
        self.terminal = False
        self.state_override = CameraSourceState(connected=False)

    async def start(self, video_cb, audio_cb) -> None:
        del video_cb, audio_cb
        self.active = True
        self.state_override = CameraSourceState(connected=True)

    async def stop(self) -> None:
        self.active = False
        self.state_override = CameraSourceState(
            connected=False,
            error_code=self.state_override.error_code,
            error_message=self.state_override.error_message,
        )

    def state(self) -> CameraSourceState:
        return self.state_override

    def is_active(self) -> bool:
        return self.active

    def is_terminal(self) -> bool:
        return self.terminal


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
    kv_repo: _FakeKV | None = None,
) -> CameraService:
    async def successful_probe(_source: RtspSourceSettings) -> RtspProbeResult:
        return _probe_result()

    kwargs = {}
    if kv_repo is not None:
        kwargs["kv_repo"] = kv_repo
    return CameraService(
        miot or _Miot(),
        perception or _Perception(),
        settings_loader=store.load,
        sources_mutator=store.mutate,
        probe=probe or successful_probe,
        **kwargs,
    )


def _transactional_service(
    store: _ConfigStore,
    perception: _Perception | None = None,
    *,
    probe: Callable | None = None,
) -> CameraService:
    async def successful_probe(_source: RtspSourceSettings) -> RtspProbeResult:
        return _probe_result()

    return CameraService(
        _Miot(),
        perception or _Perception(),
        settings_loader=store.load,
        sources_mutator=store.mutate,
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
                last_frame_unix_ms=1_787_851_234_567,
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
        "last_frame_unix_ms": 1_787_851_234_567,
            "has_password": True,
            "error_code": None,
            "error_message": None,
            "perception_prompt": "",
        }
    assert "stored-secret" not in repr(summaries)
    assert "camera-user" not in repr(summaries)


@pytest.mark.asyncio
async def test_list_reports_null_frame_time_until_rtsp_source_decodes_a_frame() -> None:
    summaries = await _service(
        _ConfigStore([_source(enabled=True)]),
        _Perception(states={SOURCE_ID: CameraSourceState(connected=True)}),
    ).list_cameras()

    assert summaries[0].last_frame_unix_ms is None


@pytest.mark.asyncio
async def test_list_reports_rtsp_perception_prompt_from_shared_prompt_map() -> None:
    store = _ConfigStore([_source(enabled=True)])
    kv = _FakeKV(
        {
            ScopeConfigKeys.CAMERA_PROMPT_MAP_KEY: json.dumps(
                {SOURCE_ID: "客厅画面右侧电视反光请忽略"}
            )
        }
    )

    summaries = await _service(store, kv_repo=kv).list_cameras()

    assert summaries[0].model_dump()["perception_prompt"] == "客厅画面右侧电视反光请忽略"
    assert summaries[0].perception_prompt == "客厅画面右侧电视反光请忽略"
    serialized = repr(summaries) + str(summaries[0].model_dump())
    assert "stored-secret" not in serialized
    assert "camera-user" not in serialized
    assert "rtsp://camera.local/live" not in serialized


@pytest.mark.asyncio
async def test_set_rtsp_perception_prompt_writes_shared_map_without_reconnect() -> None:
    store = _ConfigStore([_source(enabled=False)])
    kv = _FakeKV()
    perception = _Perception()

    async def forbidden_probe(_source: RtspSourceSettings) -> RtspProbeResult:
        raise AssertionError("prompt updates must not probe RTSP streams")

    updated = await _service(
        store, perception, probe=forbidden_probe, kv_repo=kv
    ).set_prompt(SOURCE_ID, "  门口左下角摆件不是宠物  ")

    assert updated.perception_prompt == "门口左下角摆件不是宠物"
    assert json.loads(kv.get(ScopeConfigKeys.CAMERA_PROMPT_MAP_KEY) or "{}") == {
        SOURCE_ID: "门口左下角摆件不是宠物"
    }
    assert store.write_count == 0
    assert perception.sync_count == 0
    assert perception.retry_count == 0


@pytest.mark.asyncio
async def test_clear_rtsp_perception_prompt_deletes_shared_map_without_reconnect() -> None:
    store = _ConfigStore([_source(enabled=True)])
    kv = _FakeKV(
        {ScopeConfigKeys.CAMERA_PROMPT_MAP_KEY: json.dumps({SOURCE_ID: "旧须知"})}
    )
    perception = _Perception()

    cleared = await _service(store, perception, kv_repo=kv).clear_prompt(SOURCE_ID)

    assert cleared.perception_prompt == ""
    assert json.loads(kv.get(ScopeConfigKeys.CAMERA_PROMPT_MAP_KEY) or "{}") == {}
    assert store.write_count == 0
    assert perception.sync_count == 0
    assert perception.retry_count == 0


@pytest.mark.asyncio
async def test_rtsp_perception_prompt_rejects_unknown_and_too_long_without_write() -> None:
    kv = _FakeKV()
    service = _service(_ConfigStore(), kv_repo=kv)

    with pytest.raises(CameraNotFoundError):
        await service.set_prompt(SOURCE_ID, "须知")
    with pytest.raises(CameraInvalidPromptError, match="感知须知过长") as caught:
        await _service(_ConfigStore([_source()]), kv_repo=kv).set_prompt(
            SOURCE_ID, "字" * (MAX_CAMERA_PROMPT_LEN + 1)
        )
    assert caught.value.code == "invalid_camera_prompt"

    assert kv.get(ScopeConfigKeys.CAMERA_PROMPT_MAP_KEY) is None
    assert kv.write_count == 0


@pytest.mark.asyncio
async def test_list_preserves_safe_terminal_status_after_adapter_prunes_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "miloco.perception.collect.rtsp_camera_source.RtspSession",
        _ApiTerminalSession,
    )
    store = _ConfigStore(
        [
            _source(
                enabled=True,
                uri="rtsp://private-host.example/secret-path",
                password="private-password",
            )
        ]
    )
    source = RtspCameraSource(lambda: store.load().camera.rtsp_sources)
    adapter = CameraDeviceAdapter(sources=[source])
    await adapter.sync_devices()
    session = source.get_session(SOURCE_ID)
    assert isinstance(session, _ApiTerminalSession)
    session.active = False
    session.terminal = True
    session.state_override = CameraSourceState(
        connected=False,
        error_code="resource_not_found",
        error_message="RTSP resource was not found",
    )

    await adapter.sync_devices()
    await adapter.sync_devices()
    perception = _Perception()
    perception._rtsp_camera_source = SimpleNamespace(get_state=source.get_state)

    summaries = await _service(store, perception).list_cameras()

    assert summaries[0].connected is False
    assert summaries[0].error_code == "resource_not_found"
    assert summaries[0].error_message == "RTSP resource was not found"
    assert "private-password" not in repr(summaries)
    assert "secret-path" not in repr(summaries)


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
async def test_edit_enabled_source_probes_complete_candidate_before_persist_and_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _ConfigStore([_source(enabled=True)])
    perception = _Perception()
    events: list[str] = []
    seen: list[RtspSourceSettings] = []

    async def probe(candidate: RtspSourceSettings) -> RtspProbeResult:
        events.append("probe")
        seen.append(candidate)
        return _probe_result()

    original_mutate = store.mutate

    def mutate(mutation):
        events.append("persist")
        return original_mutate(mutation)

    async def sync() -> bool:
        events.append("sync")
        perception.sync_count += 1
        return True

    monkeypatch.setattr(store, "mutate", mutate)
    monkeypatch.setattr(perception, "sync_camera_sources", sync)

    edited = await _service(store, perception, probe=probe).edit_rtsp(
        SOURCE_ID,
        _upsert(name="Renamed", uri="rtsps://new-camera.local/live", password=""),
    )

    assert events == ["probe", "persist", "sync"]
    assert seen == [store.sources[0]]
    assert seen[0].enabled is True
    assert seen[0].password == "stored-secret"
    assert edited.enabled is True


@pytest.mark.asyncio
async def test_edit_enabled_probe_failure_preserves_persisted_and_runtime_source() -> (
    None
):
    original = _source(enabled=True)
    store = _ConfigStore([original])
    perception = _Perception()
    old_runtime = perception._rtsp_camera_source
    seen: list[RtspSourceSettings] = []

    async def probe(candidate: RtspSourceSettings) -> RtspProbeResult:
        seen.append(candidate)
        raise RtspSourceError(
            "authentication_failed", "RTSP authentication failed", recoverable=False
        )

    with pytest.raises(RtspSourceError) as caught:
        await _service(store, perception, probe=probe).edit_rtsp(
            SOURCE_ID,
            _upsert(
                name="New private name",
                uri="rtsps://private-new.example/secret-path",
                password="",
            ),
        )

    assert seen[0].password == "stored-secret"
    assert seen[0].enabled is True
    assert store.write_count == 0
    assert store.sources == [original]
    assert perception.sync_count == 0
    assert perception._rtsp_camera_source is old_runtime
    assert caught.value.code == "authentication_failed"
    assert str(caught.value) == "RTSP authentication failed"
    assert "secret-path" not in repr(caught.value)
    assert "stored-secret" not in repr(caught.value)


@pytest.mark.asyncio
async def test_edit_disabled_source_still_allows_offline_save_without_probe() -> None:
    store = _ConfigStore([_source(enabled=False)])
    perception = _Perception()

    async def forbidden_probe(_candidate: RtspSourceSettings) -> RtspProbeResult:
        raise AssertionError("disabled edit must not probe")

    edited = await _service(store, perception, probe=forbidden_probe).edit_rtsp(
        SOURCE_ID,
        _upsert(uri="rtsp://offline-camera.local/live", password=""),
    )

    assert edited.enabled is False
    assert store.sources[0].uri == "rtsp://offline-camera.local/live"
    assert store.sources[0].password == "stored-secret"
    assert store.write_count == 1
    assert perception.sync_count == 1


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
async def test_enable_probes_then_persists_then_hot_applies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _ConfigStore([_source()])
    perception = _Perception()
    events: list[str] = []

    async def probe(_source: RtspSourceSettings) -> RtspProbeResult:
        events.append("probe")
        return _probe_result()

    original_mutate = store.mutate

    def mutate(mutation):
        events.append("persist")
        return original_mutate(mutation)

    async def sync() -> bool:
        events.append("sync")
        perception.sync_count += 1
        return True

    monkeypatch.setattr(store, "mutate", mutate)
    monkeypatch.setattr(perception, "sync_camera_sources", sync)

    enabled = await _service(store, perception, probe=probe).enable(SOURCE_ID)

    assert events == ["probe", "persist", "sync"]
    assert enabled.enabled is True
    assert store.sources[0].enabled is True


@pytest.mark.asyncio
async def test_enable_already_enabled_probes_then_explicitly_retries_without_write() -> (
    None
):
    store = _ConfigStore([_source(enabled=True)])
    perception = _Perception()

    async def probe(_source: RtspSourceSettings) -> RtspProbeResult:
        perception.events.append("probe")
        return _probe_result()

    enabled = await _service(store, perception, probe=probe).enable(SOURCE_ID)

    assert perception.events == ["probe", "retry"]
    assert enabled.enabled is True
    assert store.write_count == 0
    assert perception.sync_count == 0
    assert perception.retry_ids == [SOURCE_ID]


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
@pytest.mark.parametrize(
    "initial_result",
    [False, RuntimeError("initial apply private detail")],
)
async def test_enable_hot_apply_failure_compensates_disabled_and_cleans_up(
    initial_result: bool | BaseException,
) -> None:
    store = _ConfigStore([_source()])
    perception = _Perception(sync_results=[initial_result, True])

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


@pytest.mark.asyncio
async def test_probe_cancellation_propagates_without_mutation() -> None:
    store = _ConfigStore([_source()])
    perception = _Perception()
    probe_started = asyncio.Event()

    async def blocking_probe(_source: RtspSourceSettings) -> RtspProbeResult:
        probe_started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    task = asyncio.create_task(
        _transactional_service(store, perception, probe=blocking_probe).enable(
            SOURCE_ID
        )
    )
    await probe_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.write_count == 0
    assert store.sources[0].enabled is False
    assert perception.sync_count == 0


class _BlockingRuntimePerception(_Perception):
    def __init__(self, store: _ConfigStore, results: list[bool]) -> None:
        super().__init__()
        self._store = store
        self._results = list(results)
        self.first_sync_started = asyncio.Event()
        self.release_first_sync = asyncio.Event()
        self.runtime_enabled = False

    async def sync_camera_sources(self) -> bool:
        self.sync_count += 1
        if self.sync_count == 1:
            self.first_sync_started.set()
            await self.release_first_sync.wait()
        result = self._results.pop(0)
        if result:
            self.runtime_enabled = self._store.sources[0].enabled
        return result


@pytest.mark.asyncio
async def test_cancel_during_successful_enable_waits_for_stable_enabled_state() -> None:
    store = _ConfigStore([_source()])
    perception = _BlockingRuntimePerception(store, [True])
    task = asyncio.create_task(
        _transactional_service(store, perception).enable(SOURCE_ID)
    )
    await perception.first_sync_started.wait()

    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    perception.release_first_sync.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.sources[0].enabled is True
    assert perception.runtime_enabled is True
    assert perception.sync_count == 1


@pytest.mark.asyncio
async def test_cancel_during_failed_enable_waits_for_compensation_cleanup() -> None:
    store = _ConfigStore([_source()])
    perception = _BlockingRuntimePerception(store, [False, True])
    task = asyncio.create_task(
        _transactional_service(store, perception).enable(SOURCE_ID)
    )
    await perception.first_sync_started.wait()

    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    perception.release_first_sync.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.sources[0].enabled is False
    assert perception.runtime_enabled is False
    assert perception.sync_count == 2
    assert store.write_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cleanup_result",
    [False, RuntimeError("cleanup private detail")],
)
async def test_cleanup_failure_returns_stable_code_and_keeps_disabled(
    cleanup_result: bool | BaseException,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = _ConfigStore([_source()])
    perception = _Perception(sync_results=[False, cleanup_result])

    with pytest.raises(CameraConflictError) as caught:
        await _transactional_service(store, perception).enable(SOURCE_ID)

    assert caught.value.code == "cleanup_failed"
    assert caught.value.safe_message == "Camera rollback cleanup could not be applied"
    assert store.sources[0].enabled is False
    assert perception.sync_count == 2
    assert "cleanup private detail" not in caplog.text


@pytest.mark.asyncio
async def test_two_service_instances_create_without_lost_update() -> None:
    store = _ConfigStore()
    first = _transactional_service(store)
    second = _transactional_service(store)

    await asyncio.gather(
        first.create_rtsp(_upsert(name="First", uri="rtsp://first.local/live")),
        second.create_rtsp(_upsert(name="Second", uri="rtsp://second.local/live")),
    )

    assert {source.name for source in store.sources} == {"First", "Second"}


@pytest.mark.asyncio
async def test_two_service_instances_edit_different_sources_without_lost_update() -> (
    None
):
    store = _ConfigStore([_source(), _source(SECOND_SOURCE_ID, name="Bedroom")])
    first = _transactional_service(store)
    second = _transactional_service(store)

    await asyncio.gather(
        first.edit_rtsp(SOURCE_ID, _upsert(name="First Updated")),
        second.edit_rtsp(
            SECOND_SOURCE_ID,
            _upsert(name="Second Updated", uri="rtsp://second.local/live"),
        ),
    )

    assert {source.id: source.name for source in store.sources} == {
        SOURCE_ID: "First Updated",
        SECOND_SOURCE_ID: "Second Updated",
    }


@pytest.mark.asyncio
async def test_stale_blank_password_edit_cannot_overwrite_concurrent_password_update() -> (
    None
):
    store = _ConfigStore([_source(enabled=True)])
    perception = _Perception()
    stale_probe_started = asyncio.Event()
    release_stale_probe = asyncio.Event()

    async def stale_probe(_candidate: RtspSourceSettings) -> RtspProbeResult:
        stale_probe_started.set()
        await release_stale_probe.wait()
        return _probe_result()

    stale = _transactional_service(store, perception, probe=stale_probe)
    fresh = _transactional_service(store, perception)
    stale_task = asyncio.create_task(
        stale.edit_rtsp(SOURCE_ID, _upsert(name="Stale", password=""))
    )
    await stale_probe_started.wait()
    await fresh.edit_rtsp(
        SOURCE_ID, _upsert(name="Fresh", password="new-private-password")
    )
    release_stale_probe.set()

    with pytest.raises(CameraConflictError) as caught:
        await stale_task

    assert caught.value.code == "camera_configuration_changed"
    assert caught.value.safe_message == "Camera configuration changed; retry the update"
    assert "new-private-password" not in repr(caught.value)
    assert store.sources[0].name == "Fresh"
    assert store.sources[0].password == "new-private-password"
    assert store.write_count == 1
    assert perception.sync_count == 1


@pytest.mark.asyncio
async def test_disabled_edit_cannot_become_enabled_without_probing_candidate() -> None:
    store = _ConfigStore([_source(enabled=False)])
    perception = _Perception()
    stale_commit_started = threading.Event()
    release_stale_commit = threading.Event()

    def delayed_mutator(mutation):
        stale_commit_started.set()
        release_stale_commit.wait()
        return store.mutate(mutation)

    stale = CameraService(
        _Miot(),
        perception,
        settings_loader=store.load,
        sources_mutator=delayed_mutator,
    )
    fresh = _transactional_service(store, perception)
    stale_task = asyncio.create_task(
        stale.edit_rtsp(SOURCE_ID, _upsert(name="Unprobed edit"))
    )
    await asyncio.to_thread(stale_commit_started.wait)
    await fresh.enable(SOURCE_ID)
    release_stale_commit.set()

    with pytest.raises(CameraConflictError) as caught:
        await stale_task

    assert caught.value.code == "camera_configuration_changed"
    assert store.sources[0].enabled is True
    assert store.sources[0].name == "Living Room"
    assert store.write_count == 1
    assert perception.sync_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent_change", ["disable", "delete", "connection"])
async def test_enabled_edit_rejects_any_concurrent_source_revision_change(
    concurrent_change: str,
) -> None:
    store = _ConfigStore([_source(enabled=True)])
    perception = _Perception()
    stale_probe_started = asyncio.Event()
    release_stale_probe = asyncio.Event()

    async def stale_probe(_candidate: RtspSourceSettings) -> RtspProbeResult:
        stale_probe_started.set()
        await release_stale_probe.wait()
        return _probe_result()

    stale = _transactional_service(store, perception, probe=stale_probe)
    fresh = _transactional_service(store, perception)
    stale_task = asyncio.create_task(
        stale.edit_rtsp(SOURCE_ID, _upsert(name="Stale edit"))
    )
    await stale_probe_started.wait()
    if concurrent_change == "disable":
        await fresh.disable(SOURCE_ID)
    elif concurrent_change == "delete":
        await fresh.delete(SOURCE_ID)
    else:
        await fresh.edit_rtsp(
            SOURCE_ID,
            _upsert(name="Fresh connection", uri="rtsps://fresh.local/live"),
        )
    release_stale_probe.set()

    with pytest.raises(CameraConflictError) as caught:
        await stale_task

    assert caught.value.code == "camera_configuration_changed"
    assert store.write_count == 1
    assert perception.sync_count == 1
    if concurrent_change == "disable":
        assert store.sources[0].enabled is False
    elif concurrent_change == "delete":
        assert store.sources == []
    else:
        assert store.sources[0].uri == "rtsps://fresh.local/live"
        assert store.sources[0].name == "Fresh connection"


@pytest.mark.asyncio
async def test_edit_probe_does_not_hold_instance_write_lock() -> None:
    store = _ConfigStore(
        [_source(enabled=True), _source(SECOND_SOURCE_ID, enabled=True)]
    )
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()

    async def probe(candidate: RtspSourceSettings) -> RtspProbeResult:
        if candidate.id == SOURCE_ID:
            probe_started.set()
            await release_probe.wait()
        return _probe_result()

    service = _transactional_service(store, probe=probe)
    edit_task = asyncio.create_task(
        service.edit_rtsp(SOURCE_ID, _upsert(name="Updated"))
    )
    await probe_started.wait()
    disable_task = asyncio.create_task(service.disable(SECOND_SOURCE_ID))
    try:
        await asyncio.wait_for(asyncio.shield(disable_task), timeout=0.5)
        completed_while_probe_blocked = True
    except TimeoutError:
        completed_while_probe_blocked = False
    finally:
        release_probe.set()
    await asyncio.gather(edit_task, disable_task)
    assert completed_while_probe_blocked
    assert store.sources[1].enabled is False


@pytest.mark.asyncio
async def test_post_write_reload_validation_is_stable_and_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "reload-secret"
    store = _ConfigStore([_source(password=secret)])
    original_load = store.load
    load_count = 0

    def invalid_after_write():
        nonlocal load_count
        load_count += 1
        if load_count >= 1:
            raise ValueError(
                f"invalid rtsp://synthetic-user:{secret}@camera.local/live"
            )
        return original_load()

    service = CameraService(
        _Miot(),
        _Perception(),
        settings_loader=invalid_after_write,
        sources_mutator=store.mutate,
    )

    with pytest.raises(CameraConflictError) as caught:
        await service.disable(SOURCE_ID)

    assert caught.value.code == "persistence_failed"
    assert secret not in caplog.text
    assert "synthetic-user" not in caplog.text
    assert "rtsp://" not in caplog.text


@pytest.mark.asyncio
async def test_list_reload_validation_is_stable_and_redacted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "list-reload-secret"

    def invalid_load():
        raise ValueError(f"invalid rtsp://synthetic-user:{secret}@camera.local/live")

    service = CameraService(
        _Miot(),
        _Perception(),
        settings_loader=invalid_load,
        sources_mutator=lambda mutation: {},
    )

    with pytest.raises(CameraConflictError) as caught:
        await service.list_cameras()

    assert caught.value.code == "persistence_failed"
    assert secret not in caplog.text
    assert "synthetic-user" not in caplog.text
    assert "rtsp://" not in caplog.text
