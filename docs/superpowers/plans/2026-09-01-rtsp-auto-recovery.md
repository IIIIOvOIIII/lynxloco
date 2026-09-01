# RTSP Auto-Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add bounded automatic recovery for terminal RTSP camera errors, retrying every 10 minutes for up to 12 hours and disabling only the failed RTSP source if it never recovers.

**Architecture:** Keep the feature in the backend camera runtime. `RtspCameraSource` owns terminal tombstones and decides when retry or auto-disable is due; `CameraDeviceAdapter` invokes that policy during normal periodic sync. Persistent auto-disable uses the existing shared RTSP config mutation path, so `/root/.openclaw/miloco` is preserved and only the affected RTSP source's `enabled` flag changes.

**Tech Stack:** Python 3.11+, asyncio, dataclasses, Pydantic settings models, pytest, existing Miloco installer/sync production path.

**Spec:** `docs/superpowers/specs/2026-09-01-rtsp-auto-recovery-design.md`

## Global Constraints

- Apply only to manually configured RTSP cameras in Miloco.
- Do not apply to MIoT / Mi Home cameras, Xiaomi account state, Home Assistant devices, Omni model configuration, or OpenClaw gateway configuration.
- MIoT / Mi Home camera auto-disable is explicitly out of scope; only RTSP source `enabled=false` may be written by this change.
- Retry interval is exactly 10 minutes in production code.
- Failure window is exactly 12 hours in production code.
- If retry succeeds, restore normal monitoring and clear the failure window.
- If the 12-hour window expires without recovery, set only that RTSP source's Miloco `enabled` flag to `false`.
- Do not edit RTSP URLs, usernames, passwords, transports, camera firmware, firewall rules, model settings, Home Assistant settings, or OpenClaw gateway settings.
- Do not print or store RTSP URLs with credentials, API keys, bearer tokens, camera frames, raw video packets, raw prompts, or raw model responses.
- Production deployment must target `miloco.esxi` through a Software CO with active PAM and must use the official installer/sync package flow, not the retired Docker deployment shape.
- Preserve `/root/.openclaw/miloco`, including Xiaomi account state, RTSP camera definitions, Omni model settings, Home Assistant settings, dashboard auth users, and service tokens.

---

## File Structure

- Modify `backend/miloco/src/miloco/perception/collect/rtsp_camera_source.py`
  - Add RTSP auto-recovery constants.
  - Add in-memory recovery-window state.
  - Add an async `advance_auto_recovery()` method that clears terminal tombstones when a retry is due or persistently disables expired RTSP sources through an injected mutator.
  - Keep terminal-state payloads credential-safe.
- Modify `backend/miloco/src/miloco/perception/collect/camera_adapter.py`
  - Invoke `advance_auto_recovery()` during periodic camera sync before normal discovery/reconnect.
  - Do not call it for externally supplied `all_devices` snapshots.
- Modify `backend/miloco/tests/perception/collect/test_rtsp_camera_source.py`
  - Add source-level RED/GREEN tests for 10-minute retry, 12-hour disable, same-window repeated failures, and config-change reset.
- Modify `backend/miloco/tests/perception/collect/test_camera_adapter.py`
  - Add integration-style RED/GREEN tests proving periodic adapter sync invokes RTSP recovery and does not affect MIoT sources.
- Modify `docs/2026-09-01-rtsp-camera-repair_PROGRESS.md`
  - Record implementation, verification, deployment, and production acceptance milestones.
- Create `docs/co/rtsp-auto-recovery-deploy-implementation.md`
  - Production CO implementation plan.
- Create `docs/co/rtsp-auto-recovery-deploy-rollback.md`
  - Production CO rollback plan.

---

### Task 1: RTSP Source Recovery Policy

**Files:**
- Modify: `backend/miloco/src/miloco/perception/collect/rtsp_camera_source.py`
- Test: `backend/miloco/tests/perception/collect/test_rtsp_camera_source.py`

