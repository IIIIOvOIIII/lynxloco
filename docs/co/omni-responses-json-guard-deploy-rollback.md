Rollback the Miloco official installation on `miloco.esxi` to the previously deployed healthy package set from source commit `ee037668974a319ce5459c33e27ba7980e42ab4c` if the new `f83d500c3acdfcc3465d6b2148b112e656a206e8` deployment fails health checks or worsens service availability.

Rollback plan:
1. Keep the current camera configuration, Xiaomi account configuration, model endpoint configuration, model API keys, RTSP URLs, stored identity data, and Miloco databases unchanged.
2. Use the prior remote staging directory `/root/miloco-plugin-CHG260830036-ee03766-202608301918` if it is still present.
3. Reinstall the previous `miloco`, `miloco-miot`, `miloco-cli`, and `miloco-openclaw-plugin` artifacts from that staging directory.
4. Run skill-tool registration, refresh the OpenClaw plugin registry, restart the OpenClaw gateway, and restart the Miloco backend.
5. Verify Miloco health, dashboard HTTP 200, OpenClaw gateway health, and installed package versions corresponding to `2026.8.6.post1.dev223+gee0376689` / `2026.8.6-post1.dev223`.
6. If the prior staging directory is missing or corrupted, stop and request user approval for a broader rollback rebuild rather than fetching arbitrary packages.

Rollback trigger:
- Local or LAN `/health` fails after the deployment.
- Miloco backend cannot be restarted through the installed supervisor/CLI path.
- OpenClaw gateway fails to restart or the Miloco plugin cannot be loaded.
- The deployment changes unrelated runtime configuration or data unexpectedly.
