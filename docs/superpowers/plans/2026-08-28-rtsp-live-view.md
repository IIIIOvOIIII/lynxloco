# RTSP Live View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the RTSP user-facing loop with a unified camera list and low-latency browser viewing, using H.264 pass-through when safe and one viewer-driven software H.264 transcoder for H.265 or incompatible H.264.

**Architecture:** Reuse each source's existing `RtspSession` packet/frame fan-out; never open a viewing-only RTSP connection. Add a source-neutral live stream hub, normalize compatible H.264 packets to Annex B, and lazily start one shared PyAV/libx264 transcoder only while viewers require it. Keep legacy MIoT routes operational while moving the bundled viewer and React entry points to generic camera routes.

**Tech Stack:** Python, FastAPI WebSocket, PyAV/FFmpeg, asyncio, jMuxer, React, TypeScript, Vitest, pytest.

**Spec:** [RTSP 摄像机与 OpenAI Responses 本地 Omni 支持设计](../specs/2026-08-28-rtsp-responses-support-design.md)

**Prerequisite:** Complete [RTSP Perception Foundation Implementation Plan](2026-08-28-rtsp-perception-foundation.md), including `CameraService`, `RtspCameraSource`, `RtspSession`, and its packet listener.

## Global Constraints

- Do not add WebRTC, a media gateway, recording, playback, ONVIF, PTZ, or hardware-acceleration promises.
- Per source: one RTSP input session, at most one software transcoder, any number of bounded viewer queues.
- H.265 decoding for perception continues whether viewers exist or not; H.265-to-H.264 encoding exists only while at least one viewer is attached.
- A slow/disconnected viewer drops old output locally and cannot backpressure perception or other viewers.
- Transcode failure changes only live-view state; perception remains connected.
- Existing `/api/miot/...` endpoints and MIoT browser viewing remain backward compatible.
- Browser-view audio for RTSP is outside v1. Optional RTSP audio remains available to local energy/VAD only.
- Cross-platform acceptance is functional and resource-bounded. CPU, latency, sustainable fps, and concurrency are measurements, not universal veto gates.
- Commit each task locally; do not push without an approved writable remote.

---

## Task 1: Add a bounded source-neutral live-stream hub

**Files:**

- Create: `backend/miloco/src/miloco/camera/stream.py`
- Modify: `backend/miloco/src/miloco/camera/service.py`
- Modify: `backend/miloco/src/miloco/perception/collect/rtsp_session.py`
- Test: `backend/miloco/tests/camera/test_stream_hub.py`
- Modify/Test: `backend/miloco/tests/perception/collect/test_rtsp_session.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class EncodedVideoPacket:
    codec: Literal["h264", "hevc"]
    data: bytes
    pts: int | None
    dts: int | None
    is_keyframe: bool
    time_base_num: int
    time_base_den: int
    extradata: bytes = b""


@dataclass(frozen=True)
class LiveStreamState:
    viewer_count: int
    mode: Literal["idle", "passthrough", "transcoding", "error"]
    input_codec: str | None
    output_codec: str | None
    queue_depth: int
    dropped_packets: int
    error_code: str | None


class LiveStreamHub:
    async def subscribe(self, camera_id: str) -> AsyncIterator[bytes]: ...
    async def close_camera(self, camera_id: str) -> None: ...
    def state(self, camera_id: str) -> LiveStreamState: ...
```

- [ ] Add failing tests for multiple subscribers, per-subscriber bounded queues, drop-oldest behavior, detach cleanup, source shutdown, and exception isolation.
- [ ] Add a failing `RtspSession` test proving demuxed packet listeners and decoded perception callbacks receive the same source session without a second `av.open`.
- [ ] Run `cd backend && uv run pytest miloco/tests/camera/test_stream_hub.py miloco/tests/perception/collect/test_rtsp_session.py -q` and confirm failure.
- [ ] Implement immutable packet snapshots at the RTSP demux boundary; copy only packet bytes and metadata before the PyAV container/thread releases packet objects.
- [ ] Let `CameraService` resolve source type and obtain either the existing MIoT stream backend or the existing RTSP session. The hub must not know credentials or create sessions.
- [ ] Use subscriber queues with a default maximum of 8 packets. On overflow, discard the oldest entries until a keyframe boundary is retained or request a fresh encoder keyframe in transcode mode.
- [ ] Run focused tests, Ruff, and ty.
- [ ] Commit: `git add backend/miloco/src/miloco/camera/stream.py backend/miloco/src/miloco/camera/service.py backend/miloco/src/miloco/perception/collect/rtsp_session.py backend/miloco/tests/camera/test_stream_hub.py backend/miloco/tests/perception/collect/test_rtsp_session.py && git commit -m "feat(camera): add shared live stream hub"`

