# Miloco RTSP camera error diagnosis and bounded repair

## Scope

Diagnose and, where safely possible, repair two user-reported RTSP camera failures on the existing Miloco production installation on `miloco.esxi`:

1. Kitchen camera reports `RTSP video codec could not be decoded`.
2. Large-balcony camera reports `RTSP authentication failed`.

## Privacy and secret handling

- Do not print, persist, copy, or commit RTSP URLs, usernames, passwords, service tokens, Xiaomi tokens, dashboard cookies, model API keys, raw camera frames, raw video packets, raw prompts, or raw model responses.
- All production evidence must be sanitized to camera display name, source id suffix where useful, enabled/connected flags, codec labels, safe error codes/messages, timestamps, package versions, HTTP status, and bounded counts.
- Service tokens may only be read inside the remote process that makes the authenticated localhost call, and must not be echoed.

## Implementation plan

1. Confirm this Change Order is in `Implement` state with active PAM scope for `miloco.esxi` as `root` before any production mutation.
2. Confirm Miloco backend health, dashboard reachability, OpenClaw gateway/plugin health, installed package version, and current source Git SHA marker if available.
3. Read sanitized camera summaries from Miloco's local authenticated camera API, filtering for the kitchen and large-balcony RTSP entries.
4. Inspect only the safe shape of persisted RTSP configuration for the affected entries: id, name, room, enabled flag, transport, audio flag, URI scheme, whether userinfo is absent from the URI, and whether username/password fields are present. Do not print any URI host/path/user/password.
5. Run a direct in-process RTSP probe using Miloco's installed `probe_rtsp_source` code against each affected saved source, outputting only success/failure, safe error code/message, video codec, resolution, FPS, and audio codec.
6. Inspect recent bounded Miloco logs for safe RTSP error classes and state transitions, redacting any accidental URL/token-like content before output.
7. If the kitchen probe succeeds but runtime state still reports `unsupported_video_codec`, restart only the existing Miloco runtime/service once to clear stale RTSP terminal state, then re-run sanitized camera status and direct probe checks.
8. If the kitchen probe still fails with `unsupported_video_codec` while stream metadata reports an otherwise supportable codec, stop production mutation and return to local TDD/code repair before deploying a fix under this CO only if the patch is minimal and covered by tests.
9. If the large-balcony direct probe fails with `authentication_failed` while the saved configuration shape contains credentials, treat that as the camera/source rejecting the saved credentials and do not alter the credentials without user-supplied replacement values.
10. If the large-balcony saved configuration is missing expected credential fields or proves a Miloco credential-preservation/parsing defect, repair the configuration or code minimally, preserving all unrelated saved Miloco settings.
11. After any mutation, verify Miloco health, OpenClaw gateway/plugin health, sanitized camera summaries for the affected entries, and direct probe results.
12. Update the project progress document and workspace memory with outcome, CO number, root cause, and next step.

## Success criteria

- The first failing boundary for each affected camera is identified with sanitized evidence.
- Any Miloco-caused failure is repaired or reduced to a tested local patch/deployment.
- Any source-side credential failure is reported clearly without guessing or exposing credentials.
- Existing Miloco configuration, Xiaomi account state, model endpoint settings, service tokens, and dashboard auth data are preserved.

