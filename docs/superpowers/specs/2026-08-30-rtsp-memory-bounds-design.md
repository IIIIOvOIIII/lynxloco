# RTSP perception memory bounds and transcoder coalescing design

Status: Implementation and local acceptance, including independent whole-branch correction re-review, passed on 2026-08-30; candidate build, lab acceptance, and production acceptance remain pending.

## 1. Context

Miloco production on `docker.esxi` runs with a 3 GiB container memory limit. After the host was expanded to approximately 12 GiB RAM, the normal deployment preflight passed with more than 5 GiB host memory available, but the existing `d9698d7e2b9c3465faa87c8a02f1ee59cbbfc693` container continued to hit its own 3 GiB cgroup limit and exit with code 137 approximately every 30 seconds. Its accumulated restart count reached 77.

The lab-proven timeout candidate `8b211af3fbe3fbff7df7389420a8ed8e56e31bfb` changes only the OpenAI Responses visual-probe timeout. It does not change RTSP frame retention, so it was not uploaded or deployed to production. Production CO `CHG260830001` closed `Not Executed` after the pre-deployment safety check detected the continuing OOM loop.

Under user-approved mitigation CO `CHG260830002`, only container `miloco-miloco-1` was stopped. The container, saved configuration, state, images, and releases were preserved. Production Miloco is intentionally unavailable until a repaired release passes this specification and is deployed under a new CO.

## 2. Root cause

The failure is caused by a memory-boundedness mismatch rather than host memory exhaustion or an unbounded RTSP connection count.

The current RTSP perception path is:

```text
RTSP source-rate packets
  -> PyAV decodes every video frame
  -> every frame becomes a full-resolution BGR ndarray
  -> CameraDeviceAdapter inserts every ndarray into MultiTrackSyncBuffer
  -> four-second windows retain source-rate frames
  -> up to three drained windows plus the active window remain resident
  -> pipeline downsampling to perception.engine.input.fps occurs only after drain
```

For two 1280x720 sources at 25-30 FPS, four retained four-second spans account for approximately 2.1-2.5 GiB of BGR payload before PyAV/FFmpeg allocations, ONNX Runtime sessions and arenas, Python objects, thread stacks, and conditional transcoder copies. The existing window-count bound is therefore logically finite but does not impose a safe frame or byte budget.

Browser streaming is not the primary cause when H.264 passthrough is used. When HEVC or incompatible H.264 requires transcoding, the current scheduling path can amplify the peak by creating an asynchronous push for each decoded frame and copying a full ndarray before bounded-queue admission.

## 3. Goals

The implementation must:

1. Keep the container memory limit at 3 GiB and make two configured RTSP cameras stable within that budget.
2. Reduce only the decoded frames admitted to the perception buffer to the configured perception input rate.
3. Bound RTSP perception retention by both frame count and real ndarray payload bytes.
4. Preserve the newest useful frames when backpressure is applied.
5. Keep RTSP packet passthrough and browser playback frame rate independent from perception sampling.
6. Prevent transcoder scheduling and pre-admission frame copies from accumulating under encoder pressure.
7. Preserve audio collection, MIoT behavior, RTSP lifecycle semantics, saved configuration, provider configuration, and credential boundaries.
8. Provide deterministic tests that fail when frame count, payload bytes, pending tasks, or copies can grow beyond the approved bounds.
9. Produce one new immutable candidate SHA and archive after all local gates pass, then validate it sequentially on both labs before a new production CO.

## 4. Non-goals

This change does not:

- increase the 3 GiB container memory limit;
- treat host expansion as application stability proof;
- share, pool, or otherwise refactor ONNX Runtime sessions;
- alter Xiaomi, MIoT, RTSP, model, Base URL, API Key, or OpenAI Responses configuration;
- change the user-facing configuration schema or add a memory-control UI;
- change MIoT frame buffering;
- change RTSP probing, authentication, connection, reconnect, or packet passthrough semantics;
- reduce browser playback to the perception frame rate;
- add automatic release retention deletion;
- rebuild an existing SHA or overwrite any immutable archive/receipt;
- recreate the missing older local artifact or alter the preserved `3d4`, `d969`, or `8b211` releases;
- make the known-OOM `d969` release an acceptable running rollback state.