## Task 2: Normalize browser-compatible H.264 without re-encoding

**Files:**

- Create: `backend/miloco/src/miloco/camera/h264.py`
- Test: `backend/miloco/tests/camera/test_h264.py`
- Fixture: `backend/miloco/tests/fixtures/rtsp/h264_avcc_packets.bin`
- Fixture: `backend/miloco/tests/fixtures/rtsp/h264_annexb_packets.bin`

**Interfaces:**

```python
@dataclass(frozen=True)
class H264Compatibility:
    passthrough: bool
    profile: int | None
    level: int | None
    reason: str


class H264AnnexBNormalizer:
    def inspect(self, packet: EncodedVideoPacket) -> H264Compatibility: ...
    def push(self, packet: EncodedVideoPacket) -> list[bytes]: ...
    def decoder_config(self) -> bytes: ...
```

- [ ] Add failing tests for Annex B input, AVCC length-prefixed conversion, SPS/PPS extraction from extradata, SPS/PPS injection before the first IDR for a new viewer, malformed NAL rejection, non-H.264 rejection, and deterministic compatibility reasons.
- [ ] Run `cd backend && uv run pytest miloco/tests/camera/test_h264.py -q` and confirm failure.
- [ ] Implement a minimal parser that never changes slice payload bytes. Do not depend on private PyAV classes.
- [ ] Pass through only AVC streams with valid SPS/PPS and a browser-safe profile set already supported by the bundled jMuxer path. Return `passthrough=False` for unknown/malformed configuration so Task 3's transcoder handles it.
- [ ] Ensure a viewer joining between keyframes receives decoder configuration and waits for an IDR rather than seeing undecodable P-frames.
- [ ] Run focused tests, Ruff, and ty.
- [ ] Commit: `git add backend/miloco/src/miloco/camera/h264.py backend/miloco/tests/camera/test_h264.py backend/miloco/tests/fixtures/rtsp/h264_avcc_packets.bin backend/miloco/tests/fixtures/rtsp/h264_annexb_packets.bin && git commit -m "feat(camera): normalize H264 live packets"`

## Task 3: Add a viewer-driven shared software transcoder

**Files:**

- Create: `backend/miloco/src/miloco/camera/transcoder.py`
- Modify: `backend/miloco/src/miloco/camera/stream.py`
- Test: `backend/miloco/tests/camera/test_transcoder.py`
- Modify/Test: `backend/miloco/tests/camera/test_stream_hub.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class TranscodeConfig:
    max_width: int = 1280
    max_height: int = 720
    fps: int = 15
    bitrate: int = 2_000_000
    queue_size: int = 2


class SharedH264Transcoder:
    async def attach(self) -> AsyncIterator[bytes]: ...
    async def detach(self) -> None: ...
    async def push_frame(self, frame: NDArray[np.uint8], pts: int | None) -> None: ...
    async def stop(self) -> None: ...
    @property
    def viewer_count(self) -> int: ...
```

- [ ] Add failing tests for HEVC input selection, incompatible-H.264 selection, first-viewer start, second-viewer reuse, last-viewer stop, restart after later attach, bounded input queue, stale-frame drop, encoder error state, and perception callback continuation after encoder failure.
- [ ] Add a synthetic-frame test that decodes emitted Annex B H.264 and confirms dimensions, monotonically ordered timestamps, and periodic keyframes.
- [ ] Run the focused tests and confirm failure.
- [ ] Implement encoding in a worker thread with PyAV `libx264`, `yuv420p`, `zerolatency`, a short GOP, and no B-frames. Constrain output dimensions while preserving aspect ratio and even pixel sizes.
- [ ] Do not initialize codec state until the first viewer attaches. Close/drain the codec and clear queues synchronously when the final viewer detaches.
- [ ] Feed the transcoder from already decoded RTSP frames; do not decode the same HEVC packet twice. For incompatible H.264, reuse the decoded perception frame path.
- [ ] Add ref-count and generation guards so a late worker result from an old generation cannot enter a newly started viewer queue.
- [ ] Run focused tests, Ruff, and ty.
- [ ] Commit: `git add backend/miloco/src/miloco/camera/transcoder.py backend/miloco/src/miloco/camera/stream.py backend/miloco/tests/camera/test_transcoder.py backend/miloco/tests/camera/test_stream_hub.py && git commit -m "feat(camera): transcode RTSP video on demand"`

