# RTSP Perception Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add manually managed RTSP/H.264/H.265 camera sources to Miloco's existing perception pipeline without changing existing MIoT device identities or downstream semantics.

**Architecture:** Keep `CameraDeviceAdapter` as the collector's only top-level `camera` adapter. Extract MIoT transport into `MiotCameraSource`, add `RtspCameraSource` backed by one `RtspSession` per enabled source, and aggregate both sources through a generic camera service/API. Persist RTSP configuration in the existing shared config with owner-only permissions and redact credentials at every API/log boundary.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, PyAV/FFmpeg, NumPy, pytest/pytest-asyncio, Click, uv, Ruff, ty.

**Spec:** [RTSP 摄像机与 OpenAI Responses 本地 Omni 支持设计](../specs/2026-08-28-rtsp-responses-support-design.md)

## Global Constraints

- This plan is noncommercial-only under the upstream Xiaomi Miloco license.
- Do not rename or prefix existing MIoT DIDs. Single-channel MIoT IDs remain `<did>` and multi-channel IDs remain `<did>:ch<n>`.
- New RTSP IDs are immutable `rtsp:<uuid>` values. Editing any user-visible field does not change the ID.
- Never log, trace, return, or persist a URI containing userinfo. API responses never return `password`; they return `has_password`.
- RTSP sources may be saved disabled while offline. Enabling requires a successful video preflight.
- One enabled RTSP source owns at most one session. Perception and later live-view consumers must reuse it.
- Queues are bounded and latest-biased. A failed RTSP source must not stop other sources or backend health.
- Do not implement live browser viewing in this plan; that is covered by the dependent live-view plan.
- Preserve unrelated dirty-worktree changes. Commit each completed task separately; do not push until the user provides or approves a writable remote.

---

## Task 1: Define and securely persist the RTSP configuration contract

**Files:**

- Modify: `backend/miloco/src/miloco/config/settings.py`
- Modify: `backend/miloco/src/miloco/config/settings.yaml`
- Modify: `backend/miloco/src/miloco/utils/agent_config.py`
- Modify: `cli/src/miloco_cli/config.py`
- Test: `backend/miloco/tests/config/test_rtsp_settings.py`
- Test: `backend/miloco/tests/utils/test_agent_config.py`
- Test: `cli/tests/test_config_permissions.py`

**Interfaces:**

```python
class RtspSourceSettings(BaseModel):
    id: str
    name: str
    room_name: str = ""
    uri: str
    username: str = ""
    password: str = ""
    transport: Literal["tcp", "udp"] = "tcp"
    audio_enabled: bool = True
    enabled: bool = False


class CameraSettings(BaseModel):
    frame_interval: int = 1000
    max_cache_images: int = 6
    rtsp_sources: list[RtspSourceSettings] = Field(default_factory=list)
```

- [ ] Add failing validation tests for `rtsp://` and `rtsps://`, rejection of other schemes, rejection of URI userinfo, `id.startswith("rtsp:")`, duplicate IDs, and default-disabled behavior.
- [ ] Run `cd backend && uv run pytest miloco/tests/config/test_rtsp_settings.py -q` and confirm the new tests fail because the model does not exist.
- [ ] Implement `RtspSourceSettings` with Pydantic validators. Parse the URI with `urllib.parse.urlsplit`; reject `parts.username`, `parts.password`, missing host, unsupported scheme, fragments, or control characters.
- [ ] Add `camera.rtsp_sources: []` to packaged defaults without creating an example credential.
- [ ] Add failing tests asserting both backend and CLI atomic writers produce mode `0o600`, including overwriting an existing mode-`0o644` file.
- [ ] Run `cd backend && uv run pytest miloco/tests/utils/test_agent_config.py -q` and `cd cli && uv run pytest tests/test_config_permissions.py -q`; confirm the permission assertions fail.
- [ ] In each atomic writer, set the temporary descriptor and final path explicitly:

```python
fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
os.fchmod(fd, 0o600)
try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    os.chmod(path, 0o600)
finally:
    if os.path.exists(tmp):
        os.unlink(tmp)
```

