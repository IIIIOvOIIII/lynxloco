# RTSP Memory Bounds and Transcoder Coalescing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep two configured RTSP cameras stable inside Miloco's existing 3 GiB container limit by sampling decoded frames before perception retention, enforcing per-source frame and byte bounds, and coalescing transcoder work to one active plus one latest pending frame.

**Architecture:** `CameraDeviceAdapter` applies host-monotonic RTSP-only admission and opts RTSP buffers into an otherwise backward-compatible retention policy. `MultiTrackSyncBuffer` owns exact ndarray-byte accounting and newest-biased eviction across active and drained windows. `LiveStreamHub` coalesces decoded-frame callbacks before `SharedH264Transcoder`, while the transcoder admits into its bounded queue before copying and keeps bounded timestamp history. H.264 packet passthrough, MIoT collection, provider behavior, UI configuration, and deployment-controller semantics remain unchanged.

**Tech Stack:** Python 3.11+, asyncio, NumPy, PyAV/FFmpeg, pytest/pytest-asyncio, Ruff, ty, React/Vitest regression gates, Bash immutable-release controller, Docker Compose, ITSM CO/PAM.

**Spec:** `docs/superpowers/specs/2026-08-30-rtsp-memory-bounds-design.md`

## Global Constraints

- Treat `37e1d58c82814448c7a3381c256e3f4aca6b1b71` as the approved-spec/runtime-diff baseline. Begin implementation from the clean plan-approval HEAD on `feature/rtsp-responses-support`, which must descend directly from that baseline through documentation-only commits.
- Production Miloco on `docker.esxi` is intentionally stopped after `CHG260830002`; no production access or mutation occurs until Task 8 opens a new exact-SHA CO and obtains active PAM.
- Keep the production container limit exactly `3072m`; do not use the expanded host memory as a substitute for bounded application behavior.
- Apply perception sampling and retention bounds only when the owning source type is exactly `rtsp`. MIoT callbacks, DIDs, buffering, controls, and lifecycle remain unchanged.
- Keep compatible H.264 packet passthrough and browser playback at source rate. The perception sampling clock must never throttle the browser packet branch.
- Use host monotonic time for RTSP perception admission. Camera PTS is metadata only and cannot bypass or suppress the admission bound.
- Each RTSP buffer must retain at most `ceil(target_fps * window_size_seconds) + 1` decoded video frames per window and at most `128 * 1024 * 1024` video-payload bytes across active and drained windows.
- Evict oldest video first and retain the newest frame whenever it individually fits. Never evict audio merely to satisfy the video budget.
- Each transcoding feed may retain only one active push and one latest pending frame. No per-frame task set, list, or unbounded callback backlog is permitted.
- Do not change RTSP probing, authentication, reconnect policy, source configuration, provider configuration, OpenAI Responses request shape, UI schema, deployment retention behavior, or ONNX Runtime ownership.
- Do not add automatic retention deletion or same-SHA rebuilding. Preserve the `3d4`, `d969`, and `8b211` releases and their receipts/markers exactly.
- Never run the mutating `task lint` command. Use read-only Ruff, format-check, ty, tests, shell syntax, diff, and privacy gates.
- Do not expose RTSP URLs, camera credentials, API keys, Authorization headers, frames, screenshots, base64 images, provider bodies, or inference content in commands, logs, evidence, commits, or chat.
- Keep fixture acceptance, synthetic high-rate tests, lab validation, and real production RTSP/provider acceptance as distinct evidence levels.
- Use no more than two recovery attempts per failure class. Before a third attempt, stop, record the root cause and exact blocker, and ask the user for direction.
- Implement with strict RED then GREEN. Each task gets an implementation worker, a specification-compliance reviewer, and a code-quality reviewer under `superpowers:subagent-driven-development`; reviewers do not share implementation ownership.
- Preserve unrelated worktree changes. Stage only the paths named by the active task and commit every completed implementation task separately.

---

## Task 1: Add an optional frame-and-byte retention policy to `MultiTrackSyncBuffer`

**Files:**

- Modify: `backend/miloco/src/miloco/perception/collect/stream_buffer.py`
- Create: `backend/miloco/tests/perception/collect/test_stream_buffer_retention.py`
- Regression test: `backend/miloco/tests/perception/test_stream_buffer_overflow.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class TrackRetentionPolicy:
    track: str
    max_items_per_window: int
    max_payload_bytes: int
    payload_size: Callable[[object], int]


@dataclass
class StreamFragment(Generic[T]):
    data: T
    stream_ts: int
    wall_ms: int
    retained_payload_bytes: int = field(default=0, repr=False, compare=False)


class MultiTrackSyncBuffer:
    def __init__(
        self,
        track_names: list[str],
        window_ms: int = 3000,
        max_windows: int = 5,
        on_window_ready: Callable[[], None] | None = None,
        window_settle_ms: int = 500,
        buffer_full_action: str = "keep",
        retention_policy: TrackRetentionPolicy | None = None,
    ) -> None: ...

    @property
    def retained_payload_bytes(self) -> int: ...

    @property
    def peak_retained_payload_bytes(self) -> int: ...

    @property
    def retention_dropped_items(self) -> int: ...

    @property
    def retention_last_action(self) -> Literal[
        "frame_limit", "byte_limit", "oversized", "invalid_payload"
    ] | None: ...
```

The constructor remains behaviorally identical when `retention_policy` is absent. All counters and window mutations remain protected by the existing `threading.Lock`.

- [ ] **Step 1: Write RED validation and compatibility tests**

  Create `test_stream_buffer_retention.py` with tests that reject a policy whose track is not registered or whose item/byte limits are non-positive. Add incoming-payload tests where the size callback raises or returns a negative/non-integer value: `put()` must return normally, retain neither payload nor window, set `retention_last_action == "invalid_payload"`, increment the bounded aggregate drop counter, and emit at most one fixed diagnostic per buffer lifetime without the payload representation. Add a no-policy characterization test that inserts more than the future item and byte limits and proves the existing generic behavior is unchanged.

  Use a deterministic payload wrapper so the buffer test does not depend on camera schema:

  ```python
  @dataclass(frozen=True)
  class _VideoPayload:
      frame: np.ndarray
      sequence: int


  def _policy(*, frames: int = 13, bytes_: int = 128 * 1024 * 1024):
      return TrackRetentionPolicy(
          track="decoded_video",
          max_items_per_window=frames,
          max_payload_bytes=bytes_,
          payload_size=lambda item: item.frame.nbytes,
      )
  ```