If post-fix evidence shows a separate ONNX Runtime baseline problem, that work requires a new diagnosis and design. It must not be folded into this repair spec.

## 5. Architecture

The change has three independent units:

```text
RtspSession decoded frame
  |-- packet listener --------------------------> H.264 passthrough, unchanged
  |-- video-frame listener ---------------------> transcoder admission/coalescing
  `-- perception video callback
        -> RTSP-only monotonic frame admission
        -> optional bounded MultiTrackSyncBuffer policy
        -> perception drain
        -> existing pipeline/downsampling/identity/Omni
```

### 5.1 RTSP-only perception admission

`CameraDeviceAdapter` owns the boundary between a camera driver's decoded callback and `MultiTrackSyncBuffer`. It already knows which source driver owns each DID, so it must apply the new admission policy only when `_did_source_types[did] == "rtsp"`.

Each active RTSP DID receives an admission state containing the last admitted host-monotonic timestamp. The target rate is the current `perception.engine.input.fps`; the default is 3 FPS. The admission interval is derived from that value and evaluated using host monotonic time, not camera PTS.

Rules:

- the first frame is admitted;
- a later frame is admitted when the host-monotonic interval has elapsed;
- frames arriving before the interval are dropped from the perception branch;
- camera PTS may regress, repeat, jump, or be absent without disabling the bound;
- the admitted frame remains the newest frame available at that admission point;
- invalid or non-positive target FPS must fail closed to a minimum of 1 FPS rather than disabling admission;
- settings are read through an injectable/small stable boundary so tests can change the target rate without rebuilding the whole service;
- the state is removed on device disconnect, inactive-device pruning, failed connection cleanup, adapter shutdown, and source replacement.

The RTSP session continues to decode at the rate required by packet passthrough or transcoding. This policy controls only the frame reference handed to perception buffering.

### 5.2 RTSP perception buffer frame and byte bounds

`MultiTrackSyncBuffer` remains the generic synchronization primitive. It gains an optional video-retention policy. Existing callers receive the current behavior when the option is absent. `CameraDeviceAdapter` supplies the option only for RTSP sources.

The RTSP policy has two simultaneous bounds:

1. Per-window video frame count:

   ```text
   ceil(target_fps * window_size_seconds) + 1
   ```

   With the default 3 FPS and four-second window, this is 13 frames.

2. Per-source buffered video payload bytes across active and drained windows: 128 MiB.

Video payload size is calculated from `ndarray.nbytes` at insertion. The implementation must not estimate bytes from width/height metadata when an ndarray is available. Non-video or non-ndarray fragments do not contribute to this video-byte counter.

When the per-window frame limit is exceeded, the oldest video fragment in that window is removed before the newest fragment is retained. When the total byte limit is exceeded, the implementation removes the oldest buffered video fragments across the oldest drained/active windows until the total is within budget. Empty track/window structures are cleaned without changing audio data in otherwise non-empty windows.

The policy must preserve these invariants:

- the newest admitted video frame is retained whenever one frame fits within the 128 MiB budget;
- one single frame larger than 128 MiB is rejected safely and does not evict unrelated audio indefinitely;
- byte accounting exactly follows insertion, eviction, drain, clear, source removal, and shutdown;
- `_windows`, `_ready_queue`, `_ready_keys`, and `_drained` stay mutually consistent after eviction;
- existing ready-window ordering and newest-window drain semantics remain unchanged;
- video-cap drops contribute to existing/backward-compatible backpressure and dropped-frame observability;
- no cap event raises through the RTSP callback, reconnects the source, or terminates the collector;
- MIoT buffers receive no new bound unless a later separately approved design opts them in.

The 128 MiB limit is an internal safety constant, not a user-editable setting. At 720p it holds approximately 48 BGR frames, about 16 seconds at 3 FPS. At 1080p it holds approximately 21 BGR frames, more than one complete four-second window at 3 FPS. Two RTSP sources therefore have an aggregate decoded-buffer ceiling of approximately 256 MiB.

### 5.3 Transcoder latest-frame coalescing

The transcoder path must stop creating an independently retained asynchronous push for every decoded frame.

Each RTSP live-stream feed that uses transcoding may retain at most:

- one frame currently being submitted/encoded; and
- one latest pending frame awaiting that submission.

Rules:

- when no push is active, schedule one push;
- when a push is active, replace the pending frame reference with the newest frame;
- replacing a pending frame releases the older reference immediately;
- when the active push finishes, submit the latest pending frame, if present;
- no unbounded set/list of push tasks is allowed;
- feed close, viewer removal that stops transcoding, source close, reconnect, and application shutdown cancel/settle the active work and clear the pending reference;
- direct H.264 passthrough does not enter this path and remains unchanged.

`SharedH264Transcoder.push_frame()` must make its bounded-queue admission decision before allocating a full `np.ascontiguousarray(...).copy()`. If the frame will replace an older queued frame, the older item is released first; only the final admitted newest frame is copied. Encoder timestamp history must be bounded to the smallest amount required by current behavior or removed if it has no consumer.

## 6. Data flow and ownership

### 6.1 Perception branch

1. `RtspSession` produces a decoded ndarray.
2. `CameraDeviceAdapter` checks the source type.
3. MIoT follows the existing path.
4. RTSP passes through the monotonic admission policy.
5. A rejected frame loses its perception reference immediately.
6. An admitted frame becomes a `DecodedVideoFrame` and enters the RTSP-configured `MultiTrackSyncBuffer`.
7. The buffer enforces the per-window frame limit and global video-byte limit while retaining the newest data.
8. Existing collection/drain and pipeline logic consumes the bounded frames.
9. Existing pipeline sampling may remain as a defensive normalization step; it is no longer the primary memory bound.

### 6.2 Browser branch

1. Compatible H.264 packets continue through packet passthrough at source rate.
2. HEVC or H.264 fallback receives decoded frame listener callbacks.
3. The feed coalesces callbacks into one active plus one latest pending frame.
4. The transcoder admits the newest frame into its bounded input before copying it.
5. Viewer queues keep their existing bounded/latest-data behavior.

The perception and browser branches must not share a single sampling throttle because they have different frame-rate requirements.

## 7. Error handling and lifecycle

- Admission/cap drops are normal backpressure, not source failures.
- No dropped frame may set an RTSP authentication, connectivity, terminal, or reconnect error.
- Listener or transcoder teardown must be idempotent.
- Cancellation must release pending ndarray references even if encoder shutdown raises.
- Exceptions from one viewer/transcoder must not stop perception collection or other viewers.
- If payload-byte introspection fails for an unexpected object, the optional RTSP buffer policy must reject that video fragment safely and record a bounded diagnostic without logging image data.
- Reconnect creates a fresh admission epoch; it must not inherit an old monotonic deadline that suppresses initial frames.
- A source configuration replacement must not leave the old DID admission, buffer-byte, or pending-transcode state alive.

## 8. Observability and privacy

The implementation may expose only aggregate, secret-safe measurements:

- admitted and dropped perception-frame counts;
- current RTSP video payload bytes per buffer;
- maximum observed buffer payload bytes;
- cap/eviction counts and action reason;
- transcoder input depth, coalesced-frame count, and at-most-one pending state;
- container RSS/cgroup memory, restart count, OOM flag, and event timestamps during acceptance.

It must not log or expose:

- RTSP URLs, usernames, passwords, or camera IDs beyond existing safe internal identity handling;
- frame pixels, encoded frame bodies, screenshots, or inference content;
- model API keys, Authorization headers, provider request/response bodies, or saved configuration values.

No credential storage or transmission path changes. The previously approved action-time transmission boundary remains limited to the saved API key and required camera image sent by Miloco to `http://ai.esxi:18090/v1/responses` for visual preflight and one real inference during final acceptance.