- [ ] Run all three focused test files and confirm they pass.
- [ ] Run `cd backend && uv run ruff check miloco/src/miloco/config/settings.py miloco/src/miloco/utils/agent_config.py miloco/tests/config/test_rtsp_settings.py miloco/tests/utils/test_agent_config.py` and `cd cli && uv run ruff check src/miloco_cli/config.py tests/test_config_permissions.py`.
- [ ] Commit: `git add backend/miloco/src/miloco/config/settings.py backend/miloco/src/miloco/config/settings.yaml backend/miloco/src/miloco/utils/agent_config.py backend/miloco/tests/config/test_rtsp_settings.py backend/miloco/tests/utils/test_agent_config.py cli/src/miloco_cli/config.py cli/tests/test_config_permissions.py && git commit -m "feat(camera): define secure RTSP source settings"`

## Task 2: Introduce the camera-source boundary and preserve MIoT behavior

**Files:**

- Create: `backend/miloco/src/miloco/perception/collect/camera_source.py`
- Create: `backend/miloco/src/miloco/perception/collect/miot_camera_source.py`
- Modify: `backend/miloco/src/miloco/perception/collect/camera_adapter.py`
- Modify: `backend/miloco/src/miloco/perception/__init__.py`
- Test: `backend/miloco/tests/perception/collect/test_camera_source.py`
- Modify/Test: existing `backend/miloco/tests/perception/collect/test_camera_adapter.py`

**Interfaces:**

```python
VideoFrameCallback = Callable[
    [str, NDArray[np.uint8], int, int, int, int], Awaitable[None]
]
AudioFrameCallback = Callable[
    [str, NDArray[np.int16], int, int, int, int], Awaitable[None]
]


@dataclass(frozen=True)
class CameraSourceState:
    connected: bool
    video_codec: str | None = None
    audio_codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    last_frame_unix_ms: int | None = None
    reconnect_attempt: int = 0
    dropped_frames: int = 0
    error_code: str | None = None
    error_message: str | None = None


class CameraSourceDriver(Protocol):
    source_type: Literal["miot", "rtsp"]
    async def discover_devices(self, all_devices: dict | None = None, **filters) -> dict[str, PerceptionDevice]: ...
    async def connect_device(self, did: str, video_cb: VideoFrameCallback, audio_cb: AudioFrameCallback) -> None: ...
    async def disconnect_device(self, did: str) -> None: ...
    def get_state(self, did: str) -> CameraSourceState: ...
    async def shutdown(self) -> None: ...
```

- [ ] Add characterization tests for current MIoT discovery, single/multi-channel DID mapping, callback registration/unregistration, collection, and shutdown. These tests must pass against the current adapter before the refactor.
- [ ] Run `cd backend && uv run pytest miloco/tests/perception/collect/test_camera_adapter.py -q` and save the passing baseline in the progress document.
- [ ] Add failing contract tests showing one `CameraDeviceAdapter` can accept two source drivers, merge discovery results, route each DID to its owning driver, and reject duplicate DIDs.
- [ ] Run the new contract test and confirm it fails because `CameraSourceDriver` injection is absent.
- [ ] Move only MIoT discovery/subscription concerns into `MiotCameraSource`; leave `_CameraDeviceState`, `MultiTrackSyncBuffer`, timestamp normalization, collection, and window callbacks in `CameraDeviceAdapter`.
- [ ] Change adapter construction to explicit sources:

```python
miot_source = MiotCameraSource(miot_proxy)
camera_adapter = CameraDeviceAdapter(
    sources=[miot_source],
    on_window_ready=lambda: loop.call_soon_threadsafe(window_ready_event.set),
)
```

