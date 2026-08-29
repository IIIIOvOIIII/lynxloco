# Miloco production runtime repair design

**Status:** Scheme A approved; written specification awaiting user review  
**Date:** 2026-08-29  
**Diagnostic CO:** `CHG260829004` — Successfully Closed, read-only  
**Observed production SHA:** `5fd053e620636c32dd0f7c439d25bd40a743d965`  
**Target branch:** `feature/rtsp-responses-support`

## 1. Purpose

This design repairs three production symptoms found after the first Miloco deployment to `docker.esxi`:

1. enabled RTSP cameras are active in the perception engine but are absent from the dashboard's perception count and live-perception cards;
2. RTSP browser players authenticate and receive valid H.264 access units but remain at “已连接摄像头，等待画面…”;
3. the Omni connection test can be green while real visual inference is rejected, leading to contradictory and misleading model status.

The three repairs ship as one immutable release because they jointly define whether an RTSP source can be discovered, viewed, and analysed. Their internal boundaries remain separate so each failure can be tested and rolled back without changing the others.

## 2. Confirmed evidence and failure boundaries

### 2.1 Perception-device visibility

The production backend returned both enabled RTSP sources from `/api/cameras` and `/api/perception/devices`. The perception engine reported both as active and processed their frames. The dashboard nevertheless displayed `0 个在感知`.

The first failing boundary is frontend composition. The dashboard builds its perception count and live cards from MIoT-only `scopeCameras`, while RTSP sources are shown only in a separate source-management section.

This is not a backend registry failure. The repair must not alter `/api/miot/scope/cameras` or make RTSP sources pretend to be Xiaomi devices.

### 2.2 Browser live video

For both sources, the production browser session established the authenticated WebSocket and received H.264 Annex-B data. The first key access unit contained SPS, PPS, SEI, and IDR NAL units; the declared codec was supported by Chrome MediaSource.

Because the current deployment is reached by direct HTTP, WebCodecs `VideoDecoder` is unavailable and the player selects the MSE/JMuxer fallback. The video elements remained uninitialised: no buffered range, no dimensions, no playback progress.

The first failing boundary is a JMuxer readiness race. The current player feeds the first key access unit immediately after construction, before JMuxer's MediaSource has completed `sourceopen`. JMuxer v2.0.5 can drop the initialisation and media segments in that interval and does not replay them later.

This is not an RTSP connectivity, codec, authentication, or server-WebSocket failure.

### 2.3 Omni model status

The active model profile used Chat Completions. A text-only request passed, but real perception requests containing visual input were rejected with HTTP 400 until the configuration circuit opened. The same saved model identifier, Base URL, and key passed a real image request through the Responses protocol.

The first failing boundary is capability equivalence. The green test checks text connectivity, while runtime health reflects a visual request through a different payload contract. The current error copy overemphasises the model name and API key even when endpoint reachability and authentication have already been proved.

This is not evidence that the saved key or model identifier is invalid.

## 3. Goals

The release must:

- display every camera returned by the perception-device API in a source-neutral live-perception area;
- preserve source-specific management semantics for MIoT and RTSP;
- start the MSE/JMuxer decoder only after it is ready to accept the first access unit;
- prevent stale asynchronous decoder callbacks from affecting a replaced or closed player;
- make the model connection test use a real visual payload and the selected runtime protocol;
- distinguish visual/protocol rejection from invalid credentials in backend health and frontend copy;
- make current runtime health authoritative over an older successful test result;
- preserve credentials and camera-frame privacy;
- validate the repair locally, in the lab, and under a later exact-SHA production CO.

## 4. Non-goals and fixed constraints

This release does not:

- change RTSP source lifecycle, encryption-at-rest, secret redaction, or credential storage;
- change `/api/miot/scope/cameras` semantics or add RTSP rows to MIoT APIs;
- add recording, NVR, event replay, camera audio, or audio input to the model;
- add Apache, HTTPS, a certificate, SmartDNS, or a new FQDN;
- change Xiaomi account integration or MIoT device discovery;
- send RTSP URLs, camera credentials, API keys, raw frames, or frame-derived payloads to logs;
- copy, reveal, or automatically migrate the existing model API key during deployment;
- reintroduce automatic image-retention deletion or same-SHA image rebuilding;
- push to the Xiaomi upstream repository without an authorised writable remote;
- mutate production under the closed diagnostic CO.

Direct HTTP remains a supported deployment mode, so the MSE fallback is a required path rather than a temporary workaround.

## 5. Architecture overview

The repair has three independently testable units:

```text
/api/perception/devices ─┐
/api/cameras ────────────┼─> unified perception view model ─> HeroNow
MIoT scope cameras ──────┘          │
                                    └─> source-specific controls

RTSP WebSocket ─> key access unit ─> wait for JMuxer ready ─> first feed ─> MSE video

saved model profile ─> protocol-specific visual preflight ─> typed health result ─> UI status
                              │
                              └─> same adapter contract used by runtime analysis
```

No new cross-service component is introduced. The existing camera registry, WebSocket relay, provider adapters, and SSE health stream remain the system boundaries.

## 6. Unified perception display

### 6.1 Authoritative data and joins

`/api/perception/devices` is authoritative for which devices are currently available to perception. Its camera rows determine the count and membership of the live-perception area.

`/api/cameras` enriches each perception device by exact camera ID with source type and connection metadata. MIoT scope data enriches MIoT cameras with the existing Xiaomi controls. The join must use exact identifiers; it must not infer source type from names, prefixes, rooms, URLs, or display labels.

The frontend builds a small view model with these semantics:

```text
id              exact perception camera identifier
name            perception-device name, with camera-summary fallback
roomName        perception-device room, with camera-summary fallback
sourceType      miot, rtsp, or unavailable when enrichment is stale
connected       current camera-summary connectivity when available
miotScope       matching MIoT scope row, if and only if sourceType is miot
```

If enrichment is temporarily unavailable, the perception device remains counted and visible with its API-provided name and room. It receives no source-specific controls, and the existing stale/error indication remains visible. This prevents a secondary metadata failure from turning active perception devices into a false zero count.

Duplicate identifiers are collapsed by exact ID. The first authoritative perception row wins for presentation fields, and enrichment is applied once. This prevents duplicate cards without hiding distinct cameras.

### 6.2 Dashboard behaviour

The live-perception count equals the number of unique camera devices returned by the successful perception-device response. It is no longer derived from MIoT scope membership.

The live-perception area renders both MIoT and RTSP cards:

- RTSP cards have a visible RTSP source badge and open the existing live player;
- MIoT cards preserve the current MIoT display and controls;
- a temporarily unenriched camera remains visible but has no misleading RTSP or MIoT badge;
- the existing RTSP management section remains the only place for add, edit, enable, disable, and delete operations.

The MIoT all-on/all-off actions, voice targeting, feed controls, and other Xiaomi-specific operations continue to receive only MIoT scope rows. RTSP cameras must never be added to those command target lists.

### 6.3 Fetch and stale-state rules

The existing request cadence and authentication model are retained. A successful but empty `/api/perception/devices` response legitimately displays zero. A failed request must not be converted into an empty success; the existing stale/error state remains visible and the last confirmed data may remain on screen according to current application behaviour.

The change does not add a new backend endpoint or polling loop.

## 7. MSE/JMuxer readiness repair

### 7.1 Decoder lifecycle

The MSE fallback follows this state sequence:

```text
idle
  └─ create JMuxer instance
       ├─ onReady before timeout ─> ready ─> feed first access unit exactly once
       ├─ timeout ────────────────> failed ─> destroy instance and show recoverable error
       └─ teardown/switch ────────> cancelled ─> ignore all later callbacks
```

No H.264 data is fed before `onReady`. The first SPS/PPS/IDR-containing access unit is retained only in local JavaScript memory while readiness is pending. It is released after the first feed, timeout, or teardown.

Subsequent access units use the existing feed path only after the decoder is ready.

### 7.2 Testable helper boundary

A small dependency-free ES module under `web/public` owns JMuxer construction and readiness. Its public contract returns a promise that resolves to a ready muxer instance and rejects on a bounded timeout or cancellation. Vitest can import the module directly and supply a fake JMuxer constructor.

The helper owns:

- installing the JMuxer `onReady` callback before returning control;
- resolving readiness only once;
- enforcing the timeout;
- destroying a partially created muxer on failure or cancellation;
- ignoring late readiness callbacks after settlement.

The page owns:

- the current camera/player generation number;
- the pending first access unit;
- verifying that the resolved helper still belongs to the current generation;
- feeding the first unit exactly once;
- all later feeds and visible player state.

This separation makes the race unit-testable without emulating a real browser MediaSource implementation.

### 7.3 Replacement and teardown safety

Every camera selection or player restart increments a generation token. An asynchronous ready result may change player state or feed data only when its captured token still equals the current token and the WebSocket/player has not been closed.

On camera switch, component teardown, WebSocket terminal failure, or fallback replacement:

- cancel the pending readiness operation;
- destroy the JMuxer instance if present;
- clear the pending first access unit;
- remove transient playback state;
- ignore any late callback from the old instance.

This prevents an old decoder from consuming a new camera's frame or attaching itself to a replaced video element.

### 7.4 Playback and error behaviour