## Task 4: Expose the unified WebSocket and migrate the bundled viewer

**Files:**

- Modify: `backend/miloco/src/miloco/camera/router.py`
- Modify: `backend/miloco/src/miloco/manager.py`
- Modify: `web/public/watch.html`
- Test: `backend/miloco/tests/camera/test_camera_stream_router.py`
- Test: `web/tests/watch-page.test.ts`

**Endpoint contract:**

```text
GET /api/cameras/{camera_id}/watch
WS  /api/cameras/{camera_id}/stream?token=<optional-existing-auth-token>
```

The WebSocket sends binary Annex B H.264 chunks only. It closes with a stable policy/error code if the camera is missing, disabled, unavailable, or the live codec path fails.

- [ ] Add failing backend tests for authenticated upgrade, MIoT routing, RTSP pass-through, RTSP transcode, missing/disabled camera close codes, subscriber detach on disconnect, and stream error isolation.
- [ ] Add failing web tests asserting the watch page builds only ``/api/cameras/${encodeURIComponent(cameraId)}/stream``, never interpolates an unencoded ID, and does not contain the legacy MIoT path.
- [ ] Run the two focused suites and confirm failure.
- [ ] Implement the watch HTML route by serving the existing asset with the camera ID provided as a URL/query parameter. Do not duplicate the viewer page per source type.
- [ ] Replace the hardcoded MIoT WebSocket URL in `watch.html` with the generic endpoint. Keep the existing jMuxer feeding and reconnect UI, but surface stable backend error messages without raw connection detail.
- [ ] On WebSocket exit, close the async iterator in `finally` so the last-viewer transcode stop happens even on abrupt browser disconnect.
- [ ] Keep legacy `/api/miot/watch` and `/api/miot/ws/video_stream` registered and unchanged for old clients.
- [ ] Run focused tests, existing MIoT streaming tests, Ruff/ty, and `cd web && npm run typecheck`.
- [ ] Commit: `git add backend/miloco/src/miloco/camera/router.py backend/miloco/src/miloco/manager.py backend/miloco/tests/camera/test_camera_stream_router.py web/public/watch.html web/tests/watch-page.test.ts && git commit -m "feat(camera): expose unified live viewing"`

## Task 5: Add RTSP source management to the web camera surface

**Files:**

- Modify: `web/src/lib/types.ts`
- Modify: `web/src/api/real.ts`
- Create: `web/src/lib/rtspCamera.ts`
- Create: `web/src/components/RtspCameraDialog.tsx`
- Modify: `web/src/App.tsx`
- Modify: `web/src/components/HeroNow.tsx`
- Modify: `web/src/components/LivePlayerPlaceholder.tsx`
- Modify: `web/src/i18n/locales/zh/*.json`
- Modify: `web/src/i18n/locales/en/*.json`
- Test: `web/tests/rtsp-camera-api.test.ts`
- Test: `web/tests/rtsp-camera-form.test.ts`
- Modify/Test: relevant existing live-player test

**Types:**

```typescript
export type CameraSourceType = "miot" | "rtsp";

export interface CameraSummary {
  id: string;
  source_type: CameraSourceType;
  name: string;
  room_name: string;
  enabled: boolean;
  connected: boolean;
  video_codec: string | null;
  audio_codec: string | null;
  has_password: boolean;
  error_code: string | null;
  error_message: string | null;
}

export interface RtspSourceInput {
  name: string;
  room_name: string;
  uri: string;
  username: string;
  password: string;
  transport: "tcp" | "udp";
  audio_enabled: boolean;
}
```