- [ ] **Step 2: Write RED per-window newest-retention tests**

  Insert 30 synthetic video payloads into one logical four-second window with `max_items_per_window=13`. Assert:

  ```python
  assert [fragment.data.sequence for fragment in video] == list(range(17, 30))
  assert len(video) == 13
  assert buffer.retention_dropped_items == 17
  assert buffer.retention_last_action == "frame_limit"
  ```

  Add interleaved audio fragments and assert their sequence and count are unchanged after video eviction.

- [ ] **Step 3: Write RED byte-budget and lifecycle-accounting tests**

  Cover all approved accounting transitions with small arrays and a small byte budget so tests do not allocate hundreds of MiB:

  - insertion increments `retained_payload_bytes` by exact `ndarray.nbytes`;
  - an over-budget insertion evicts the oldest video across drained then active windows while retaining the newest fitting frame;
  - a single payload larger than the entire budget is rejected without inserting a window or evicting audio;
  - skipping a partial first window subtracts its payload;
  - ready-window `drop` and `clear` backpressure subtracts removed payload;
  - `drain_ready()` moves windows without double-counting, then trimming `_drained` subtracts the removed oldest windows;
  - `clear()` returns current retained bytes to zero while preserving the historical peak;
  - `_windows`, `_ready_queue`, `_ready_keys`, `_drained`, and `tracks_seen` remain consistent after the last fragment of a track/window is removed.

  Add two independent buffers and assert their counters never share state.

- [ ] **Step 4: Run focused tests and confirm RED**

  ```bash
  (cd backend && uv run pytest -q \
    miloco/tests/perception/collect/test_stream_buffer_retention.py \
    miloco/tests/perception/test_stream_buffer_overflow.py)
  ```

  Expected: the new test module fails to import `TrackRetentionPolicy`; the existing overflow suite remains green.

- [ ] **Step 5: Add the policy and centralized accounting helpers**

  Add `TrackRetentionPolicy`, constructor validation, and internal counters. Centralize every removal through helpers instead of scattering integer adjustments:

  ```python
  def _payload_size_locked(self, track: str, data: object) -> int | None:
      policy = self._retention_policy
      if policy is None or track != policy.track:
          return 0
      try:
          size = policy.payload_size(data)
      except Exception:
          self._record_retention_drop_locked("invalid_payload")
          self._warn_invalid_payload_once_locked()
          return None
      if not isinstance(size, int) or isinstance(size, bool) or size < 0:
          self._record_retention_drop_locked("invalid_payload")
          self._warn_invalid_payload_once_locked()
          return None
      return size

  def _discard_fragment_locked(
      self,
      window: _TimeWindow,
      track: str,
      index: int,
      *,
      action: Literal[
          "frame_limit", "byte_limit", "oversized", "invalid_payload"
      ],
  ) -> None:
      fragment = window.tracks[track].pop(index)
      self._retained_payload_bytes -= fragment.retained_payload_bytes
      self._retention_dropped_items += 1
      self._retention_last_action = action
      if not window.tracks[track]:
          window.tracks.pop(track)
          window.tracks_seen.discard(track)
  ```

  Store the measured byte count once on `StreamFragment.retained_payload_bytes`; removal must never call the user-supplied size callback again. Add `_record_retention_drop_locked(action)`, `_warn_invalid_payload_once_locked()`, `_discard_window_payload_locked(window)`, and `_remove_window_locked(key)` helpers. `_warn_invalid_payload_once_locked()` uses one boolean flag to emit the same content-free warning at most once during the buffer lifetime. Use the accounting helpers in partial-window expiry, ready-queue drop, clear overflow, drained trimming, and `clear()`. Moving a window from `_windows` to `_drained` must not change the byte counter.

- [ ] **Step 6: Enforce oversized, frame-count, and global-byte bounds in insertion order**

  In `put()`:

  1. Calculate policy payload size once before creating/mutating a window; an invalid measurement is a safe rejected fragment.
  2. If one item exceeds `max_payload_bytes`, increment the policy drop counter/action and return normally without invoking the ready callback.
  3. Append the new fragment and update current/peak bytes.
  4. While the target window exceeds `max_items_per_window`, remove its oldest policy-track fragment.
  5. While total bytes exceed the global budget, scan policy-track fragments by `(window_start_ms, wall_ms, stream_ts)` across `_drained` and `_windows`, remove the oldest, and clean empty structures/ready keys.
  6. Never remove a non-policy track to meet either bound.

  Keep existing window-overflow counters and `consume_drop_stats()` semantics backward compatible; do not write fragment counts into the existing `dropped_windows` field. Policy drops use the new retention counters/action properties and must never be relabelled as a connectivity failure.

- [ ] **Step 7: Run focused GREEN and a deterministic 30-second logical load test**

  Extend the new suite with 25 FPS/720p-shaped and 30 FPS/1080p-shaped arrays reused by reference over 30 seconds of logical timestamps. Do not allocate a new full-resolution array for every logical frame. Assert per-window count `<=13`, per-buffer bytes `<=128 MiB`, two-buffer aggregate `<=256 MiB`, newest sequence retained, audio preserved, and exact zero after clear.

  ```bash
  (cd backend && uv run pytest -q \
    miloco/tests/perception/collect/test_stream_buffer_retention.py \
    miloco/tests/perception/test_stream_buffer_overflow.py)
  ```

  Expected: all pass with no warning or exception from normal cap eviction.

- [ ] **Step 8: Run static checks and commit Task 1**

  ```bash
  (cd backend && uv run ruff check \
    miloco/src/miloco/perception/collect/stream_buffer.py \
    miloco/tests/perception/collect/test_stream_buffer_retention.py \
    miloco/tests/perception/test_stream_buffer_overflow.py)
  (cd backend && uv run ruff format --check \
    miloco/src/miloco/perception/collect/stream_buffer.py \
    miloco/tests/perception/collect/test_stream_buffer_retention.py)
  git diff --check
  git add backend/miloco/src/miloco/perception/collect/stream_buffer.py \
    backend/miloco/tests/perception/collect/test_stream_buffer_retention.py \
    backend/miloco/tests/perception/test_stream_buffer_overflow.py
  git commit -m "fix(perception): bound RTSP frame retention"
  ```

---