## 9. Test design

Implementation must use strict RED then GREEN tests at the smallest stable behavioral boundary.

### 9.1 Admission tests

- Generate 25 FPS and 30 FPS callback sequences using controlled monotonic time.
- Verify default 3 FPS admission within the deterministic interval boundary.
- Verify 1 FPS and a higher configured FPS without hard-coded default coupling.
- Verify the first frame and newest eligible frames are admitted.
- Verify camera PTS repeats, reversals, jumps, and missing values cannot bypass admission.
- Verify two DIDs maintain independent state.
- Verify MIoT callbacks remain unthrottled.
- Verify disconnect, prune, failed connect, reconnect, settings replacement, and shutdown reset state.

### 9.2 Buffer-budget tests

- Feed synthetic 720p and 1080p `numpy.uint8` arrays at 25/30 FPS over at least 30 seconds of logical time.
- Verify the default per-window retained frame count never exceeds 13 for the RTSP policy.
- Verify total buffered video `nbytes` never exceeds 128 MiB per source.
- Verify the newest timestamp/frame remains after frame-count and byte eviction.
- Verify one oversized frame is rejected safely.
- Verify two independent buffers remain within 256 MiB aggregate video payload.
- Verify audio fragments survive video eviction.
- Verify drain, skip, clear, overflow, source removal, and shutdown keep exact byte accounting and internal queue/key consistency.
- Preserve all existing generic-buffer tests for callers without the optional policy.

