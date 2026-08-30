# Read-only Miloco RTSP post-deploy regression inspection

## Scope

Read-only production inspection on `miloco.esxi` to diagnose two user-reported regressions after deployment of source SHA `0ed57bebd763b002cfadfecb66bb4ebd28c19609`:

1. The kitchen RTSP camera shows "configuration needs attention" and reports "RTSP video codec could not be decoded".
2. The living-room RTSP camera frequently enters "connecting camera" after being stable before the redeploy.

## Pre-checks

1. Use approved PAM scope only for `miloco.esxi` as `root`.
2. Confirm Miloco health and current package version.
3. Inspect only bounded status, logs, codec names, camera IDs, state transitions, and safe config shape. Do not print RTSP URLs, usernames, passwords, API keys, Xiaomi tokens, service bearer tokens, cookies, camera frames, raw prompts, or raw model responses.

## Execution steps

1. Read Miloco service status, package version, and recent bounded logs around RTSP decode/reconnect failures.
2. Query authenticated Miloco camera/runtime status using the local service token only inside the command process; do not print the token.
3. Inspect safe RTSP source metadata such as camera ID, display name, enabled/connected flags, state/error codes, and codec labels without printing source URIs.
4. Compare the currently installed runtime dependency surface with the expected package/runtime path when needed, limited to PyAV/FFmpeg/OpenCV versions and available decoders.
5. Inspect local source code and recent commits to correlate the production symptoms with code changes. This read-only CO does not authorize editing or restarting production.

## Verification output

Report only:

- service health/version;
- affected camera IDs/display names;
- sanitized error classes/messages;
- codec labels and decoder availability;
- reconnect frequency/status pattern;
- root-cause hypothesis and the smallest next fix plan.

Do not retain raw camera data or secret-bearing config content.