- [ ] Make `CameraDeviceAdapter.discover_devices()` merge driver results deterministically and maintain a private `did -> source_type` ownership map. Raise a descriptive internal error on duplicate DIDs instead of allowing silent overwrite.
- [ ] Make connect/disconnect call the owning driver while common callbacks append `DecodedVideoFrame` and `DecodedAudioFrame` to the existing sync buffer.
- [ ] Run the old characterization suite and new contract suite; confirm no MIoT DID or collection behavior changed.
- [ ] Run Ruff and ty on the changed module set.
- [ ] Commit: `git add backend/miloco/src/miloco/perception/collect/camera_source.py backend/miloco/src/miloco/perception/collect/miot_camera_source.py backend/miloco/src/miloco/perception/collect/camera_adapter.py backend/miloco/src/miloco/perception/__init__.py backend/miloco/tests/perception/collect && git commit -m "refactor(camera): separate MIoT source transport"`

## Task 3: Implement credential-safe RTSP preflight and error classification

**Files:**

- Create: `backend/miloco/src/miloco/perception/collect/rtsp_probe.py`
- Test: `backend/miloco/tests/perception/collect/test_rtsp_probe.py`
- Fixture: `backend/miloco/tests/fixtures/rtsp/h264_video_audio.mkv`
- Fixture: `backend/miloco/tests/fixtures/rtsp/h265_video_only.mkv`

**Interfaces:**

```python
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
    def __init__(self, code: str, safe_message: str, *, recoverable: bool): ...


async def probe_rtsp_source(
    source: RtspSourceSettings,
    *,
    timeout_sec: float = 8.0,
    open_input: Callable[..., av.container.InputContainer] = av.open,
) -> RtspProbeResult: ...
```

- [ ] Generate the two tiny deterministic media fixtures with the repository's existing FFmpeg/PyAV tooling; keep each under 250 KB and include no audio in the H.265 file.
- [ ] Add failing tests for: H.264+audio success, H.265 video-only success, no-video rejection, auth failure classification, missing resource classification, transient timeout classification, total timeout, and secret redaction.
- [ ] Include adversarial credentials such as `user@example.com`, `p@ss:word`, and query-like characters; assert none appear in `str(error)`, `repr(error)`, or captured logs.
- [ ] Run `cd backend && uv run pytest miloco/tests/perception/collect/test_rtsp_probe.py -q` and confirm failure.
- [ ] Build FFmpeg options without embedding credentials in the stored URI:

```python
options = {
    "rtsp_transport": source.transport,
    "stimeout": str(int(timeout_sec * 1_000_000)),
}
container = open_input(
    source.uri,
    options=options,
    timeout=(timeout_sec, timeout_sec),
    metadata_errors="ignore",
)
```

- [ ] Build an authenticated URL only in a short-lived local variable immediately before `av.open`, using `urllib.parse.quote(..., safe="")` for username/password and `urlunsplit` for reconstruction. Never store that value on the session/source/result, never include it in an exception or log, and discard it when the call returns. The persisted `source.uri` remains userinfo-free.
- [ ] Decode at least one video frame and inspect the optional audio stream. Accept only H.264 and HEVC/H.265 for v1 perception.
- [ ] Map terminal errors to `invalid_uri`, `authentication_failed`, `resource_not_found`, `no_video_stream`, and `unsupported_video_codec`; map DNS, timeout, EOF, and reset failures to recoverable codes.
- [ ] Run the focused suite, Ruff, and ty; confirm all pass.
- [ ] Commit: `git add backend/miloco/src/miloco/perception/collect/rtsp_probe.py backend/miloco/tests/perception/collect/test_rtsp_probe.py backend/miloco/tests/fixtures/rtsp && git commit -m "feat(camera): add RTSP source preflight"`

## Task 4: Add one bounded, reconnecting RTSP session per source

**Files:**

- Create: `backend/miloco/src/miloco/perception/collect/rtsp_session.py`
- Test: `backend/miloco/tests/perception/collect/test_rtsp_session.py`

**Interfaces:**