**Interfaces:**
- Consumes: existing `RtspCameraSource.retain_pending_connection(did: str) -> bool`, `RtspCameraSource.request_retry(did: str) -> bool`, `RtspCameraSource.discover_devices(...) -> dict[str, PerceptionDevice]`, and `mutate_rtsp_sources(mutation)`.
- Produces:
  - `RTSP_AUTO_RECOVERY_RETRY_INTERVAL_MS: int = 10 * 60 * 1000`
  - `RTSP_AUTO_RECOVERY_WINDOW_MS: int = 12 * 60 * 60 * 1000`
  - `@dataclass(frozen=True) class RtspAutoRecoveryResult: success: bool; retry_dids: frozenset[str]; disabled_dids: frozenset[str]`
  - `async def RtspCameraSource.advance_auto_recovery(self, *, now_ms: int | None = None) -> RtspAutoRecoveryResult`

- [ ] **Step 1: Write the failing source retry-window test**

Append this test to `backend/miloco/tests/perception/collect/test_rtsp_camera_source.py`:

```python
@pytest.mark.asyncio
async def test_terminal_rtsp_is_retried_only_after_recovery_interval() -> None:
    clock_ms = 1_000_000
    configured = _source(101)
    current = [configured]
    source = RtspCameraSource(lambda: current, clock_ms=lambda: clock_ms)
    await source.connect_device(configured.id, _video_cb, _audio_cb)
    terminal = source.get_session(configured.id)
    assert isinstance(terminal, _RecordingSession)
    terminal.connected = False
    terminal.active = False
    terminal.terminal = True
    terminal.state_override = CameraSourceState(
        connected=False,
        video_codec="h264",
        error_code="authentication_failed",
        error_message="RTSP authentication failed",
    )

    assert source.retain_pending_connection(configured.id) is False
    await source.disconnect_device(configured.id)
    assert await source.discover_devices() == {}

    early = await source.advance_auto_recovery(now_ms=clock_ms + 599_999)
    assert early.retry_dids == frozenset()
    assert early.disabled_dids == frozenset()
    assert await source.discover_devices() == {}

    due = await source.advance_auto_recovery(now_ms=clock_ms + 600_000)
    assert due.success is True
    assert due.retry_dids == frozenset({configured.id})
    assert due.disabled_dids == frozenset()
    assert set(await source.discover_devices()) == {configured.id}
```

- [ ] **Step 2: Run the source retry-window test and verify RED**

Run:

```bash
uv run --package miloco pytest backend/miloco/tests/perception/collect/test_rtsp_camera_source.py::test_terminal_rtsp_is_retried_only_after_recovery_interval -q
```

Expected: FAIL because `RtspCameraSource.__init__` does not accept `clock_ms` and/or `advance_auto_recovery` is missing.

- [ ] **Step 3: Implement minimal recovery state and retry due logic**

In `rtsp_camera_source.py`, add imports:

```python
import time
from typing import Any

from miloco.utils.agent_config import mutate_rtsp_sources
```

Add constants, mutator aliases, and dataclasses near the existing dataclasses:

```python
RTSP_AUTO_RECOVERY_RETRY_INTERVAL_MS = 10 * 60 * 1000
RTSP_AUTO_RECOVERY_WINDOW_MS = 12 * 60 * 60 * 1000

SourcesMutation = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]
SourcesMutator = Callable[[SourcesMutation], dict[str, Any]]


def _monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


@dataclass(frozen=True)
class RtspAutoRecoveryResult:
    success: bool
    retry_dids: frozenset[str] = frozenset()
    disabled_dids: frozenset[str] = frozenset()


@dataclass
class _AutoRecoveryWindow:
    fingerprint: str
    first_failure_ms: int
    last_retry_ms: int
    attempt_count: int = 0
    last_error_code: str | None = None
```

Extend `RtspCameraSource.__init__`:

```python
def __init__(
    self,
    settings_loader: Callable[[], list[RtspSourceSettings]],
    *,
    sources_mutator: SourcesMutator | None = mutate_rtsp_sources,
    clock_ms: Callable[[], int] = _monotonic_ms,
    auto_retry_interval_ms: int = RTSP_AUTO_RECOVERY_RETRY_INTERVAL_MS,
    auto_disable_window_ms: int = RTSP_AUTO_RECOVERY_WINDOW_MS,
) -> None:
    self._settings_loader = settings_loader
    self._sources_mutator = sources_mutator
    self._clock_ms = clock_ms
    self._auto_retry_interval_ms = auto_retry_interval_ms
    self._auto_disable_window_ms = auto_disable_window_ms
    self._auto_recovery: dict[str, _AutoRecoveryWindow] = {}
    ...
```