### 9.3 Transcoder tests

- Block the encoder while delivering a large burst of full-resolution frames.
- Verify at most one active push and one latest pending frame exist.
- Verify the pending frame is replaced rather than accumulated.
- Count copies and prove rejected/replaced frames are not copied before admission.
- Verify the final delivered pending frame is the newest one.
- Verify passthrough bypasses coalescing/copy logic.
- Verify close/cancel/reconnect clears pending references and leaves no background task.
- Verify bounded timestamp history.

### 9.4 Regression and release gates

At minimum, run:

- focused RTSP session, camera adapter, stream buffer, live stream, transcoder, and browser-stream tests;
- the existing RTSP/Responses feature matrix;
- complete backend tests required by the repository local CI;
- complete Web tests, typecheck, and production build;
- node-monitor tests;
- deployment-contract tests;
- shell syntax, scoped Ruff, diff/scope, and privacy checks;
- immutable artifact review proving exact SHA, archive digest, packaged code, required watch assets, and preservation of prior releases.

Inherited warnings/baselines must remain explicitly distinguished from new regressions.

## 10. Laboratory acceptance

After all local gates and independent review pass:

1. Create one docs milestone commit containing the release evidence.
2. Build exactly one new immutable archive for that new SHA.
3. Do not rebuild or overwrite `8b211`, `d969`, or `3d4`.
4. Independently review the archive and receipt.
5. Deploy the exact same archive to `ai-lab01.esxi`.
6. Require preflight, fixture acceptance, exact-SHA verify/status, `/health`, packaged `/watch-mse.js`, timeout semantics, `RestartCount=0`, and `OOMKilled=false`.
7. Only after lab01 passes, deploy the byte-identical archive to `ai-lab02.esxi` and repeat the same checks.

Lab fixture and synthetic high-rate-frame tests prove packaging and bounded logic. They do not count as real RTSP or provider acceptance unless a real approved source/provider is present.

## 11. Production rollout and acceptance

Production remains intentionally stopped until both labs and their independent review pass.

A new exact-SHA production CO must include:

- `docker.esxi/root` as the only host/user scope;
- the new candidate SHA and immutable archive digest;
- the normal memory preflight with no exception;
- preserved 3 GiB container limit;
- the stopped `d969` release as historical rollback evidence, not a healthy online fallback;
- exact rollout, acceptance, and offline rollback commands;
- permission to use the already-saved OpenAI Responses profile for the already-approved destination and acceptance purpose without reading or printing the key.

After deployment, acceptance must prove:

1. Exact new SHA, image, artifact, accepted marker, runtime marker, and container binding.
2. `/health` and `/watch-mse.js` HTTP 200 with all required lifecycle markers.
3. Two RTSP sources appear as perception devices/cards.
4. RTSP cards do not expose MIoT-only controls.
5. Every required live/management player reaches at least `readyState=2`, has non-zero video dimensions, has no media error, and advances playback time.
6. The saved OpenAI Responses profile activates successfully.
7. Visual preflight succeeds using the 30-second Responses POST timeout.
8. One real visual inference succeeds.
9. The container completes at least ten minutes of simultaneous real RTSP and model load with:
   - `RestartCount=0`;
   - `OOMKilled=false`;
   - no Docker or kernel OOM event;
   - no sustained monotonic memory growth across the final observation samples;
   - peak cgroup `memory.current` below 2.5 GiB, leaving at least approximately 512 MiB below the 3 GiB limit.
10. Evidence contains only structural status, timings, memory/counter values, video state/dimensions, and SHA bindings.

The ten-minute and 2.5 GiB requirements are veto gates, not reporting-only metrics. A failure blocks acceptance and triggers the offline rollback path.

## 12. Rollback

The old `d969` release is known to OOM under the saved two-camera configuration. It must not be restarted and reported as a healthy rollback.

Rollback triggers include:

- standard preflight failure;
- deployment/fixture/verify failure;
- wrong SHA or image binding;
- health or watch-asset failure;
- any new restart or OOM;
- peak memory at or above 2.5 GiB;
- continued monotonic memory growth;
- RTSP perception or playback regression;
- Responses activation, visual preflight, or real-inference failure.

If failure occurs before mutation, production remains stopped. If the deployment controller automatically restores `d969`, the same CO must immediately stop that restored container after collecting bounded structural status. The final safe rollback state is:

- no Miloco container running;
- saved configuration/state preserved;
- all old and new releases and proof records preserved;
- exact current/previous controller state reported accurately;
- no cleanup, same-SHA rebuild, configuration rewrite, or credential access.

The CO closes according to the actual outcome. Production service remains unavailable until a corrected new candidate passes the complete rollout again.

## 13. Implementation boundaries

Expected runtime ownership is limited to:

- `backend/miloco/src/miloco/perception/collect/camera_adapter.py` for RTSP-only perception admission and lifecycle reset;
- `backend/miloco/src/miloco/perception/collect/stream_buffer.py` for optional frame/byte retention policy and accounting;
- `backend/miloco/src/miloco/camera/stream.py` for per-feed latest-frame scheduling;
- `backend/miloco/src/miloco/camera/transcoder.py` for admission-before-copy and bounded timestamp history;
- corresponding focused backend tests;
- progress/spec/plan evidence required by the approved workflow.

If implementation evidence proves a different file is required to connect one of these exact boundaries, the implementation plan must name that file and justify it before code changes. No model/provider, deployment-controller behavior, Web UI, MIoT, authentication, or unrelated refactor is authorized by this specification.

## 14. Completion definition

The repair is complete only when:

- all specified TDD and repository gates pass;
- independent code and artifact reviews have no open Critical or Important findings;
- the same immutable archive passes ai-lab01 and then ai-lab02;
- a new production CO reaches Implement with active PAM and exact-source verification;
- real two-camera RTSP playback, perception visibility, Responses activation/preflight, and one real inference pass;
- the ten-minute 3 GiB production memory/restart/OOM acceptance passes;
- the production CO is closed accurately;
- final documentation distinguishes code SHA, deployed SHA, and any later docs-only HEAD;
- no secret, RTSP credential, camera image, provider body, or inference content is stored in evidence, commits, or chat.