## Task 2: Sample RTSP perception frames before buffering and wire the retention policy

**Files:**

- Modify: `backend/miloco/src/miloco/perception/collect/camera_adapter.py`
- Modify/Test: `backend/miloco/tests/perception/collect/test_camera_adapter.py`
- Create: `backend/miloco/tests/perception/collect/test_rtsp_frame_admission.py`
- Regression test: `backend/miloco/tests/perception/collect/test_rtsp_camera_source.py`

**Interfaces:**

```python
_RTSP_VIDEO_BUFFER_BYTES = 128 * 1024 * 1024


def _perception_input_fps() -> int:
    raw = get_settings().perception.engine.get("input", {}).get("fps", 3)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


@dataclass
class _CameraDeviceState:
    did: str
    sync_buffer: MultiTrackSyncBuffer
    epoch_delta: int | None = None
    rtsp_target_fps: int | None = None
    last_rtsp_video_admit_ms: int | None = None
    rtsp_admitted_frames: int = 0
    rtsp_dropped_frames: int = 0
```

`CameraDeviceAdapter.__init__` gains one keyword-only injectable dependency for deterministic tests:

```python
perception_fps_provider: Callable[[], int] = _perception_input_fps
```

Existing constructors need no change.

- [ ] **Step 1: Write RED RTSP admission tests with a controlled monotonic clock**

  Create a small fake source using the existing camera-source test style. Construct an adapter with `perception_fps_provider=lambda: 3`, connect one RTSP DID, retrieve its callback, and monkeypatch `_monotonic_ms` with a deterministic sequence representing 25 FPS for one second.

  Assert:

  ```python
  assert admitted_wall_times == [0, 360, 720]
  assert state.rtsp_admitted_frames == 3
  assert state.rtsp_dropped_frames == 22
  ```

  Use interval semantics `now_ms - last_admit_ms >= 1000 / target_fps`; do not derive admission from `ts`/PTS.

- [ ] **Step 2: Add RED clock/lifecycle/source-isolation tests**

  Add tests for:

  - configured 1 FPS and 10 FPS;
  - invalid `0`, negative, non-numeric, and missing values fail closed to 1 FPS;
  - repeated, reversed, jumped, and absent-equivalent camera PTS values do not alter host-monotonic admission;
  - two RTSP DIDs keep independent last-admit timestamps and counters;
  - an MIoT DID admits every callback at 25/30 FPS;
  - disconnect then reconnect admits the first new frame immediately;
  - failed connect cleanup, inactive pending-device pruning, explicit source replacement, and shutdown leave no stale ndarray reference or admission deadline;
  - `clear_buffers()` releases every buffered ndarray and resets byte accounting without disconnecting or resetting the still-active source's monotonic admission clock.

  For `clear_buffers()`, retain the connected device but clear byte/accounting state; the next RTSP frame follows the current admission clock unless the source was replaced. For disconnect/prune/replacement/shutdown, the entire `_CameraDeviceState` must disappear.

- [ ] **Step 3: Write RED RTSP-policy wiring tests**

  Connect one RTSP source and one MIoT source with the same collect settings. Assert only the RTSP buffer has a retention policy and that its configured values are:

  ```python
  assert policy.track == "decoded_video"
  assert policy.max_items_per_window == math.ceil(fps * collect.window_size) + 1
  assert policy.max_payload_bytes == 128 * 1024 * 1024
  ```

  Feed `DecodedVideoFrame` payloads backed by NumPy arrays and assert size comes from `decoded.frame.nbytes`, not width/height metadata. The MIoT buffer must retain legacy behavior.

- [ ] **Step 4: Run focused tests and confirm RED**

  ```bash
  (cd backend && uv run pytest -q \
    miloco/tests/perception/collect/test_rtsp_frame_admission.py \
    miloco/tests/perception/collect/test_camera_adapter.py)
  ```

  Expected: new admission/policy assertions fail because every RTSP frame is currently buffered and the adapter does not pass a retention policy.

- [ ] **Step 5: Add the injected FPS boundary and RTSP state at connection time**

  Normalize the provider result once for each new device state. For source type exactly `rtsp`, construct:

  ```python
  target_fps = max(1, self._perception_fps_provider())
  retention_policy = TrackRetentionPolicy(
      track="decoded_video",
      max_items_per_window=math.ceil(target_fps * collect_cfg.window_size) + 1,
      max_payload_bytes=_RTSP_VIDEO_BUFFER_BYTES,
      payload_size=lambda item: item.frame.nbytes,
  )
  ```

  Pass `retention_policy=None` for every non-RTSP source. Store `rtsp_target_fps` only on RTSP states. Do not add policy state to `RtspSession`, `RtspCameraSource`, or the Web API.

- [ ] **Step 6: Reject early frames before creating `DecodedVideoFrame`**

  Add a pure state helper:

  ```python
  @staticmethod
  def _admit_rtsp_video(state: _CameraDeviceState, now_ms: int) -> bool:
      target_fps = state.rtsp_target_fps
      if target_fps is None:
          return True
      last = state.last_rtsp_video_admit_ms
      if last is not None and now_ms - last < 1000 / target_fps:
          state.rtsp_dropped_frames += 1
          return False
      state.last_rtsp_video_admit_ms = now_ms
      state.rtsp_admitted_frames += 1
      return True
  ```

  In `_make_decoded_video_callback`, obtain `wall_ms` from `_calibrate`, call the helper, and return normally before constructing `DecodedVideoFrame` or calling `sync_buffer.put()` when rejected. Call `h.skip_rolling()` for perception-admission drops so source decode monitoring is not misrepresented as perception throughput. Do not raise, log per frame, change source state, or schedule reconnect.

- [ ] **Step 7: Prove lifecycle cleanup through existing real adapter paths**

  Extend existing RTSP replacement/prune/shutdown tests rather than calling private dictionaries directly where a public path exists. Use weak references to one admitted array in a removed state, call the normal lifecycle operation plus `gc.collect()`, and assert the array can be released. Verify reconnect receives a new `_CameraDeviceState` and admits its first frame immediately.

- [ ] **Step 8: Run focused GREEN and adjacent RTSP regressions**

  ```bash
  (cd backend && uv run pytest -q \
    miloco/tests/perception/collect/test_rtsp_frame_admission.py \
    miloco/tests/perception/collect/test_camera_adapter.py \
    miloco/tests/perception/collect/test_stream_buffer_retention.py \
    miloco/tests/perception/collect/test_rtsp_camera_source.py \
    miloco/tests/perception/collect/test_rtsp_session.py)
  ```

  Expected: admission, 128 MiB policy, lifecycle reset, MIoT non-regression, RTSP session, and source tests all pass.