When `retain_pending_connection()` records a terminal tombstone, also call:

```python
self._record_auto_recovery_window(entry.setting, tombstone.state)
```

Implement:

```python
def _record_auto_recovery_window(
    self, setting: RtspSourceSettings, state: CameraSourceState
) -> None:
    now_ms = self._clock_ms()
    fingerprint = self._connection_fingerprint(setting)
    existing = self._auto_recovery.get(setting.id)
    if existing is None or existing.fingerprint != fingerprint:
        self._auto_recovery[setting.id] = _AutoRecoveryWindow(
            fingerprint=fingerprint,
            first_failure_ms=now_ms,
            last_retry_ms=now_ms,
            last_error_code=state.error_code,
        )
        return
    existing.last_error_code = state.error_code
```

Implement `advance_auto_recovery()` with enough logic to pass the retry-window test:

```python
async def advance_auto_recovery(
    self, *, now_ms: int | None = None
) -> RtspAutoRecoveryResult:
    del now_ms  # remove once full implementation uses the parameter
    return RtspAutoRecoveryResult(success=True)
```

Then replace that stub with real logic in the same step:

```python
async def advance_auto_recovery(
    self, *, now_ms: int | None = None
) -> RtspAutoRecoveryResult:
    async with self._lifecycle_lock:
        current_ms = self._clock_ms() if now_ms is None else now_ms
        settings = self._load_settings()
        self._reconcile_terminal_tombstones(settings)
        self._clear_recovered_sessions_locked()
        retry_dids: set[str] = set()
        for did, tombstone in list(self._terminal_tombstones.items()):
            setting = settings.get(did)
            if setting is None or not setting.enabled:
                continue
            fingerprint = self._connection_fingerprint(setting)
            if tombstone.fingerprint != fingerprint:
                continue
            window = self._auto_recovery.get(did)
            if window is None or window.fingerprint != fingerprint:
                window = _AutoRecoveryWindow(
                    fingerprint=fingerprint,
                    first_failure_ms=current_ms,
                    last_retry_ms=current_ms,
                    last_error_code=tombstone.state.error_code,
                )
                self._auto_recovery[did] = window
            if current_ms - window.last_retry_ms < self._auto_retry_interval_ms:
                continue
            window.last_retry_ms = current_ms
            window.attempt_count += 1
            self._terminal_tombstones.pop(did, None)
            retry_dids.add(did)
        return RtspAutoRecoveryResult(success=True, retry_dids=frozenset(retry_dids))
```

Implement `_clear_recovered_sessions_locked()`:

```python
def _clear_recovered_sessions_locked(self) -> None:
    for did, entry in list(self._sessions.items()):
        try:
            state = entry.session.state()
        except Exception:
            continue
        if state.connected:
            self._auto_recovery.pop(did, None)
            self._terminal_tombstones.pop(did, None)
```

- [ ] **Step 4: Run the source retry-window test and verify GREEN**

Run:

```bash
uv run --package miloco pytest backend/miloco/tests/perception/collect/test_rtsp_camera_source.py::test_terminal_rtsp_is_retried_only_after_recovery_interval -q
```

Expected: PASS.

- [ ] **Step 5: Write failing source auto-disable and bounded-window tests**

Append:

```python
@pytest.mark.asyncio
async def test_terminal_rtsp_is_disabled_after_recovery_window_expires() -> None:
    clock_ms = 2_000_000
    configured = _source(102)
    current = [configured]
    mutation_calls = 0

    def mutate_sources(mutation):
        nonlocal current, mutation_calls
        mutation_calls += 1
        current = [
            RtspSourceSettings.model_validate(raw)
            for raw in mutation([item.model_dump() for item in current])
        ]
        return {}

    source = RtspCameraSource(
        lambda: current,
        sources_mutator=mutate_sources,
        clock_ms=lambda: clock_ms,
    )
    await source.connect_device(configured.id, _video_cb, _audio_cb)
    terminal = source.get_session(configured.id)
    assert isinstance(terminal, _RecordingSession)
    terminal.connected = False
    terminal.active = False
    terminal.terminal = True
    terminal.state_override = CameraSourceState(
        connected=False,
        video_codec="h264",
        error_code="unsupported_video_codec",
        error_message="RTSP video codec could not be decoded",
    )
    assert source.retain_pending_connection(configured.id) is False
    await source.disconnect_device(configured.id)

    expired = await source.advance_auto_recovery(
        now_ms=clock_ms + 12 * 60 * 60 * 1000
    )

    assert expired.success is True
    assert expired.retry_dids == frozenset()
    assert expired.disabled_dids == frozenset({configured.id})
    assert current == [configured.model_copy(update={"enabled": False})]
    assert mutation_calls == 1
    assert source.get_state(configured.id) == CameraSourceState(connected=False)
    assert await source.discover_devices() == {}


@pytest.mark.asyncio
async def test_repeated_terminal_failures_keep_original_recovery_deadline() -> None:
    clock_ms = 3_000_000
    configured = _source(103)
    current = [configured]
    disabled: list[str] = []

    def mutate_sources(mutation):
        nonlocal current
        current = [
            RtspSourceSettings.model_validate(raw)
            for raw in mutation([item.model_dump() for item in current])
        ]
        disabled.extend(item.id for item in current if not item.enabled)
        return {}

    source = RtspCameraSource(
        lambda: current,
        sources_mutator=mutate_sources,
        clock_ms=lambda: clock_ms,
    )
    await source.connect_device(configured.id, _video_cb, _audio_cb)
    first = source.get_session(configured.id)
    assert isinstance(first, _RecordingSession)
    first.connected = False
    first.active = False
    first.terminal = True
    first.state_override = CameraSourceState(
        connected=False,
        error_code="authentication_failed",
        error_message="RTSP authentication failed",
    )
    assert source.retain_pending_connection(configured.id) is False
    await source.disconnect_device(configured.id)

    retry = await source.advance_auto_recovery(now_ms=clock_ms + 600_000)
    assert retry.retry_dids == frozenset({configured.id})
    await source.connect_device(configured.id, _video_cb, _audio_cb)
    second = source.get_session(configured.id)
    assert isinstance(second, _RecordingSession)
    second.connected = False
    second.active = False
    second.terminal = True
    second.state_override = first.state_override
    assert source.retain_pending_connection(configured.id) is False
    await source.disconnect_device(configured.id)

    expired = await source.advance_auto_recovery(
        now_ms=clock_ms + 12 * 60 * 60 * 1000
    )
    assert expired.disabled_dids == frozenset({configured.id})
    assert disabled == [configured.id]
```

- [ ] **Step 6: Run the new source tests and verify RED**

Run:

```bash
uv run --package miloco pytest \
  backend/miloco/tests/perception/collect/test_rtsp_camera_source.py::test_terminal_rtsp_is_disabled_after_recovery_window_expires \
  backend/miloco/tests/perception/collect/test_rtsp_camera_source.py::test_repeated_terminal_failures_keep_original_recovery_deadline -q
```

Expected: FAIL because expired windows are not disabled yet.

- [ ] **Step 7: Implement persistent auto-disable**

In `advance_auto_recovery()`, check the 12-hour window before the retry-interval branch:

```python
if current_ms - window.first_failure_ms >= self._auto_disable_window_ms:
    if await self._disable_setting_locked(setting, fingerprint):
        self._terminal_tombstones.pop(did, None)
        self._auto_recovery.pop(did, None)
        disabled_dids.add(did)
    else:
        success = False
    continue
```

Track and return `success`, `retry_dids`, and `disabled_dids`.

Add `_disable_setting_locked()`:

```python
async def _disable_setting_locked(
    self, setting: RtspSourceSettings, fingerprint: str
) -> bool:
    if self._sources_mutator is None:
        return False
    changed = False

    def disable(raw_sources: list[dict[str, Any]]) -> list[dict[str, Any]]:
        nonlocal changed
        sources = [RtspSourceSettings.model_validate(source) for source in raw_sources]
        for index, current in enumerate(sources):
            if current.id != setting.id:
                continue
            if not current.enabled:
                changed = True
                break
            if self._connection_fingerprint(current) != fingerprint:
                changed = True
                break
            sources[index] = current.model_copy(update={"enabled": False})
            changed = True
            break
        return [source.model_dump() for source in sources]

    try:
        await asyncio.to_thread(self._sources_mutator, disable)
    except Exception as error:  # noqa: BLE001
        self._log_lifecycle_failure("auto-disable", setting.id, error)
        return False
    return changed
```

Update `_reconcile_terminal_tombstones()` so removed, disabled, or connection-changed settings also remove `_auto_recovery[did]`.

- [ ] **Step 8: Run source tests and verify GREEN**

Run:

```bash
uv run --package miloco pytest backend/miloco/tests/perception/collect/test_rtsp_camera_source.py -q
```

Expected: PASS.

- [ ] **Step 9: Commit Task 1**

Run:

```bash
git add backend/miloco/src/miloco/perception/collect/rtsp_camera_source.py \
  backend/miloco/tests/perception/collect/test_rtsp_camera_source.py
git commit -m "fix(rtsp): add bounded terminal recovery"
```

---

### Task 2: Periodic Adapter Integration

**Files:**
- Modify: `backend/miloco/src/miloco/perception/collect/camera_adapter.py`
- Test: `backend/miloco/tests/perception/collect/test_camera_adapter.py`

**Interfaces:**
- Consumes: `RtspCameraSource.advance_auto_recovery(now_ms: int | None = None) -> RtspAutoRecoveryResult`
- Produces: periodic `CameraDeviceAdapter.sync_devices(all_devices=None)` invokes RTSP recovery before normal discovery/reconnect; snapshot-based `sync_devices(all_devices=...)` does not mutate RTSP recovery state.

- [ ] **Step 1: Write failing adapter auto-retry integration test**

Append:

```python
@pytest.mark.asyncio
async def test_periodic_sync_advances_rtsp_auto_recovery_before_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "miloco.perception.collect.rtsp_camera_source.RtspSession",
        _AdapterRtspSession,
    )
    clock_values = iter([1_000_000, 1_600_000, 1_600_001, 1_600_002])
    monkeypatch.setattr(
        "miloco.perception.collect.camera_adapter._monotonic_ms",
        lambda: next(clock_values),
    )
    camera_id = "rtsp:00000000-0000-0000-0000-000000000201"
    settings = [
        RtspSourceSettings(
            id=camera_id,
            name="auto retry",
            uri="rtsp://auto-retry.example/stream",
            enabled=True,
        )
    ]
    rtsp = RtspCameraSource(lambda: settings, clock_ms=lambda: 1_000_000)
    adapter = CameraDeviceAdapter(sources=[rtsp])
    await adapter.sync_devices()
    terminal = rtsp.get_session(camera_id)
    assert isinstance(terminal, _AdapterRtspSession)
    terminal.connected = False
    terminal.active = False
    terminal.terminal = True
    terminal.state_override = CameraSourceState(
        connected=False,
        error_code="authentication_failed",
        error_message="RTSP authentication failed",
    )
    await adapter.sync_devices()
    assert rtsp.get_session(camera_id) is None
    assert camera_id not in adapter.get_connected_devices()

    await adapter.sync_devices()

    assert rtsp.get_session(camera_id) is not None
    assert camera_id in adapter.get_connected_devices()
```

- [ ] **Step 2: Run adapter auto-retry test and verify RED**

Run:

```bash
uv run --package miloco pytest backend/miloco/tests/perception/collect/test_camera_adapter.py::test_periodic_sync_advances_rtsp_auto_recovery_before_discovery -q
```

Expected: FAIL because `CameraDeviceAdapter` does not call `advance_auto_recovery()`.

- [ ] **Step 3: Implement periodic recovery hook**

In `camera_adapter.py`, add a private helper:

```python
async def _advance_auto_recovery(self, now_ms: int) -> None:
    for camera_source in self._sources:
        advance = getattr(camera_source, "advance_auto_recovery", None)
        if advance is None:
            continue
        try:
            result = advance(now_ms=now_ms)
            if inspect.isawaitable(result):
                result = await result
            if getattr(result, "success", True) is not True:
                logger.warning(
                    "RTSP auto recovery reported partial failure for source %s",
                    camera_source.source_type,
                )
        except Exception as error:  # noqa: BLE001
            logger.warning("RTSP auto recovery failed: %s", type(error).__name__)
```

Call this from `_sync_devices_unlocked()` immediately after `_prune_inactive_pending_devices()` and only when `all_devices is None`:

```python
if all_devices is None:
    await self._advance_auto_recovery(_monotonic_ms())
```

- [ ] **Step 4: Run adapter auto-retry test and verify GREEN**

Run:

```bash
uv run --package miloco pytest backend/miloco/tests/perception/collect/test_camera_adapter.py::test_periodic_sync_advances_rtsp_auto_recovery_before_discovery -q
```

Expected: PASS.

- [ ] **Step 5: Write failing adapter auto-disable/MiOT isolation test**

Append:

```python
@pytest.mark.asyncio
async def test_rtsp_auto_disable_does_not_disable_miot_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "miloco.perception.collect.rtsp_camera_source.RtspSession",
        _AdapterRtspSession,
    )
    camera_id = "rtsp:00000000-0000-0000-0000-000000000202"
    configured = RtspSourceSettings(
        id=camera_id,
        name="expired rtsp",
        uri="rtsp://expired.example/stream",
        enabled=True,
    )
    settings = [configured]

    def mutate_sources(mutation):
        nonlocal settings
        settings = [
            RtspSourceSettings.model_validate(raw)
            for raw in mutation([item.model_dump() for item in settings])
        ]
        return {}

    clock_ms = 5_000_000
    rtsp = RtspCameraSource(
        lambda: settings,
        sources_mutator=mutate_sources,
        clock_ms=lambda: clock_ms,
    )
    miot = _FrameProducingMiotSource()
    adapter = CameraDeviceAdapter(sources=[miot, rtsp])
    await adapter.sync_devices()
    terminal = rtsp.get_session(camera_id)
    assert isinstance(terminal, _AdapterRtspSession)
    terminal.connected = False
    terminal.active = False
    terminal.terminal = True
    terminal.state_override = CameraSourceState(
        connected=False,
        error_code="unsupported_video_codec",
        error_message="RTSP video codec could not be decoded",
    )
    await adapter.sync_devices()
    assert "miot-stable-did" in adapter.get_connected_devices()

    clock_ms += 12 * 60 * 60 * 1000
    await adapter.sync_devices()

    assert settings == [configured.model_copy(update={"enabled": False})]
    assert "miot-stable-did" in adapter.get_connected_devices()
    assert camera_id not in adapter.get_connected_devices()
```

- [ ] **Step 6: Run adapter auto-disable test and verify RED or already GREEN**

Run:

```bash
uv run --package miloco pytest backend/miloco/tests/perception/collect/test_camera_adapter.py::test_rtsp_auto_disable_does_not_disable_miot_sources -q
```

Expected: PASS if Task 1 already implemented auto-disable and Step 3 invokes recovery. If it fails, fix only the adapter integration branch, not the RTSP source policy.

- [ ] **Step 7: Run focused adapter/source regression suite**

Run:

```bash
uv run --package miloco pytest \
  backend/miloco/tests/perception/collect/test_rtsp_camera_source.py \
  backend/miloco/tests/perception/collect/test_camera_adapter.py \
  backend/miloco/tests/camera/test_camera_service.py \
  backend/miloco/tests/camera/test_camera_router.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit Task 2**

Run:

```bash
git add backend/miloco/src/miloco/perception/collect/camera_adapter.py \
  backend/miloco/tests/perception/collect/test_camera_adapter.py