- [ ] Add failing API mapping tests for list/test/create/edit/enable/disable/delete, including exact HTTP paths and redacted response types.
- [ ] Add failing pure form-validation tests for scheme/host/userinfo, password-preservation semantics, default disabled create, test-before-enable, and localization keys. Avoid introducing a new DOM test framework solely for this form.
- [ ] Run `cd web && npm test -- tests/rtsp-camera-api.test.ts tests/rtsp-camera-form.test.ts` and confirm failure.
- [ ] Implement typed API helpers and a dialog that supports add/edit/test. The password field is always blank on load; blank edit means preserve. Never place the password in browser logs, error telemetry, URL, or local storage.
- [ ] Change `App.tsx` to load the generic `GET /api/cameras` list and pass it to `HeroNow`. In `HeroNow`, display source badge, enabled/connected state, safe codec information, and safe error text. Provide explicit enable/disable/delete actions with confirmation for deletion while keeping MIoT-only scope/prompt/voice controls hidden for RTSP rows.
- [ ] Point `LivePlayerPlaceholder` at `/api/cameras/${encodeURIComponent(cameraId)}/watch` for both MIoT and RTSP.
- [ ] Add concise Chinese and English strings for RTSP URL, room, username, password-preservation hint, transport, audio gate, test, enable, disable, offline, reconnecting, and live-transcode state.
- [ ] Run focused Vitest, full `npm test`, `npm run typecheck`, and `npm run build`.
- [ ] Commit: `git add web/src/App.tsx web/src/lib/types.ts web/src/api/real.ts web/src/lib/rtspCamera.ts web/src/components/HeroNow.tsx web/src/components/RtspCameraDialog.tsx web/src/components/LivePlayerPlaceholder.tsx web/src/i18n web/tests && git commit -m "feat(web): manage and view RTSP cameras"`

## Task 6: Measure viewing behavior and close the batch

**Files:**

- Create: `scripts/rtsp-view-smoke.sh`
- Create: `backend/miloco/tests/integration/test_rtsp_live_view.py`
- Modify: `docs/2026-08-28-rtsp-responses-support_PROGRESS.md`
- Modify: `docs/superpowers/specs/2026-08-28-rtsp-responses-support-design.md`

- [ ] Add an integration test for `fixture session -> perception callback + live hub -> WebSocket`, covering H.264 pass-through and H.265 transcode. Assert exactly one source open, bounded queues, and transcode shutdown after the last viewer.
- [ ] Add a local/lab smoke script that accepts only camera ID and backend URL; credentials remain in persisted owner-only config and are never accepted on the command line. Measure first-frame latency, 30-second output fps, process CPU delta, viewer count, and queue drops.
- [ ] Run `cd backend && uv run pytest miloco/tests/camera miloco/tests/integration/test_rtsp_live_view.py -q`.
- [ ] Run `cd web && npm test && npm run typecheck && npm run build`.
- [ ] Run `./scripts/local-ci.sh --tests` and then `./scripts/local-ci.sh`. Record platform-dependent/pre-existing failures separately.
- [ ] With a lab H.264 camera, verify the hub reports `passthrough`, live video renders, and no transcoder exists. With a lab H.265 camera, verify one shared transcoder exists only while viewers are open. If either codec source is unavailable, mark that real-camera branch `not_measured`; fixture success is not a substitute.
- [ ] Record measured hardware, CPU, latency, fps, concurrent viewers, and drop count. Do not turn those observations into a universal guarantee.
- [ ] Update the design status to include `RTSP 实时预览已实施` only after mandatory automated gates pass and at least the fixture WebSocket E2E succeeds.
- [ ] Commit: `git add scripts/rtsp-view-smoke.sh backend/miloco/tests/integration/test_rtsp_live_view.py docs/2026-08-28-rtsp-responses-support_PROGRESS.md docs/superpowers/specs/2026-08-28-rtsp-responses-support-design.md && git commit -m "test(camera): verify RTSP live view"`

## Completion Gate

- Existing MIoT routes still pass their tests.
- The generic camera viewer handles MIoT and RTSP IDs.
- H.264 pass-through emits valid Annex B with decoder config/keyframe startup.
- H.265 and incompatible H.264 share one viewer-driven transcoder per source.
- Zero viewers means zero RTSP transcode work; last detach releases codec and queues.
- Slow viewers cannot grow memory or stall perception.
- Frontend management never receives/stores an existing password.
- Fixture E2E passes; real-camera H.264/H.265 measurements are reported or explicitly `not_measured`.
