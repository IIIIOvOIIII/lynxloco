# Read-only Miloco one-frame Omni E2E verification

## Scope

Read-only production inspection on `miloco.esxi` to verify whether a single living-room RTSP camera frame reaches the configured Miloco Omni LLM endpoint and returns a parseable perception result.

## Pre-checks

1. Connect to `miloco.esxi` with approved PAM SSH access.
2. Confirm the Miloco service is running and `/health` is healthy.
3. Inspect only sanitized runtime status needed to identify the living-room RTSP camera source and the current Omni profile.

## Execution steps

1. Use the current running Miloco configuration and active RTSP source; do not edit configuration.
2. Capture or request exactly one current living-room camera frame through the existing Miloco/RTSP read path.
3. Submit one minimal Omni perception request through the same configured Omni protocol, model, base URL, API key, and parser path used by Miloco.
4. Collect only sanitized evidence:
   - frame acquired: yes/no;
   - visual payload included: yes/no;
   - provider HTTP status and latency;
   - response text present: yes/no;
   - parser success: yes/no;
   - structured field presence/counts such as caption/rules/suggestions/speech.

## Prohibitions

- No code deployment.
- No service restart.
- No settings mutation.
- No database write beyond any unavoidable existing application inference accounting.
- No RTSP URL, username, password, API key, bearer token, Xiaomi token, cookie, raw image, raw video, raw audio, raw prompt, or raw model response may be printed, persisted, or copied into notes.
- Temporary frame data, if any, must remain under a task-specific temporary directory and be deleted before closure.

## Verification

Report the first failing boundary:

1. camera frame acquisition;
2. request construction with visual payload;
3. provider HTTP/auth/model acceptance;
4. response text extraction;
5. Miloco structured parsing;
6. non-empty visual caption/semantic fields.

If every boundary succeeds, report that the endpoint is wired and that the remaining realtime-no-log issue is likely prompt/semantic policy or downstream dedup/event filtering rather than "no LLM involved".

