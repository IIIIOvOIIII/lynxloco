# RTSP Status Ribbon and Toggle UX Design

## Goal

Make the Miloco dashboard reflect the true active perception state when RTSP cameras are enabled, and let users enable or disable RTSP perception from the same home dashboard area where MIoT camera perception is controlled.

## Background

Production read-only diagnosis on `miloco.esxi` under `CHG260830026` showed that Miloco was already watching through two RTSP cameras: the perception engine was running and ready, the active source list contained two RTSP camera sources, both RTSP camera summaries were enabled and connected, and the active Omni profile was healthy. The dashboard still showed `待机中` because the top status ribbon receives `allCamerasOff` from MIoT scope cameras only. The sole MIoT camera was offline and not `in_use`, so the frontend treated all cameras as off even though RTSP cameras were active.

## User-Facing Behavior

The top ribbon should show `在看家` whenever the perception engine is running and ready and at least one camera source is currently feeding perception. Camera sources include:

- MIoT scope cameras whose `inUse` is true.
- RTSP camera summaries whose `sourceType` is `rtsp`, `enabled` is true, and `connected` is true.

The ribbon should show `待机中` only when the engine is running and ready but no camera source is actively feeding perception. Empty MIoT scope camera lists are still supported; if there are no MIoT cameras but one RTSP camera is enabled and connected, the ribbon must show `在看家`.

RTSP cards in the dashboard should expose a perception enable/disable switch:

- Enabled RTSP camera: switch is on, user can turn it off.
- Disabled RTSP camera: switch is off, user can turn it on.
- Enabling an RTSP camera reuses the existing backend flow `POST /api/cameras/{id}/enable`; the backend probes the source before persisting enabled state.
- Disabling an RTSP camera reuses `POST /api/cameras/{id}/disable`.
- During the in-flight operation only the affected RTSP switch should be disabled.
- On failure the existing toast path should show the backend error and reload RTSP summaries.

## Architecture

This is a frontend-only correction. Existing backend contracts already expose both required state and mutation paths:

- `/api/perception/engine/status` tells the app whether the engine is running and ready.
- `/api/miot/scope/cameras` exposes MIoT camera active state as `in_use`.
- `/api/cameras` exposes credential-safe MIoT and RTSP summaries, including RTSP `enabled` and `connected`.
- `/api/cameras/{id}/enable|disable` already handles RTSP activation, probe, persistence, and hot apply.

`App.tsx` will derive a source-agnostic `hasActivePerceptionCamera` from both MIoT and RTSP state, then pass `allCamerasOff={!hasActivePerceptionCamera}` to `StatusRibbon` when the inputs are loaded. `HeroNow.tsx` will render a reusable switch control on RTSP management cards and call the already-provided `onToggleRtsp` callback.

No backend API, RTSP credential storage, model configuration, camera probe logic, or OpenClaw configuration changes are in scope.

## Error Handling

The RTSP switch follows existing RTSP mutation behavior:

- `onToggleRtsp(camera, true)` calls the backend enable action, which probes before enabling.
- `onToggleRtsp(camera, false)` disables and hot-applies the change.
- On failure, the existing toast receives the error message.
- The local switch busy state is cleared in `finally` so the card cannot remain stuck after an exception.
- The status ribbon does not infer an active RTSP source from `enabled=true` alone; it requires `enabled && connected` so an unreachable RTSP source does not make the top bar claim `在看家`.

## Testing

Add frontend tests for the behavior that failed in production. The existing web
test environment is Node-based Vitest without jsdom/React Testing Library, so
the implementation should expose small pure helpers or hook-free components
that can be tested without adding new dependencies:

1. A pure status helper returns `hasActivePerceptionCamera=true` when all MIoT scope cameras are off and an RTSP camera summary is enabled and connected.
2. The same helper returns false when all MIoT cameras plus all RTSP cameras are inactive or disconnected.
3. A hook-free RTSP perception switch component exposes `role="switch"` and `aria-checked` from `camera.enabled`.
4. Calling the switch `onClick` path invokes the existing RTSP toggle callback with the camera and target enabled state.

Tests should assert rendered behavior and callback contracts, not internal implementation text. No tests should contain RTSP URLs, API keys, tokens, or camera images.

## Production Deployment

After tests and build pass, deploy the exact source SHA to `miloco.esxi` under a Software CO with active PAM. Verification should be bounded and credential-safe:

- Miloco service running.
- Local and LAN `/health` return OK.
- Dashboard root returns HTTP 200 HTML.
- Sanitized API evidence shows engine `running=true`, `ready=true`, at least one RTSP `enabled=true && connected=true`, and the derived frontend condition should be `在看家`.

Do not print or store API keys, Xiaomi tokens, RTSP URLs, raw model responses, or camera frames.

## Out of Scope

- Changing Omni model/profile configuration.
- Rewriting the camera service API.
- Enabling the offline MIoT camera.
- Adding ONVIF, PTZ, recording, retention, or additional RTSP diagnostics.
- Changing OpenClaw gateway authentication or listener configuration.