After the first buffer becomes usable, the page requests muted inline playback using the existing video element. A rejected `play()` promise is handled as a user-visible playback state, not an unhandled exception. The user may still start playback manually when browser policy requires it.

Readiness timeout produces a bounded, recoverable message that recommends retrying the stream. It must not claim that the RTSP source or credentials are invalid. Codec-unsupported and H.265/WebCodecs limitations retain their distinct existing messages.

The WebCodecs path is unchanged. H.265 handling is unchanged.

## 8. Runtime-equivalent Omni visual preflight

### 8.1 Preflight contract

The connection test validates the selected provider protocol with an actual image-bearing request. A text-only success is insufficient to produce a green result.

The probe uses a generated in-memory synthetic image with no camera content and asks for a short deterministic acknowledgement. It uses the saved model identifier, Base URL, API key, timeout policy, and selected protocol through the same provider adapter family used by runtime analysis.

For `openai_responses`, the probe uses the Responses image-input contract. If Chat Completions remains selectable, its probe uses that protocol's supported visual payload rather than a text ping. Other existing provider types retain their provider-specific implementation but must satisfy the same “visual request succeeded” meaning before returning green.

The synthetic image and provider response are not persisted. Request bodies, image bytes, authorisation headers, and response bodies are not logged.

### 8.2 Typed result semantics

Probe and runtime provider failures use typed health codes. The following distinction is required:

- authentication rejection: credentials were rejected by the endpoint;
- model-not-found or unsupported model: the endpoint explicitly rejected the model identifier;
- `visual_payload_rejected`: the endpoint was reached but rejected the selected protocol's image-bearing request;
- transport or timeout failure: the endpoint could not be reached or did not respond in time;
- provider/internal failure: the provider returned a server-side or unclassified error;
- success: the visual request returned a valid response.

HTTP 400 or 422 from an authenticated multimodal request must not default to copy that says the API key is probably wrong. The provider adapter supplies the request context needed to classify it as visual/protocol rejection unless the body contains an explicit safe authentication or model error signal.

The UI copy for `visual_payload_rejected` states that the endpoint is reachable but the selected protocol or visual payload is unsupported. It recommends checking the protocol/capability, not replacing the key as the first action.

### 8.3 Current health versus historical test result

The connection-test result and SSE runtime health are separate observations with timestamps or ordering. Current runtime health is authoritative for the active profile.

Rules:

- a newly completed visual test may show its own result;
- if a later runtime health event reports error or an open circuit for the active profile, the UI must not continue presenting the older green test as the current overall state;
- editing identity fields such as provider type, protocol, Base URL, or model clears the prior test result;
- saving a profile does not manufacture a green result;
- a successful runtime request follows the existing breaker-success path and resets failure state.

This removes the contradictory “connection normal” plus “configuration invalid” presentation without hiding either the last test or current runtime evidence.

### 8.4 Production protocol switch

The confirmed compatible production setting is `openai_responses` with the existing model identifier, Base URL, and API key. The source change makes that configuration test meaningful; it does not silently rewrite the deployed profile.

During the later production CO, the active profile must be explicitly saved with `openai_responses`. The deployment script must not read, extract, print, or copy the current API key. The same key must be re-entered by the user through the authenticated UI, or supplied through a separately approved Vault-backed workflow. Without that authorised credential step, the code deployment may proceed but production model acceptance remains not measured and must not be reported as fixed.

## 9. Security and privacy

The existing security boundary remains mandatory:

- RTSP credentials stay server-side and encrypted at rest;
- browser clients receive short-lived authenticated stream access, not RTSP URLs;
- model API keys remain write-only to the browser after submission;
- source IDs may appear in structured health data, but URLs, credentials, frames, auth headers, and response bodies must not appear in logs or documentation;
- tests use synthetic credentials, fake frames, and mocked endpoints;
- the visual preflight uses a generated synthetic image rather than a real camera frame;
- camera audio is neither relayed to the browser decoder nor sent to the model by this change.

The exact-identity credential-reuse restrictions are not weakened. Changing protocol therefore requires an explicit key submission rather than implicit reuse across identities.

## 10. Verification design

### 10.1 Frontend unit tests

Tests must prove:

- a perception response containing one MIoT and one RTSP camera yields a count of two and two live cards;
- an RTSP card is classified by exact `/api/cameras` ID and displays the RTSP badge;
- an unenriched perception camera remains counted and rendered without source-specific controls;
- MIoT bulk and command targets exclude RTSP cameras;
- duplicate exact IDs produce one card;
- a perception fetch error is not displayed as a successful zero-device state;
- an older green test result is superseded by a later active-profile runtime error;
- editing a profile identity field clears the prior test result;
- `visual_payload_rejected` renders protocol/capability guidance rather than credential guidance.