```python
def reconnect_delay(attempt: int, *, jitter: float) -> float:
    base = min(60.0, float(2 ** max(0, attempt)))
    return max(0.0, base * (1.0 + jitter))


class RtspSession:
    def __init__(self, source: RtspSourceSettings, *, queue_size: int = 3): ...
    async def start(self, video_cb: VideoFrameCallback, audio_cb: AudioFrameCallback) -> None: ...
    async def stop(self) -> None: ...
    def state(self) -> CameraSourceState: ...
    def add_packet_listener(self, listener: PacketListener) -> Callable[[], None]: ...
```

- [ ] Add failing pure tests for base delays `1, 2, 4, 8, 16, 32, 60, 60`, deterministic positive/negative jitter bounds, and cancellation during backoff.
- [ ] Add failing async tests with fake containers for video-only decode, video+audio decode/resampling to mono signed-16-bit PCM, EOF reconnect, terminal-error stop, bounded queue drop-oldest semantics, callback failure isolation, and idempotent stop.
- [ ] Run the focused test and confirm failure before implementation.
- [ ] Implement the container demux/decode loop off the event loop with `asyncio.to_thread` or a dedicated worker thread. Marshal callbacks back to the owning loop with bounded queues.
- [ ] Convert decoded video to BGR24 `NDArray[np.uint8]`. Convert audio to mono `s16` at the existing 16 kHz pipeline rate when audio is enabled; omit audio work entirely when disabled.
- [ ] Record `last_frame_unix_ms`, codecs, dimensions, fps, reconnect attempt, drop count, and a safe error code/message. Never store raw exception text if it can include connection material.
- [ ] Make the packet-listener hook transport-neutral and dormant when unused; the dependent live-view plan will consume it without opening a second connection.
- [ ] Ensure terminal `RtspSourceError(recoverable=False)` leaves the session stopped. Recoverable failures close the container before sleeping and reset attempt count after stable frames resume.
- [ ] Run focused tests plus `cd backend && uv run pytest miloco/tests/perception/collect/test_rtsp_probe.py miloco/tests/perception/collect/test_rtsp_session.py -q`.
- [ ] Run Ruff and ty on `rtsp_probe.py`, `rtsp_session.py`, and their tests.
- [ ] Commit: `git add backend/miloco/src/miloco/perception/collect/rtsp_session.py backend/miloco/tests/perception/collect/test_rtsp_session.py && git commit -m "feat(camera): add bounded RTSP sessions"`

## Task 5: Connect RTSP sessions to the unified camera adapter

**Files:**

- Create: `backend/miloco/src/miloco/perception/collect/rtsp_camera_source.py`
- Modify: `backend/miloco/src/miloco/perception/__init__.py`
- Modify: `backend/miloco/src/miloco/perception/service.py`
- Test: `backend/miloco/tests/perception/collect/test_rtsp_camera_source.py`
- Test: `backend/miloco/tests/perception/collect/test_camera_adapter.py`

**Interfaces:**

```python
class RtspCameraSource:
    source_type = "rtsp"

    def __init__(self, settings_loader: Callable[[], list[RtspSourceSettings]]): ...
    async def apply_settings(self) -> None: ...
    def get_session(self, did: str) -> RtspSession | None: ...
```

- [ ] Add failing tests that discovery returns enabled RTSP sources with stable IDs/rooms, disabled sources are not connected, `apply_settings()` starts/stops only changed sessions, editing a URI restarts that source, and shutdown releases every session.
- [ ] Add an adapter-level test proving a failing RTSP session does not prevent MIoT collection or another RTSP source from producing `DeviceData`.
- [ ] Run both tests and confirm failure.
- [ ] Implement a settings-backed driver with a session registry keyed by immutable RTSP ID. Treat additions, enable/disable changes, connection-field changes, and deletion explicitly; do not recreate unchanged sessions.
- [ ] Construct both sources while retaining one collector adapter:

```python
camera_adapter = CameraDeviceAdapter(
    sources=[
        MiotCameraSource(miot_proxy),
        RtspCameraSource(lambda: get_settings().camera.rtsp_sources),
    ],
    on_window_ready=lambda: loop.call_soon_threadsafe(window_ready_event.set),
)
```