- [ ] **Step 9: Run static checks and commit Task 2**

  ```bash
  (cd backend && uv run ruff check \
    miloco/src/miloco/perception/collect/camera_adapter.py \
    miloco/tests/perception/collect/test_camera_adapter.py \
    miloco/tests/perception/collect/test_rtsp_frame_admission.py)
  (cd backend && uv run ruff format --check \
    miloco/src/miloco/perception/collect/camera_adapter.py \
    miloco/tests/perception/collect/test_camera_adapter.py \
    miloco/tests/perception/collect/test_rtsp_frame_admission.py)
  git diff --check
  git add backend/miloco/src/miloco/perception/collect/camera_adapter.py \
    backend/miloco/tests/perception/collect/test_camera_adapter.py \
    backend/miloco/tests/perception/collect/test_rtsp_frame_admission.py
  git commit -m "fix(perception): sample RTSP frames before buffering"
  ```

---

## Task 3: Admit transcoder input before copying and bound timestamp history

**Files:**

- Modify: `backend/miloco/src/miloco/camera/transcoder.py`
- Modify/Test: `backend/miloco/tests/camera/test_transcoder.py`

**Interfaces:**

```python
_EMITTED_TIMESTAMP_HISTORY = 256


class SharedH264Transcoder:
    # Existing public push_frame, stop, and properties remain compatible.
    self._emitted_timestamps: deque[int] = deque(
        maxlen=_EMITTED_TIMESTAMP_HISTORY
    )
```

- [ ] **Step 1: Write RED copy-admission tests**

  Monkeypatch `miloco.camera.transcoder.np.ascontiguousarray` with a counting wrapper. Assert an invalid-shape frame raises without copying, a valid frame pushed before any viewer/generation returns without copying, and a valid active-generation frame copies exactly once.

  Add a blocking encoder case with `queue_size=2`: fill the worker plus queue, push newest frames, and assert queue depth never exceeds two and stale `_FrameInput.frame` references are released before the newest copy is retained.

- [ ] **Step 2: Write RED bounded-history test**

  Start a generation, call `_publish()` with 300 safe synthetic packet/timestamp pairs, and assert:

  ```python
  assert len(transcoder.emitted_timestamps) == 256
  assert transcoder.emitted_timestamps == tuple(range(44, 300))
  ```

  This test exercises only timestamp retention and viewer queue behavior; it does not encode 300 video frames.

- [ ] **Step 3: Run focused tests and confirm RED**

  ```bash
  (cd backend && uv run pytest -q miloco/tests/camera/test_transcoder.py)
  ```

  Expected: inactive valid frames currently invoke the copy path and timestamp history grows beyond 256.

- [ ] **Step 4: Move the bounded-queue decision before the ndarray copy**

  Keep shape validation first, then take the existing lock. Under the lock:

  ```python
  queue = self._input
  if self._active_generation is None or queue is None:
      return
  if queue.full():
      try:
          queue.get_nowait()
          self._dropped_frames += 1
      except asyncio.QueueEmpty:
          pass
  image = np.ascontiguousarray(frame, dtype=np.uint8).copy()
  queue.put_nowait(_FrameInput(image, pts))
  ```

  The copy remains inside the lock so `stop()` cannot invalidate the admitted generation between the decision and `put_nowait()`. Release the evicted queue item before allocating the replacement copy. Do not change FPS/PTS filtering, viewer queues, encoder threads, error codes, or codec settings.

- [ ] **Step 5: Replace the unbounded list with a fixed deque**

  Import `deque`, define `_EMITTED_TIMESTAMP_HISTORY = 256`, initialize `deque(maxlen=...)`, and keep the public property returning an immutable tuple. Existing small-history assertions remain unchanged.

- [ ] **Step 6: Run GREEN, real encoder regressions, static checks, and commit**

  ```bash
  (cd backend && uv run pytest -q miloco/tests/camera/test_transcoder.py)
  (cd backend && uv run ruff check \
    miloco/src/miloco/camera/transcoder.py \
    miloco/tests/camera/test_transcoder.py)
  (cd backend && uv run ruff format --check \
    miloco/src/miloco/camera/transcoder.py \
    miloco/tests/camera/test_transcoder.py)
  git diff --check
  git add backend/miloco/src/miloco/camera/transcoder.py \
    backend/miloco/tests/camera/test_transcoder.py
  git commit -m "fix(camera): bound transcoder frame copies"
  ```

  Expected: all existing Annex B, aspect-ratio, PTS, generation, stop, and error tests remain green; new copy/history bounds pass.

---

## Task 4: Coalesce live-stream transcoder callbacks to one active plus one latest pending frame

**Files:**

- Modify: `backend/miloco/src/miloco/camera/stream.py`
- Modify/Test: `backend/miloco/tests/camera/test_stream_hub.py`
- Regression test: `backend/miloco/tests/integration/test_rtsp_live_view.py`

**Interfaces:**

```python
@dataclass
class _CameraFeed:
    # existing fields remain
    transcode_push_task: asyncio.Task[None] | None = None
    transcode_pending: tuple[NDArray[np.uint8], int | None] | None = None
    transcode_coalesced_frames: int = 0
```

`transcode_task` remains the encoded-output consumer. `transcode_push_task` is the single decoded-input drain task; the two roles must not be combined.

- [ ] **Step 1: Add a blocking fake transcoder and write RED burst tests**

  Extend `_FakeTranscoder` or add `_BlockingPushTranscoder` with `push_entered`, `push_release`, and a list of received pixel values. Subscribe to an HEVC source, emit frame `1`, wait until its push blocks, then synchronously emit frames `2..100`.

  Assert before release:

  ```python
  assert transcoder.received == [1]
  assert feed.transcode_pending[0][0, 0, 0] == 100
  assert feed.transcode_push_task is not None
  assert not feed.transcode_push_task.done()
  assert feed.transcode_coalesced_frames == 98
  ```

  After release, assert received values are exactly `[1, 100]`, the pending reference becomes `None`, and no second push task is created.