git commit -m "fix(camera): advance rtsp auto recovery during sync"
```

---

### Task 3: Release Verification, Production CO, and Deployment

**Files:**
- Modify: `docs/2026-09-01-rtsp-camera-repair_PROGRESS.md`
- Create: `docs/co/rtsp-auto-recovery-deploy-implementation.md`
- Create: `docs/co/rtsp-auto-recovery-deploy-rollback.md`

**Interfaces:**
- Consumes: committed source SHA from Tasks 1-2.
- Produces: pushed `main`, approved production CO, production deployment on `miloco.esxi`, closed CO, and sanitized acceptance evidence.

- [ ] **Step 1: Run final local gates**

Run:

```bash
uv run --package miloco pytest \
  backend/miloco/tests/perception/collect/test_rtsp_camera_source.py \
  backend/miloco/tests/perception/collect/test_camera_adapter.py \
  backend/miloco/tests/camera/test_camera_service.py \
  backend/miloco/tests/camera/test_camera_router.py -q
uv run --package miloco ruff check \
  backend/miloco/src/miloco/perception/collect/rtsp_camera_source.py \
  backend/miloco/src/miloco/perception/collect/camera_adapter.py \
  backend/miloco/tests/perception/collect/test_rtsp_camera_source.py \
  backend/miloco/tests/perception/collect/test_camera_adapter.py