- [ ] Expose a narrow `PerceptionService.sync_camera_sources()` method that applies settings and runs adapter sync after management writes. Do not expose collector internals through the HTTP layer.
- [ ] Run focused tests, the existing perception collector test directory, Ruff, and ty.
- [ ] Commit: `git add backend/miloco/src/miloco/perception/collect/rtsp_camera_source.py backend/miloco/src/miloco/perception/__init__.py backend/miloco/src/miloco/perception/service.py backend/miloco/tests/perception/collect && git commit -m "feat(camera): feed RTSP into perception"`

## Task 6: Add generic camera management and hot-apply APIs

**Files:**

- Create: `backend/miloco/src/miloco/camera/__init__.py`
- Create: `backend/miloco/src/miloco/camera/schema.py`
- Create: `backend/miloco/src/miloco/camera/service.py`
- Create: `backend/miloco/src/miloco/camera/router.py`
- Modify: `backend/miloco/src/miloco/manager.py`
- Modify: `backend/miloco/src/miloco/main.py`
- Test: `backend/miloco/tests/camera/test_camera_service.py`
- Test: `backend/miloco/tests/camera/test_camera_router.py`

**Interfaces:**

```python
class CameraSummary(BaseModel):
    id: str
    source_type: Literal["miot", "rtsp"]
    name: str
    room_name: str
    enabled: bool
    connected: bool
    video_codec: str | None
    audio_codec: str | None
    has_password: bool = False
    error_code: str | None = None
    error_message: str | None = None


class RtspSourceUpsert(BaseModel):
    name: str
    room_name: str = ""
    uri: str
    username: str = ""
    password: str = ""
    transport: Literal["tcp", "udp"] = "tcp"
    audio_enabled: bool = True
```

- [ ] Add failing service tests for aggregation, create-disabled, immutable generated ID, edit-with-blank-password preservation, explicit password replacement, test-without-save, enable-after-probe, enable failure without mutation, disable, delete, and atomic hot apply.
- [ ] Add failing router tests for all approved endpoints, auth enforcement consistent with existing admin endpoints, 404/409 behavior, stable error codes, and response redaction.
- [ ] Assert response bodies and captured logs contain neither stored password nor username-bearing URI.
- [ ] Run `cd backend && uv run pytest miloco/tests/camera -q` and confirm failure.
- [ ] Implement service writes as read-modify-write operations through `update_shared_config(camera={"rtsp_sources": ...})`, followed by `PerceptionService.sync_camera_sources()` only after persistence succeeds.
- [ ] Generate IDs with `f"rtsp:{uuid.uuid4()}"`; ignore any client-supplied ID.
- [ ] `POST /api/cameras/rtsp` must save `enabled=False`. `POST /api/cameras/{id}/enable` must probe first, persist enabled state second, hot-apply third; if hot apply fails, return an error and restore the persisted disabled state.
- [ ] Keep `/api/miot/...` unchanged. `GET /api/cameras` is an additive aggregated view.
- [ ] Wire `CameraService` through the manager and include the new router from `main.py` after auth/bootstrap dependencies exist.
- [ ] Run focused tests, existing admin/MIoT router tests, Ruff, and ty.
- [ ] Commit: `git add backend/miloco/src/miloco/camera backend/miloco/src/miloco/manager.py backend/miloco/src/miloco/main.py backend/miloco/tests/camera && git commit -m "feat(camera): add RTSP management API"`

## Task 7: Add CLI management without exposing credentials

**Files:**

- Create: `cli/src/miloco_cli/commands/camera.py`
- Modify: `cli/src/miloco_cli/main.py`
- Test: `cli/tests/test_camera_commands.py`

**CLI contract:**

```text
miloco-cli camera list
miloco-cli camera rtsp test --uri ... [--username ...] [--password-stdin]
miloco-cli camera rtsp add --name ... --room ... --uri ... [--username ...] [--password-stdin]
miloco-cli camera rtsp edit <id> [...]
miloco-cli camera enable <id>
miloco-cli camera disable <id>
miloco-cli camera delete <id> --yes
```