- [ ] **Step 2: Write RED teardown, stale-feed, error, and passthrough tests**

  Cover:

  - last viewer detach cancels/settles the push task and clears pending before `transcoder.stop()` completes;
  - `close_camera`, source close, transcoder failure, and replacement subscription leave no push task or pending ndarray on the old feed;
  - a stale callback from an old feed cannot populate the replacement feed;
  - a cancelled push task does not publish `transcode_failed` during normal teardown;
  - compatible H.264 passthrough never creates `transcode_push_task`, never retains `transcode_pending`, and never calls the transcoder factory;
  - two cameras coalesce independently and one blocked camera does not block the other.

- [ ] **Step 3: Run focused tests and confirm RED**

  ```bash
  (cd backend && uv run pytest -q miloco/tests/camera/test_stream_hub.py \
    -k 'transcod or passthrough or shutdown or close_camera')
  ```

  Expected: the burst test observes many independent tasks/pushes because `_schedule_frame()` currently calls `asyncio.create_task()` for every decoded frame.

- [ ] **Step 4: Implement latest-frame scheduling without copying**

  Replace the per-frame task creation with reference replacement:

  ```python
  def _schedule_frame(self, camera_id, source_feed, frame, pts) -> None:
      feed = self._feeds.get(camera_id)
      if feed is not source_feed or feed.mode != "transcoding":
          return
      if feed.transcoder is None:
          return
      if feed.transcode_pending is not None:
          feed.transcode_coalesced_frames += 1
      feed.transcode_pending = (frame, pts)
      task = feed.transcode_push_task
      if task is None or task.done():
          feed.transcode_push_task = asyncio.create_task(
              self._drain_transcode_frames(camera_id, feed, feed.transcoder)
          )
  ```

  The hub stores the source ndarray reference without copying. Replacing `transcode_pending` immediately releases the old pending reference.

- [ ] **Step 5: Implement one drain loop with identity checks at both boundaries**

  ```python
  async def _drain_transcode_frames(
      self,
      camera_id: str,
      source_feed: _CameraFeed,
      transcoder: H264Transcoder,
  ) -> None:
      try:
          while True:
              if (
                  self._feeds.get(camera_id) is not source_feed
                  or source_feed.transcoder is not transcoder
              ):
                  return
              pending, source_feed.transcode_pending = (
                  source_feed.transcode_pending,
                  None,
              )
              if pending is None:
                  return
              frame, pts = pending
              await transcoder.push_frame(frame, pts)
      except asyncio.CancelledError:
          raise
      except Exception:
          self._transcoder_failed(camera_id, source_feed, "transcode_failed")
      finally:
          if source_feed.transcode_push_task is asyncio.current_task():
              source_feed.transcode_push_task = None
  ```

  Check feed/transcoder identity again after an awaited push before taking the next pending frame. A normal cancellation must not enter the error path.

- [ ] **Step 6: Make shutdown release pending data before stopping the encoder**

  In `_stop_transcoder()`:

  1. clear `feed.transcode_pending`;
  2. detach `feed.transcode_push_task`, cancel it if it is not the current task, and gather it with `return_exceptions=True`;
  3. clear `feed.transcoder` so stale identity checks fail;
  4. stop the transcoder;
  5. settle the existing encoded-output consumer and close a stream only when no consumer owns it.

  Preserve the existing single registered shutdown transaction in `_stopping`; do not add a second feed lock or global task registry.

- [ ] **Step 7: Run GREEN and the real fixture integration**

  ```bash
  (cd backend && uv run pytest -q \
    miloco/tests/camera/test_stream_hub.py \
    miloco/tests/camera/test_transcoder.py \
    miloco/tests/integration/test_rtsp_live_view.py)
  ```

  Expected: burst coalescing, teardown, compatible H.264 passthrough, H.265/incompatible-H.264 transcode selection, one-session fixture E2E, and bounded viewer queues all pass.

- [ ] **Step 8: Run static checks and commit Task 4**

  ```bash
  (cd backend && uv run ruff check \
    miloco/src/miloco/camera/stream.py \
    miloco/tests/camera/test_stream_hub.py \
    miloco/tests/integration/test_rtsp_live_view.py)
  (cd backend && uv run ruff format --check \
    miloco/src/miloco/camera/stream.py \
    miloco/tests/camera/test_stream_hub.py)
  git diff --check
  git add backend/miloco/src/miloco/camera/stream.py \
    backend/miloco/tests/camera/test_stream_hub.py \
    backend/miloco/tests/integration/test_rtsp_live_view.py
  git commit -m "fix(camera): coalesce pending transcode frames"
  ```

---

## Task 5: Run whole-branch regression, independent review, and freeze one candidate SHA

**Files:**

- Modify: `docs/2026-08-28-rtsp-responses-support_PROGRESS.md`
- Modify: `docs/superpowers/specs/2026-08-30-rtsp-memory-bounds-design.md`
- Read-only: all runtime/test changes from Tasks 1-4

**Interfaces:**

- Consumes: four separately reviewed implementation commits and the approved spec.
- Produces: no open Critical/Important review findings, a complete local gate record, and one clean candidate commit that is the only SHA eligible for build/lab/production deployment.

- [ ] **Step 1: Run the full focused backend repair matrix**

  ```bash
  (cd backend && uv run pytest -q \
    miloco/tests/perception/collect/test_stream_buffer_retention.py \
    miloco/tests/perception/test_stream_buffer_overflow.py \
    miloco/tests/perception/collect/test_rtsp_frame_admission.py \
    miloco/tests/perception/collect/test_camera_adapter.py \
    miloco/tests/perception/collect/test_rtsp_camera_source.py \
    miloco/tests/perception/collect/test_rtsp_session.py \
    miloco/tests/camera/test_stream_hub.py \
    miloco/tests/camera/test_transcoder.py \
    miloco/tests/integration/test_rtsp_live_view.py)
  ```

  Require zero failures. Do not classify a new failure as baseline without reproducing it on `37e1d58c` in an isolated read-only comparison.

- [ ] **Step 2: Run inherited RTSP/Responses repair regressions**

  ```bash
  (cd backend && uv run pytest -q \
    miloco/tests/api/test_cameras.py \
    miloco/tests/api/test_config_api.py \
    miloco/tests/omni/test_probe.py \
    miloco/tests/omni/test_error_classifier.py \
    miloco/tests/omni/test_openai_responses_adapter.py \
    miloco/tests/test_omni_client_circuit.py)
  (cd web && npm test -- \
    tests/perception-camera-view.test.ts \
    tests/watch-mse.test.js \
    tests/watch-page.test.ts \
    tests/omni-config-policy.test.ts \
    tests/omni-health-state.test.ts)
  ```

  Preserve the already-proven 15-second global connect/read timeout and 30-second Responses visual POST behavior; this repair does not edit them.

