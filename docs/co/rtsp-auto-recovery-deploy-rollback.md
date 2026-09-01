# Rollback Miloco RTSP auto-recovery deployment

Rollback the Miloco official installation on `miloco.esxi` to the previously deployed healthy package set if source commit `de6f162aff3f3b060dcad48a614376f3af2ca222` fails health checks, cannot be installed cleanly, or unexpectedly changes runtime configuration outside the approved RTSP-only package update.

## Rollback triggers

1. Miloco local or LAN `/health` fails after deployment and does not recover after one service restart.
2. OpenClaw gateway health fails or the Miloco plugin cannot be loaded after one gateway restart.
3. Installed package/plugin versions do not match the approved artifacts for source commit `de6f162aff3f3b060dcad48a614376f3af2ca222`.
4. Existing `/root/.openclaw/miloco` configuration is unexpectedly modified outside the approved package installation and RTSP auto-disable runtime behavior.
5. The official installer/sync path fails before a healthy Miloco service is available.

## Rollback plan

1. Preserve `/root/.openclaw/miloco` by default. Do not delete or overwrite Xiaomi account state, RTSP URLs or credentials, Omni model settings, Home Assistant settings, dashboard users, service tokens, or local databases unless evidence proves this deployment corrupted the specific file being restored.
2. Use the pre-change version/source and prior staging directory recorded during pre-checks to reinstall the previously deployed Miloco, Miloco MIoT, Miloco CLI, and Miloco OpenClaw plugin artifacts through the same official installer-compatible `uv tool install` / `openclaw plugins install --force` path.
3. If the prior staging directory is unavailable, rebuild or reinstall the exact previously deployed source SHA recorded during pre-checks; do not fetch arbitrary packages or switch to the retired Docker deployment path.
4. Restore the CO-specific backup of `/root/.openclaw/openclaw.json` or `/root/.openclaw/miloco/config.json` only if the new deployment demonstrably corrupted that exact file.
5. Run Miloco skill-tool registration if the plugin package was reinstalled, refresh the OpenClaw plugin registry, restart the OpenClaw gateway, and restart the Miloco backend through the installed CLI/supervisor path.
6. Verify Miloco local and LAN `/health`, dashboard HTTP 200, OpenClaw gateway health/status, Miloco plugin loaded state, package/plugin versions, and sanitized camera-source counts.
7. Stop and request user direction if rollback would require unknown RTSP credentials, model API keys, dashboard admin passwords, broader host cleanup, firewall changes, or database surgery outside this CO.

## Rollback success criteria

- Miloco local and LAN `/health` return OK.
- OpenClaw gateway is healthy and the Miloco plugin is loaded.
- Package/plugin versions correspond to the previously deployed healthy source.
- Persistent Miloco configuration remains present and secret values are not printed or stored.