- [ ] Add failing Click runner tests for each command, HTTP method/path/body mapping, noninteractive delete protection, `--password-stdin`, and absence of password in stdout, stderr, debug log, and exception messages.
- [ ] Run `cd cli && uv run pytest tests/test_camera_commands.py -q` and confirm failure.
- [ ] Implement the command group using the existing API client/output conventions. Do not accept a plaintext `--password VALUE` option because it leaks through shell history/process listings.
- [ ] Preserve exit-code conventions: validation `1`, network `2`, backend business error `3`.
- [ ] Add the command group to `main.py` and update command discovery tests.
- [ ] Run focused CLI tests, then `cd cli && uv run pytest -q` and Ruff.
- [ ] Commit: `git add cli/src/miloco_cli/commands/camera.py cli/src/miloco_cli/main.py cli/tests/test_camera_commands.py cli/tests/test_commands.py && git commit -m "feat(cli): manage RTSP cameras"`

## Task 8: Prove end-to-end perception and document measured limits

**Files:**

- Create: `backend/miloco/tests/integration/test_rtsp_perception.py`
- Create: `scripts/rtsp-smoke.sh`
- Modify: `docs/2026-08-28-rtsp-responses-support_PROGRESS.md`
- Modify: `docs/superpowers/specs/2026-08-28-rtsp-responses-support-design.md`

- [ ] Add an integration test that injects the deterministic H.264 and H.265 PyAV fixture containers into `RtspSession`, runs through `RtspCameraSource -> CameraDeviceAdapter -> MultimodalCollector`, and asserts representative frames and optional 16 kHz audio reach `DeviceData`.
- [ ] Add a real-network smoke script that reads `MILOCO_RTSP_TEST_URL`, `MILOCO_RTSP_TEST_USERNAME`, and `MILOCO_RTSP_TEST_PASSWORD` from the environment, never echoes them, creates a temporary disabled source, tests/enables it, waits for a decoded frame, reports only codec/dimensions/timing, and deletes it in a trap.
- [ ] The script must refuse to run without an explicit test URL and must never modify a production camera. It is a local/lab acceptance tool.
- [ ] Run `cd backend && uv run pytest miloco/tests/config/test_rtsp_settings.py miloco/tests/perception/collect miloco/tests/camera miloco/tests/integration/test_rtsp_perception.py -q`.
- [ ] Run `cd cli && uv run pytest -q`, `cd backend && uv run task check`, and `cd backend && uv run task lint`.
- [ ] Run `./scripts/local-ci.sh --tests`. Fix only failures caused by this plan; report pre-existing or platform-dependent failures explicitly.
- [ ] If a lab RTSP source is available, run `./scripts/rtsp-smoke.sh` and record success/failure, codec, decode startup time, and reconnect behavior without secrets. If no source is available, record real-network E2E as `not_measured`; do not claim it passed from fake-container tests.
- [ ] Update the design status to `已批准；RTSP 感知基础已实施` only after all mandatory automated gates pass. Record CPU/fps as `not_measured` here because browser transcode is intentionally deferred.
- [ ] Commit: `git add backend/miloco/tests/integration/test_rtsp_perception.py scripts/rtsp-smoke.sh docs/2026-08-28-rtsp-responses-support_PROGRESS.md docs/superpowers/specs/2026-08-28-rtsp-responses-support-design.md && git commit -m "test(camera): verify RTSP perception foundation"`

## Completion Gate

- All existing MIoT camera tests pass with unchanged IDs.
- RTSP H.264 and H.265 frames enter the collector; optional audio becomes existing PCM input.
- Config and temp files are mode `0600`; APIs, CLI, logs, traces, and errors expose no credential.
- Disabled/offline save, test, enable, reconnect, disable, edit, and delete behaviors are covered.
- A single source failure is isolated.
- Real RTSP network E2E is either evidenced or explicitly `not_measured`.
- No live-view claim is made until the dependent live-view plan is complete.