- [ ] **Step 3: Run repository-wide read-only gates**

  ```bash
  ./scripts/local-ci.sh --tests
  (cd web && npm test && npm run typecheck && npm run build)
  (cd backend && uv run pytest -q miloco/tests/test_node_monitor.py)
  backend/.venv/bin/python -m pytest -q deploy/ai-lab/tests/test_deploy_contract.py
  bash -n deploy.sh deploy/ai-lab/remote-release.sh \
    deploy/ai-lab/container-entrypoint.sh scripts/rtsp-smoke.sh \
    scripts/rtsp-view-smoke.sh scripts/responses-vlm-smoke.sh
  ```

  Record inherited macOS node-monitor exclusions and any existing Vite bundle warning separately. A known baseline may be documented but never counted as new green proof.

- [ ] **Step 4: Run scoped static, scope, and privacy gates**

  ```bash
  (cd backend && uv run ruff check \
    miloco/src/miloco/perception/collect/stream_buffer.py \
    miloco/src/miloco/perception/collect/camera_adapter.py \
    miloco/src/miloco/camera/transcoder.py \
    miloco/src/miloco/camera/stream.py \
    miloco/tests/perception/collect/test_stream_buffer_retention.py \
    miloco/tests/perception/collect/test_rtsp_frame_admission.py \
    miloco/tests/perception/collect/test_camera_adapter.py \
    miloco/tests/camera/test_transcoder.py \
    miloco/tests/camera/test_stream_hub.py)
  (cd backend && uv run ruff format --check \
    miloco/src/miloco/perception/collect/stream_buffer.py \
    miloco/src/miloco/perception/collect/camera_adapter.py \
    miloco/src/miloco/camera/transcoder.py \
    miloco/src/miloco/camera/stream.py)
  git diff --check
  git status --short
  git diff --name-only 37e1d58c82814448c7a3381c256e3f4aca6b1b71..HEAD
  ```

  The diff must remain within the four runtime modules, their focused tests, and approved docs. Search changed text for URI userinfo, Authorization values, base64 image material, API-key literals, and provider bodies; findings must be absent, not merely redacted after capture.

- [ ] **Step 5: Perform independent whole-branch spec and quality review**

  Ask one fresh reviewer to compare `37e1d58c..HEAD` against every numbered section of the approved spec, including MIoT/passthrough non-goals, lifecycle cleanup, exact accounting, and deployment gates. Ask a second fresh reviewer to inspect concurrency, lock ordering, cancellation, reference release, byte-accounting under every window transition, test realism, goal fit, and process cost.

  Critical or Important findings block the candidate. Apply at most two targeted correction rounds, rerun the affected RED/GREEN tests plus the full focused matrix, and commit any correction as:

  ```bash
  git commit -m "fix(camera): close bounded memory review findings"
  ```

  Stop and escalate before a third correction round for the same failure class.

- [ ] **Step 6: Record the local release milestone and freeze the candidate**

  Update the spec status to implementation complete/local acceptance passed, while production acceptance remains pending. Append a timestamped progress entry containing:

  - implemented frame/item/byte/task bounds;
  - focused and full gate results;
  - independent review outcome;
  - inherited baseline exclusions;
  - real RTSP/provider and 10-minute production proof as `not_measured`;
  - the next action: build once and validate sequentially.

  Commit only the two docs:

  ```bash
  git add docs/2026-08-28-rtsp-responses-support_PROGRESS.md \
    docs/superpowers/specs/2026-08-30-rtsp-memory-bounds-design.md
  git commit -m "docs(progress): freeze RTSP memory repair candidate"
  git status --porcelain
  git rev-parse HEAD
  ```

  Require a clean worktree. Record this full 40-character SHA as `candidate_sha`. No tracked edit is allowed until the same SHA has completed both labs and production acceptance.

---

## Task 6: Build and independently review exactly one immutable candidate archive

**Files:**

- Create through the existing controller only: `dist/lab/<candidate_sha>/miloco-lab-<candidate_sha>.tar.gz`
- Create through the existing controller only: `dist/lab/<candidate_sha>/miloco-lab-<candidate_sha>.receipt`
- Write operational evidence only to the ignored SDD ledger until production acceptance completes.

**Interfaces:**

- Consumes: Task 5's clean exact candidate SHA.
- Produces: one immutable archive/receipt pair and its SHA-256 digest. This task may invoke `./deploy.sh build` exactly once.

- [ ] **Step 1: Prove the build identity and prior-release preservation boundary**

  ```bash
  candidate_sha="$(git rev-parse HEAD)"
  test -z "$(git status --porcelain)"
  test "$candidate_sha" != "8b211af3fbe3fbff7df7389420a8ed8e56e31bfb"
  find dist/lab -maxdepth 2 -type f -print | sort
  ```

  Record structural names/digests for preserved `3d4`, `d969`, and `8b211` artifacts where present. Do not recreate a missing historical local artifact and do not delete anything.

- [ ] **Step 2: Build once**

  ```bash
  ./deploy.sh build
  ```

  Do not rerun this command for the same SHA. Any failure is a failed candidate-build attempt; diagnose without deleting published history or invoking a same-SHA rebuild.

- [ ] **Step 3: Review receipt, archive, and packaged behavior independently**

  Require:

  - receipt mode `0444`, exact `git_sha`, archive digest, controller digest, and allowlist digest;
  - archive path bound to the full candidate SHA;
  - archive checksum equals the receipt;
  - packaged versions of all four changed runtime modules match the committed tree;
  - packaged `/watch-mse.js` contains the existing readiness/cancellation markers;
  - packaged Responses timeout behavior remains 30 seconds for the visual POST and 15/10 globally;
  - no `.git`, local config, virtual environment, cache, credentials, RTSP userinfo, Authorization values, image bodies, or provider bodies;
  - prior immutable archives/receipts/markers remain byte-identical.

  A fresh artifact reviewer must report no Critical/Important finding before lab deployment.

