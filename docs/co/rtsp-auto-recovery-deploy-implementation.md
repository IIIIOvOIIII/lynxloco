# Deploy Miloco RTSP auto-recovery to miloco.esxi

Deploy the exact Miloco source commit `de6f162aff3f3b060dcad48a614376f3af2ca222` to `miloco.esxi` to add bounded automatic recovery for terminal RTSP camera states.

## Scope

- Target host: `miloco.esxi`
- Target user: `root`
- Impacted service: Miloco official installation and OpenClaw Miloco plugin
- Source commit: `de6f162aff3f3b060dcad48a614376f3af2ca222`
- Version: `2026.8.6.post1.dev265+gde6f162af`
- OpenClaw plugin version: `2026.8.6-post1.dev265`
- This change adds RTSP-only runtime auto-recovery: retry every 10 minutes for up to 12 hours, restore monitoring if reconnect succeeds, and set only the failed RTSP source's `enabled` flag to `false` if the 12-hour window expires without recovery.
- This change does not edit Xiaomi account state, MIoT / Mi Home cameras, Home Assistant configuration, Omni model configuration, model API keys, RTSP URLs, RTSP usernames/passwords, dashboard users, service tokens, stored identity data, or the Miloco database schema.

## Approved artifacts and SHA-256

- `dist/install.sh`: `2895383145226d77a1891158004064811f701a0cbad024802c003d2d6f1800e3`
- `dist/miloco-2026.8.6.post1.dev265+gde6f162af-py3-none-any.whl`: `d500b4978a719ad03755d0540bd2532bab1dc8b32d3915c07c5d33b2e298471c`
- `dist/miloco_cli-2026.8.6.post1.dev265+gde6f162af-py3-none-any.whl`: `301a9bcff4c9c8f925de9aa85811abe3530f8b6388ab5a06fa0dbd379f85d9b5`
- `dist/miloco_miot-2026.8.6.post1.dev265+gde6f162af-py3-none-manylinux_2_28_x86_64.whl`: `d8864c585aad8e702e914dbcb98c27459479c2b5dc625626914957f175a0d2cf`
- `dist/miloco-openclaw-plugin-2026.8.6-post1.dev265.tgz`: `f4f1a605bd25fdeeddf32de8945e818678b65be0552f1bb98e61e97edd4dabba`
- `dist/miloco-linux-x86_64-2026.8.6.post1.dev265+gde6f162af.tar.gz`: `da2d74623110fbc68d93af6e6e5edfb41c5e22b31606d3f68baa0b9e2484d6b9`

## Pre-checks

1. Verify this CO is in `Implement`, PAM is active for `miloco.esxi/root`, and the local protected branch `main` plus `origin/main` both resolve to `de6f162aff3f3b060dcad48a614376f3af2ca222`.
2. Recompute local SHA-256 for every approved artifact listed above before upload.
3. Capture bounded non-secret pre-change evidence on `miloco.esxi`:
   - `miloco-cli --version`
   - `miloco-cli service status`
   - local Miloco `/health`
   - LAN Miloco `/health`
   - OpenClaw gateway health/status
   - Miloco OpenClaw plugin status/version
   - sanitized RTSP source counts and enabled/error state only
4. Create a CO-specific backup under `/root/miloco-backup-<CO>-de6f162-<timestamp>` before package installation. Include `/root/.openclaw/miloco/config.json` and `/root/.openclaw/openclaw.json` when present.
5. Stop before mutation if Miloco or OpenClaw is already unhealthy and cannot be safely distinguished from this pending package deployment.

## Execution

1. Create a bounded staging directory on `miloco.esxi`, for example `/root/miloco-plugin-<CO>-de6f162-<timestamp>`.
2. Upload only `dist/`, `scripts/`, and non-secret `plugins/skills/` content to that staging directory using the official installer/sync artifact flow. Do not upload workspace secrets, local credentials, Git metadata, runtime databases, or raw camera/model data.
3. Recompute remote SHA-256 values for the approved artifacts and compare them with the values in this implementation plan before installation.
4. Install `miloco`, `miloco-miot`, `miloco-cli`, and `miloco-openclaw-plugin` from the staged local artifacts using the existing official installer-compatible `uv tool install` and `openclaw plugins install --force` path.
5. Run Miloco skill-tool registration, refresh the OpenClaw plugin registry, restart the OpenClaw gateway, and restart the Miloco backend through the installed CLI/supervisor path.
6. Do not edit RTSP URLs, RTSP usernames, RTSP passwords, Xiaomi account state, MIoT / Mi Home camera settings, Omni model configuration, model API keys, Home Assistant configuration, dashboard auth users, service tokens, host firewall, or camera firmware.

## Verification

1. Verify installed Miloco, Miloco MIoT, Miloco CLI, and OpenClaw plugin versions match `2026.8.6.post1.dev265+gde6f162af` and `2026.8.6-post1.dev265`.
2. Verify Miloco local and LAN `/health` return OK.
3. Verify dashboard root returns HTTP 200 without printing the HTML body.
4. Verify OpenClaw gateway health/status is OK and the Miloco plugin is loaded.
5. Verify installed source contains `RTSP_AUTO_RECOVERY_RETRY_INTERVAL_MS` and `RTSP_AUTO_RECOVERY_WINDOW_MS`.
6. Verify sanitized camera API output still includes the existing RTSP sources and does not show unexpected loss of Xiaomi, RTSP, Omni, Home Assistant, dashboard-user, or service-token configuration.
7. If any RTSP source is in a terminal state during verification, confirm only the safe error code/message, enabled flag, and connected flag. Do not print RTSP URLs, credentials, bearer tokens, camera frames, raw video packets, prompts, or model responses.

## Success criteria

- Source and artifact hash checks match this CO.
- Miloco and OpenClaw restart successfully.
- Miloco `/health` returns OK locally and over LAN.
- OpenClaw gateway remains healthy and the Miloco plugin is loaded.
- Installed package/plugin versions correspond to source commit `de6f162aff3f3b060dcad48a614376f3af2ca222`.
- Existing persistent Miloco configuration under `/root/.openclaw/miloco` is preserved.
