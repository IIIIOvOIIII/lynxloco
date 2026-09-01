# RTSP Auto-Recovery and Auto-Disable Design

## Goal

Make Miloco recover RTSP cameras that enter a terminal error state without requiring an operator to press the existing retry path manually. For the current production problem, this covers RTSP terminal errors such as `unsupported_video_codec` and `authentication_failed`, where a later retry can succeed even though the dashboard still shows the earlier error.

The recovery policy is deliberately bounded:

- Retry every 10 minutes after a terminal RTSP error.
- Keep retrying for up to 12 hours from the first terminal failure in the current failure window.
- If a retry succeeds, restore normal monitoring and clear the failure window.
- If the 12-hour window expires without recovery, disable that RTSP camera by setting its Miloco RTSP `enabled` flag to `false`.

## Scope

This feature applies only to manually configured RTSP cameras in Miloco. It does not apply to MIoT / Mi Home cameras, Xiaomi account state, Home Assistant devices, Omni model configuration, or OpenClaw gateway configuration.

The auto-disable action disables only Miloco's RTSP perception source. It does not alter the physical camera, RTSP server, camera password, RTSP URL, firewall, or any external NVR.

## User-Facing Behavior

When an RTSP camera enters a terminal error state, the dashboard may continue to show the safe error message, for example:

- `RTSP video codec could not be decoded`
- `RTSP authentication failed`

Behind the scenes, Miloco starts a bounded recovery window. The user does not need to keep the browser open. Every 10 minutes, Miloco attempts the same retry/reconnect behavior that is currently available through the existing enable/retry path.

If the source becomes reachable and decodable again within 12 hours, Miloco reconnects it and the dashboard returns to the normal online/connected state.

If the camera is still failing after 12 hours, Miloco disables the RTSP source. It should then appear disabled in the RTSP camera list. The user can manually re-enable it later after fixing the RTSP URL, credentials, camera firmware, network path, or codec setting.

## Architecture

Put the recovery logic in the backend RTSP source runtime, not in the frontend and not in an external cron job.

The natural owner is `RtspCameraSource`, because it already owns:

- active RTSP sessions;
- terminal tombstones for non-recoverable RTSP failures;
- the explicit `request_retry` path that clears terminal suppression before reconnecting;
- source settings fingerprints, which let Miloco distinguish a continued failure from a user configuration change.

Add a small in-memory recovery state alongside terminal tombstones:

- `first_failure_monotonic_ms`
- `last_retry_monotonic_ms`
- `attempt_count`
- `last_error_code`
- connection fingerprint for the failed configuration

The state is intentionally in memory. A Miloco process restart should perform a fresh startup discovery and connection attempt from the persisted RTSP configuration; it does not need to preserve a half-expired recovery timer across restarts.

The periodic sync path should call a new RTSP-source method before normal discovery, roughly:

1. Load the current RTSP settings.
2. Drop recovery state for removed, disabled, or changed sources.
3. For each terminal tombstone whose fingerprint still matches:
   - if less than 10 minutes since the last retry, keep suppressing discovery;
   - if within 12 hours and retry interval has elapsed, clear the tombstone and allow the normal sync pass to reconnect the source;
   - if 12 hours has expired, request auto-disable for that source.
4. Return the RTSP source IDs that must be disabled.
5. The higher-level camera adapter or perception service performs the persisted disable through the existing configuration mutation path, preserving locking and hot-apply behavior.

This keeps the RTSP source runtime responsible for deciding when recovery is due, while leaving persisted configuration writes in the existing service/config writer boundary.

## Error Handling

Only terminal RTSP states participate. Recoverable connection failures that already retry inside `RtspSession` remain unchanged.

The recovery window starts when a terminal state is recorded into a tombstone. If the RTSP configuration changes, the old recovery state is discarded because the fingerprint no longer matches; the changed configuration gets a normal fresh connection attempt.

If an automatic retry attempt fails and produces another terminal error, the failure remains in the same 12-hour window rather than resetting the timer forever. This prevents a genuinely bad password or unsupported codec from being retried indefinitely.

If the persisted auto-disable write fails, Miloco should leave the source enabled and log a safe error. It should not delete credentials, alter the URL, or loop aggressively. The next periodic sync can attempt the bounded disable again.

## Configuration

Use code constants for this first implementation:

- retry interval: 10 minutes;
- failure window: 12 hours.

Do not add a dashboard settings page or config file knobs in this change. The user asked for one concrete policy, and adding knobs would increase deployment and support surface without improving this fix.

Tests may inject a clock and shorter intervals through constructor arguments or private test-only seams, but production behavior should remain the fixed policy above.

## Testing

Use test-first coverage around the source runtime and adapter/service integration:

1. A terminal RTSP tombstone is not retried before the 10-minute interval.
2. After 10 minutes, the terminal tombstone is cleared and the normal sync path reconnects the RTSP source.
3. If the reconnect succeeds, the source returns to connected monitoring and the recovery state is cleared.
4. Repeated terminal failures stay within the original 12-hour window and do not reset the timer indefinitely.
5. After 12 hours without success, Miloco requests persistent disable for that RTSP source.
6. Auto-disable affects only RTSP sources and does not disable MIoT cameras.
7. User configuration change resets the old recovery state by fingerprint mismatch.

Tests must not include RTSP URLs with credentials, API keys, bearer tokens, camera frames, or raw model responses.

## Production Deployment

Deploy the exact source SHA to `miloco.esxi` under a Software CO with active PAM.

Use the existing official installer/sync package flow for the current Miloco VM installation. Do not switch back to the retired Docker deployment shape. Preserve `/root/.openclaw/miloco`, including Xiaomi account state, RTSP camera definitions, Omni model settings, Home Assistant settings, dashboard auth users, and service tokens.

Production verification should be credential-safe:

- Miloco local and LAN `/health` return OK.
- OpenClaw gateway health returns OK.
- `miloco-openclaw-plugin` remains loaded.
- Sanitized camera API output still shows existing RTSP sources.
- Installed source contains the RTSP auto-recovery policy constants and tests have passed locally.
- If any RTSP source is already in terminal state during deployment verification, confirm that the auto-recovery state is scheduled without printing RTSP URLs, credentials, tokens, or camera frames.

## Out of Scope

- MIoT / Mi Home camera auto-disable.
- Editing RTSP URLs, usernames, passwords, or transports automatically.
- Camera firmware or ONVIF changes.
- External cron jobs or systemd timers.
- Dashboard policy configuration UI.
- OpenClaw model/provider changes.
- Home Assistant device recovery.