- [ ] **Step 4: Freeze exact values in the ignored ledger**

  Record only:

  ```text
  candidate_sha=<40 lowercase hex>
  archive_sha256=<64 lowercase hex>
  receipt_sha256=<64 lowercase hex>
  controller_sha256=<64 lowercase hex>
  build_invocations=1
  worktree_clean=true
  ```

  Do not make a tracked evidence commit; HEAD must remain the built candidate SHA.

---

## Task 7: Deploy the byte-identical candidate to `ai-lab01.esxi`, then `ai-lab02.esxi`

**Files:**

- Read-only local candidate archive/receipt from Task 6.
- Write operational evidence only to the ignored SDD ledger.
- No tracked repository edits.

**Interfaces:**

- Consumes: the one Task 6 archive and `/Users/nicholasliao/.ssh/id_co_openclaw`.
- Produces: sequential exact-SHA lab acceptance with fixture, health, restart, OOM, asset, and timeout proof. Labs are CO/PAM-exempt.

- [ ] **Step 1: Verify SSH identity structurally without reading it**

  ```bash
  stat -f '%Su %Lp %N' /Users/nicholasliao/.ssh/id_co_openclaw
  test ! -L /Users/nicholasliao/.ssh/id_co_openclaw
  test -z "$(git status --porcelain)"
  ```

  Require current-user ownership, regular file, and no group/other permission bits.

- [ ] **Step 2: Preflight and deploy `ai-lab01.esxi`**

  ```bash
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw \
    ./deploy.sh preflight ai-lab01.esxi
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw \
    ./deploy.sh deploy ai-lab01.esxi
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw \
    ./deploy.sh verify ai-lab01.esxi
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw \
    ./deploy.sh status ai-lab01.esxi
  ```

  Require exact candidate/archive binding, acceptance marker, fixture suite success, HTTP `/health`, packaged `/watch-mse.js`, `RestartCount=0`, `OOMKilled=false`, and the configured lab01 resource profile. On failure, stop; do not touch lab02.

- [ ] **Step 3: Independently review lab01 evidence**

  A reviewer must match the remote runtime marker, canonical images, release directory, artifact record, acceptance marker, and container binding to `candidate_sha` and the Task 6 archive digest. Confirm no retention deletion or same-SHA mutation. Fixture proof is recorded as fixture proof, not real-camera/provider proof.

- [ ] **Step 4: Prove no drift and deploy `ai-lab02.esxi` without rebuilding**

  Recompute local archive/receipt/controller digests and require exact equality with the frozen Task 6 ledger. Then run:

  ```bash
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw \
    ./deploy.sh preflight ai-lab02.esxi
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw \
    ./deploy.sh deploy ai-lab02.esxi
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw \
    ./deploy.sh verify ai-lab02.esxi
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw \
    ./deploy.sh status ai-lab02.esxi
  ```

  Require the same exact evidence as lab01, with lab02's configured resource profile. Do not invoke `build`.

- [ ] **Step 5: Observe lab02 idle stability and review both hosts**

  Take bounded status samples for five minutes, yielding control at least every 60 seconds. Require no restart, no OOM flag, no health loss, and no candidate/archive drift. A fresh reviewer compares both host results and confirms the same byte-identical release passed sequentially.

  Keep the worktree and HEAD unchanged for production. Real two-camera playback and provider inference remain pending even if lab fixtures pass.

---

## Task 8: Open a new production CO and deploy the exact candidate from the stopped state

**Files:**

- Read-only local candidate archive/receipt and ignored evidence ledger.
- Write ITSM CO payload/receipt only under a safe temporary directory as directed by the `itsm-co` skill.
- Write acceptance samples only to the ignored SDD ledger until the CO outcome is known.
- No tracked repository edits during the change window.

**Interfaces:**

- Consumes: exact candidate SHA/digest, two green labs, intentionally stopped production, existing saved RTSP sources and OpenAI Responses profile.
- Produces: one governed production deployment at `docker.esxi:1811`, real two-camera/browser/provider evidence, ten-minute memory acceptance, or the approved offline rollback state.

- [ ] **Step 1: Invoke `itsm-co` and draft an exact-SHA Standard CO**

  The CO scope is only `docker.esxi/root`. Include:

  - candidate SHA and archive SHA-256;
  - current production state: `miloco-miloco-1` stopped, historical `d969` known to OOM;
  - normal preflight with no memory exception;
  - unchanged `3072m` container limit and port `1811`;
  - sequential lab evidence and independent review;
  - deploy, verify, real acceptance, ten-minute sampling, and offline rollback commands;
  - permission to use the already-saved camera sources and saved `openai_responses` profile only for structural playback, visual preflight, and one real inference to `http://ai.esxi:18090/v1/responses`;
  - a prohibition on reading/printing the saved key, RTSP credentials, frames, request/response bodies, and inference content;
  - a prohibition on cleanup, retention deletion, config rewrite, same-SHA rebuild, or treating running `d969` as a healthy rollback.

  Stop and ask the user if ITSM requests approval or denies the CO. Do not bypass.

- [ ] **Step 2: Wait for `state=Implement` and active PAM, then run pre-mutation checks**

  After approval/PAM, verify the local worktree is clean and the candidate receipt/digests still match. Through the approved controller run:

  ```bash
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw \
    ./deploy.sh preflight docker.esxi
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw \
    ./deploy.sh status docker.esxi
  ```

  Require host memory preflight green, port/controller ownership expected, no running Miloco container, current historical release pointer preserved, and no new OOM event since the stop. A preflight failure makes the CO `Not Executed`; production remains offline.

- [ ] **Step 3: Deploy and verify the exact candidate**

  ```bash
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw \
    ./deploy.sh deploy docker.esxi
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw \
    ./deploy.sh verify docker.esxi
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw \
    ./deploy.sh status docker.esxi
  ```

  Require exact SHA, archive/artifact/acceptance/runtime markers, canonical image/container binding, `/health` HTTP 200, `/watch-mse.js` HTTP 200 with `createReadyJmuxer`, `jmuxer_ready_timeout`, and `jmuxer_cancelled`, `RestartCount=0`, `OOMKilled=false`, and `Memory=3072m`.