### 10.2 MSE readiness unit tests

With a fake JMuxer, tests must prove:

- no data is fed before `onReady`;
- the first access unit is fed once after readiness;
- readiness timeout rejects and destroys the partial instance;
- cancellation destroys the instance and ignores a late callback;
- an old generation cannot feed after a camera switch;
- normal subsequent access units feed only through the ready instance;
- a failed autoplay promise is handled.

### 10.3 Backend provider tests

Tests must prove:

- the Responses preflight sends a synthetic image through the Responses contract;
- a successful text-only endpoint cannot make the visual preflight green;
- the Chat Completions preflight, while supported, includes visual input;
- the probe never uses a camera frame;
- authenticated HTTP 400/422 on visual input maps to `visual_payload_rejected` unless an explicit safer classification applies;
- explicit authentication and model-not-found responses retain their own codes;
- success resets the existing circuit-breaker state;
- logs and serialised health results contain no key, auth header, image bytes, RTSP URL, or provider response body.

### 10.4 Repository and CI verification

Run the repository's focused frontend and backend tests first, followed by the existing full local CI entry point. Formatting, lint, type checking, builds, and tests must pass at the new immutable commit. Test count is not an acceptance metric; the required behaviours above are.

## 11. Lab acceptance

The exact candidate SHA is deployed through the repository's existing lab profile to `ai-lab01.esxi` and `ai-lab02.esxi`. Lab acceptance is split into fixture evidence and real integration evidence:

- fixture acceptance proves mixed MIoT/RTSP perception aggregation, MSE readiness ordering, typed model errors, and no regressions in existing UI/API tests;
- browser acceptance over direct HTTP proves the MSE path displays advancing video rather than only a connected WebSocket;
- provider acceptance uses a compatible internal Responses endpoint and a synthetic visual probe;
- real RTSP or local-VLM acceptance is claimed only when the corresponding non-secret lab configuration is actually available.

The deployed SHA, container health, relevant browser state, probe result code, and rollback command are recorded without secrets. Lab success does not substitute for production proof.

## 12. Production release, acceptance, and rollback

Production mutation requires a new CO naming `docker.esxi`, the exact candidate SHA, the chosen port/current service, the active profile protocol change, validation steps, and rollback.

The release uses the existing immutable image/SHA workflow. It does not rebuild an existing SHA and does not automatically delete retained images.

Production acceptance requires all of the following:

1. container health reports the exact candidate SHA;
2. `/api/perception/devices` and the dashboard show the same unique camera count for the configured sources;
3. each enabled RTSP camera displays advancing browser video over the actual direct-HTTP access path;
4. the active model profile is explicitly saved as `openai_responses` with authorised credential submission;
5. the visual connection test succeeds through Responses;
6. a real perception inference succeeds and current runtime health remains healthy after the request;
7. logs contain no new secret or frame leakage.

Rollback restores the prior immutable image/SHA and its compatible configuration snapshot through the repository deploy workflow. If only the model profile switch fails, restore the previous protocol/profile configuration without changing camera state. Rollback must not delete RTSP sources, Xiaomi account data, or stored credentials.

## 13. Implementation decomposition

The later implementation plan will keep these code changes reviewable and sequential:

1. unified perception view-model tests and frontend composition;
2. JMuxer readiness helper tests and player integration;
3. visual preflight/error-classification tests and backend implementation;
4. model-status UI tests and presentation changes;
5. full local verification, documentation, immutable commit, lab deployment, and evidence review;
6. separate user-approved production CO and exact-SHA rollout.

Each behavioural change begins with a failing test. Production rollout is not part of code implementation approval and remains separately gated.

## 14. Acceptance criteria

The design is implemented successfully when:

- RTSP and MIoT cameras active in `/api/perception/devices` appear together in the live-perception count and cards;
- no RTSP camera is passed to MIoT-only command paths;
- an enabled H.264 RTSP source displays moving video through the MSE fallback over direct HTTP;
- the first MSE access unit cannot be lost to pre-readiness feeding, including under switch, timeout, and teardown races;
- a green model test proves an image-bearing request through the selected runtime protocol;
- protocol/visual rejection is reported distinctly from authentication or model errors;
- current runtime failure supersedes an obsolete green connection indicator;
- production uses Responses only after explicit authorised profile submission;
- the existing security, deployment immutability, and source-lifecycle boundaries remain intact;
- local CI, lab evidence, and later production acceptance are reported as separate evidence levels.

## 15. Resolved decisions

There are no unresolved design decisions in this specification. Scheme A fixes frontend aggregation rather than backend registries, gates the first MSE feed on JMuxer readiness, and replaces text connectivity with protocol-specific visual preflight. Credential submission and production mutation remain explicit actions under a later CO.