git diff --check
```

Expected: PASS.

- [ ] **Step 2: Update progress doc**

Append a milestone to `docs/2026-09-01-rtsp-camera-repair_PROGRESS.md` recording the local implementation result, focused tests, and commit SHA. Do not include secrets or camera image content.

- [ ] **Step 3: Commit local verification/progress docs**

Run:

```bash
git add docs/2026-09-01-rtsp-camera-repair_PROGRESS.md
git commit -m "docs: record rtsp auto recovery verification"
```

- [ ] **Step 4: Merge implementation worktree back to main and push**

If executing from an implementation worktree branch:

```bash
git status --short
git checkout main
git merge --ff-only <implementation-branch>
git push origin main
```

If already on an approved implementation branch that is `main`, only push after verifying the branch is clean except unrelated pre-existing ignored/untracked files.

- [ ] **Step 5: Build exact source artifacts for official VM deployment**

Use the existing Miloco build/packaging flow for the official installer/sync path. The expected flow is:

```bash
./scripts/build-release.sh
```

If this script does not exist or the repo's current packaging command differs, inspect `scripts/` and prior `docs/co/*deploy*.md` files, then use the same official installer/sync packaging path already used for `miloco.esxi`. Do not use the retired Docker deployment.

- [ ] **Step 6: Draft production CO files**

Create `docs/co/rtsp-auto-recovery-deploy-implementation.md` with:

```markdown
# Deploy Miloco RTSP auto-recovery to miloco.esxi

## Scope

Deploy the exact implementation commit SHA recorded after local verification to the existing official Miloco installation on `miloco.esxi`.

## Pre-checks

1. Confirm CO is in `Implement` and PAM is active for `miloco.esxi/root`.
2. Confirm local source SHA is the recorded implementation SHA and the worktree has no uncommitted tracked source changes.
3. Confirm Miloco and OpenClaw health before deployment.
4. Confirm existing `/root/.openclaw/miloco` configuration is present and create a CO-specific backup before installing packages.

## Execution

1. Build local Miloco backend, CLI, MIoT, web static assets, and OpenClaw plugin artifacts from the recorded implementation SHA.
2. Upload only release artifacts and deployment scripts to a CO-specific staging directory on `miloco.esxi`.
3. Install using the official installer/sync package path.
4. Restart only the existing Miloco/OpenClaw managed services required by the installer/sync flow.
5. Do not edit RTSP URLs, usernames, passwords, Xiaomi account state, Omni model configuration, Home Assistant configuration, dashboard auth users, service tokens, host firewall, or camera firmware.

## Verification

1. Verify local and LAN Miloco `/health` return OK.
2. Verify OpenClaw gateway health returns OK and `miloco-openclaw-plugin` is loaded.
3. Verify installed package/plugin versions correspond to the recorded implementation SHA.
4. Verify sanitized camera API output still includes existing RTSP sources.
5. Verify installed source contains `RTSP_AUTO_RECOVERY_RETRY_INTERVAL_MS` and `RTSP_AUTO_RECOVERY_WINDOW_MS`.
6. If any RTSP source is in terminal state at verification time, confirm auto-recovery is scheduled or due without printing RTSP URLs, credentials, tokens, or camera frames.
```

Create `docs/co/rtsp-auto-recovery-deploy-rollback.md` with:

```markdown
# Rollback Miloco RTSP auto-recovery deployment

Rollback triggers:

1. Miloco health fails and does not recover after one service restart.
2. OpenClaw gateway/plugin health fails and does not recover after one restart.
3. Installed package versions do not match the approved source artifacts.
4. Existing Miloco configuration is unexpectedly changed outside the package install.

Rollback plan:

1. Restore the prior Miloco package set from the pre-change backup or rebuild/reinstall the exact previously deployed source SHA recorded during pre-checks.
2. Preserve `/root/.openclaw/miloco` unless evidence proves this deployment corrupted it.
3. Restart only existing Miloco/OpenClaw managed services.
4. Re-run Miloco health, OpenClaw health, plugin loaded, and sanitized camera-summary checks.
5. Stop and request direction if rollback would require RTSP credentials or broader host cleanup.
```

- [ ] **Step 7: Create and poll CO**

Run:

```bash
python3 /Users/nicholasliao/clawd/skills/itsm-co/scripts/itsm_co.py build-payload \
  --description "Deploy Miloco RTSP auto-recovery to miloco.esxi" \
  --service "Miloco" \
  --category Software \
  --window-hours 4 \
  --host miloco.esxi \
  --target-user root \
  --change-items "miloco.esxi: deploy exact Miloco RTSP auto-recovery source commit recorded after local verification; preserve existing /root/.openclaw/miloco configuration and secrets" \
  --implementation-file docs/co/rtsp-auto-recovery-deploy-implementation.md \
  --rollback-file docs/co/rtsp-auto-recovery-deploy-rollback.md \
  --output docs/co/rtsp-auto-recovery-deploy-payload.json
python3 /Users/nicholasliao/clawd/skills/itsm-co/scripts/itsm_co.py create \
  --payload-file docs/co/rtsp-auto-recovery-deploy-payload.json
MILOCO_RTSP_AUTO_RECOVERY_CO="the change id returned by the create step"
python3 /Users/nicholasliao/clawd/skills/itsm-co/scripts/itsm_co.py poll "$MILOCO_RTSP_AUTO_RECOVERY_CO" --timeout-seconds 600 --interval-seconds 5
```

Expected: CO reaches `state=Implement` and `pam_status=active`, or stops for Lynx approval. If Lynx approval is required, stop and ask the user to approve.

- [ ] **Step 8: Deploy to production after CO approval**

Run the existing official installer/sync deployment flow used by recent Miloco production changes. Do not run Docker deploy commands. The remote SSH key is:

```bash
ssh -i /Users/nicholasliao/.ssh/id_co_openclaw -o BatchMode=yes -o IdentitiesOnly=yes root@miloco.esxi
```

Use command output only for package versions, health status, plugin status, sanitized camera status, and artifact hashes. Do not print RTSP URLs, credentials, service token, API key, raw frame data, prompt, or model response.

- [ ] **Step 9: Production acceptance**

Verify:

```bash
curl -fsS http://miloco.esxi:1810/health
curl -fsS http://miloco.esxi:18789/health
```

On-host, use the local service token only inside a process to query `/api/cameras` and print only sanitized fields:

- camera name;
- source type;
- enabled;
- connected;
- video codec;
- audio codec;
- error code/message;
- no URI, username, password, token, or frame.

Also verify the installed Python package contains the auto-recovery constants by importing or inspecting the installed module path.

- [ ] **Step 10: Close CO and update memories**

Close the CO with actual outcome. Update:

- `docs/2026-09-01-rtsp-camera-repair_PROGRESS.md`
- `/Users/nicholasliao/clawd/memory/2026-09-01.md`
- `/Users/nicholasliao/clawd/MEMORY.md`

Record the CO number, source SHA, deployed package/plugin versions, verification result, and root-cause lesson. Do not record secrets.