- [ ] **Step 4: Validate real perception membership and browser playback**

  Use the authenticated app/browser surface without exposing saved configuration. Require:

  - exactly the two configured RTSP sources appear in `/api/perception/devices` and as live perception cards;
  - their card identities join exactly to camera summaries;
  - no MIoT-only voice, prompt, bulk, or feed controls render on RTSP cards;
  - every required dashboard/management player reaches `readyState >= 2`;
  - every player has `videoWidth > 0`, `videoHeight > 0`, and `video.error === null`;
  - `currentTime` advances across two samples separated by at least two seconds.

  Record only camera count, safe source type, player route, readyState, dimensions, error-null boolean, and time-advanced boolean. Do not record camera names if they contain sensitive location context, pixels, screenshots, URLs, or credentials.

- [ ] **Step 5: Validate the saved Responses profile and one real visual inference**

  Activate/test the already-saved `openai_responses` profile through the existing UI/API. Require the connection result to be the protocol-specific visual preflight success, not only a text `/models` reachability result. Then trigger one normal Miloco visual inference with the configured cameras.

  Record only protocol `openai_responses`, endpoint host/port/path already approved in the CO, success booleans, safe error code if failed, and elapsed milliseconds. Do not capture the API key, request, image, response body, or inferred content.

- [ ] **Step 6: Run the pre-registered ten-minute memory/restart/OOM acceptance**

  While both RTSP streams and model path remain active, take 11 samples at `t=0,60,...,600` seconds. Yield control between samples; do not issue one blocking sleep longer than 60 seconds. Each sample records:

  ```text
  elapsed_seconds
  /sys/fs/cgroup/memory.current from miloco-miloco-1
  Docker RestartCount
  Docker State.OOMKilled
  HTTP health boolean
  player time-advanced boolean
  model health state/code without content
  ```

  Fixed veto rules, established before sampling:

  - every sample has `RestartCount=0`, `OOMKilled=false`, and health true;
  - no Docker event or kernel journal entry reports Miloco OOM after deployment;
  - peak `memory.current < 2_684_354_560` bytes (2.5 GiB);
  - the final six memory samples must not be strictly increasing with a cumulative rise of `>= 67_108_864` bytes; that pattern is treated as sustained monotonic growth;
  - playback remains advancing and the saved model health does not enter a terminal configuration/error state.

  These are veto gates. Do not shorten the ten-minute window or reinterpret a failure as warning-only.

- [ ] **Step 7: Execute the offline rollback path on any veto failure**

  If failure occurs before mutation, leave production stopped. If deployment/acceptance fails after mutation, use the controller's recorded rollback transaction as required for state integrity, collect bounded structural status, then stop any automatically restored `d969` container within the same CO.

  Final rollback acceptance is:

  ```text
  running Miloco containers = 0
  saved configuration/state preserved
  historical and candidate releases/receipts/markers preserved
  no cleanup or same-SHA rebuild
  exact controller current/previous state reported
  ```

  Never report `d969` as a healthy online rollback. Close the CO according to the actual result.

- [ ] **Step 8: Independently review production evidence before closure**

  A fresh reviewer checks exact-SHA binding, both real camera cards, playback measurements, Responses preflight/inference booleans, all 11 memory samples, restart/OOM evidence, privacy boundary, and the actual rollback/service state. Any open Critical/Important finding blocks successful CO closure.

---

## Task 9: Close the CO accurately and publish the final evidence boundary

**Files:**

- Modify: `docs/2026-08-28-rtsp-responses-support_PROGRESS.md`
- Modify: `docs/superpowers/specs/2026-08-30-rtsp-memory-bounds-design.md`
- Modify when operationally accurate: `docs/2026-08-29-docker-esxi-deployment_PROGRESS.md`

**Interfaces:**

- Consumes: reviewed production outcome and ITSM record.
- Produces: accurate CO closure, a docs-only closeout commit, clean branch, and an explicit distinction between deployed candidate SHA and later documentation HEAD.

- [ ] **Step 1: Close the CO according to measured outcome**

  Use `itsm-co` to close:

  - `Successfully Closed` only if exact deployment, two-camera playback/perception, Responses preflight/inference, and all ten-minute veto gates passed;
  - `Not Executed` if no production mutation occurred;
  - the skill's correct unsuccessful/rollback outcome if mutation occurred and offline rollback was required.

  Include only bounded structural evidence and no secret/content data.

- [ ] **Step 2: Update final docs after the change window**

  Record:

  - code/candidate/deployed SHA and archive digest;
  - later documentation HEAD relationship;
  - both lab outcomes and evidence level;
  - CO number, PAM/Implement gate, actual production outcome;
  - real camera count, browser readiness/dimensions/time advancement without images;
  - Responses protocol, preflight/inference booleans and safe timings;
  - 11 memory samples summarized as min/peak/final plus fixed-growth-rule result;
  - restart/OOM results and final running/offline service state;
  - preserved `3d4`, `d969`, `8b211`, and candidate history;
  - remaining limitations and separately scoped follow-up, if any.

  Mark the spec complete only after successful production acceptance. If rollback occurred, mark implementation/labs complete and production acceptance failed/pending without weakening the completion definition.

- [ ] **Step 3: Run docs/privacy checks and commit closeout**

  ```bash
  git diff --check
  git diff -- \
    docs/2026-08-28-rtsp-responses-support_PROGRESS.md \
    docs/2026-08-29-docker-esxi-deployment_PROGRESS.md \
    docs/superpowers/specs/2026-08-30-rtsp-memory-bounds-design.md
  git add docs/2026-08-28-rtsp-responses-support_PROGRESS.md \
    docs/2026-08-29-docker-esxi-deployment_PROGRESS.md \
    docs/superpowers/specs/2026-08-30-rtsp-memory-bounds-design.md
  git commit -m "docs(progress): record RTSP memory repair acceptance"
  git status --short --branch
  ```

  Inspect the staged diff before commit and reject any secret, URL userinfo, frame/image content, provider body, or raw config value.

- [ ] **Step 4: Handle remote publication without writing to Xiaomi upstream**

  Inspect remote names/URLs without credentials. Push only if a user-authorized writable remote for this branch exists. Never push to `XiaoMi/xiaomi-miloco`. If no authorized writable remote exists, report `push not performed` and provide the exact local branch, deployed SHA, and docs-only HEAD for later publication.

- [ ] **Step 5: Final handoff**

  Report the outcome first: production healthy or intentionally offline. Then provide candidate/deployed SHA, archive digest, CO outcome, lab status, real acceptance summary, memory peak/final, restart/OOM state, preserved rollback history, later docs HEAD, verification commands/categories, and only genuine remaining limitations.
