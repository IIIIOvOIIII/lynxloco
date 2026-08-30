Implement Miloco RTSP preview/runtime repair on `miloco.esxi` from exact local Git commit `ed6fdff96eab101684d7e27eb87c6548ad3f7170`.

1. Confirm this Change Order is in `Implement` state and PAM is active for `miloco.esxi/root` before any remote mutation.
2. Confirm the local repository is at commit `ed6fdff96eab101684d7e27eb87c6548ad3f7170` with no uncommitted tracked source changes.
3. Build the release artifacts locally with the existing project build flow, including web static assets, Miloco backend wheel, Miloco CLI wheel, MIoT wheel, and OpenClaw plugin package.
4. Use `scripts/sync-to-remote.sh --local-build` to copy only `dist/` and `scripts/` to `miloco.esxi` and install the selected artifacts with `uv tool install` / `openclaw plugins install --force`.
5. Restart only the existing Miloco/OpenClaw managed services through the installer/sync flow; do not alter saved RTSP camera definitions, Xiaomi account state, model API keys, database content, or host networking.
6. Verify `miloco.esxi` health, Miloco CLI service status, OpenClaw gateway/plugin status, and the served application version.
7. Verify the kitchen RTSP camera no longer reports `unsupported_video_codec`, the living-room RTSP source keeps advancing frame timestamps across a bounded sample window, and the dashboard/watch static asset contains the less aggressive reconnect watchdog.

Secrets and privacy boundary: do not print, persist, or copy RTSP URLs, usernames, passwords, service tokens, Xiaomi tokens, model API keys, raw camera frames, or raw model responses. API calls may use the service token only inside the remote shell as a transient header.
