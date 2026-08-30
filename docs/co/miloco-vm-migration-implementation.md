Pre-checks:
1. Use the approved PAM scope only for `docker.esxi` and `miloco.esxi` as `root`.
2. Verify the local deployment source SHA before execution: `6c6b00fd6e9b12aea29d70a897cc2d505b8b8694`.
3. Verify the local release artifacts before upload:
   - `dist/install.sh` SHA256 `6402aa63f2a163e9ea13a820b2344220a9782d6e4c0c9b500a67d8c3fe70b776`
   - `dist/miloco-linux-x86_64-2026.8.6.post1.dev192+g6c6b00fd6.tar.gz` SHA256 `81bf38c21710f40979ebc7a70fb43bbd8db448a3c05439361f06e320c1f72b68`
   - `dist/miloco-openclaw-plugin-2026.8.6-post1.dev192.tgz` SHA256 `c64bd1f359fb5b69361afdcd625b39f40460c29bab6b2a326fd32de3779249c8`
4. On `docker.esxi`, capture bounded pre-change evidence: Docker containers/images for the `miloco` compose project, active listeners on port `1811`, `/opt/miloco` metadata, and the current health endpoint status. Do not print env files, service tokens, RTSP URLs, API keys, cookies, or camera imagery.
5. On `miloco.esxi`, capture bounded pre-change evidence: OS version, CPU architecture, memory, disk, availability of ports `1810` and `18789`, OpenClaw/Node/uv presence, and whether any active Miloco state already exists.

Execution steps:
1. On `docker.esxi`, stop the standalone Docker deployment with `docker compose` from `/opt/miloco/current` or the active compose file, using project name `miloco`: `docker compose -p miloco down --remove-orphans`.
2. Remove any leftover Miloco containers from the `miloco` project if `docker compose down` did not remove them. Do not use `docker compose down -v` and do not purge Docker volumes or database files.
3. Move the active `/opt/miloco` deployment directory to a timestamped backup path under `/root/lynx-demise-<CO>-<timestamp>/opt-miloco` so the service cannot be restarted accidentally from the old active path. Preserve deployment metadata and state files inside the backup; do not print secrets.
4. Remove or untag only the old Miloco application image(s) that are tied to the retired standalone deployment after the container is stopped and the active directory has been archived. Skip image removal if another running container uses the image.
5. On `miloco.esxi`, create a staging directory such as `/root/miloco-install-6c6b00f-<timestamp>` and upload only the release installer and Linux x86_64 platform bundle, not the full repository.
6. On `miloco.esxi`, verify uploaded SHA256 values match the pre-check artifact hashes.
7. If OpenClaw CLI/Gateway is absent on `miloco.esxi`, install the OpenClaw Linux prerequisite and start the root user gateway service using the same minimal gateway approach previously validated in lab. Do not restart the gateway while a first-run migration lock is active.
8. Serve the uploaded release bundle locally on `miloco.esxi` via a temporary loopback-only HTTP server and run the modified installer with the official OpenClaw agent path:
   - `MILOCO_DOWNLOAD_URL=http://127.0.0.1:<temporary-port> bash install.sh --agent-prepare --agent-platform=openclaw`
   - `MILOCO_DOWNLOAD_URL=http://127.0.0.1:<temporary-port> bash install.sh --agent-finish --agent-platform=openclaw`
9. Do not silently migrate Xiaomi account tokens, RTSP camera credentials, Omni API keys, cookies, or service bearer tokens from `docker.esxi` to `miloco.esxi`. If the installer reports account/model configuration as missing, complete software installation and report that credential setup remains user-driven or requires separate explicit migration approval.
10. Adjust the installed Miloco configuration to bind the backend on LAN if needed: `server.host=0.0.0.0` and `server.url=http://miloco.esxi:1810`, with a config backup before mutation and a fresh internal service bearer if a token is generated.
11. Restart or start Miloco through `miloco-cli service` and reload/restart OpenClaw gateway only as needed after the plugin is installed.

Verification steps:
1. On `docker.esxi`, verify no Miloco container is running, no container has a restart policy that will relaunch the retired service, port `1811` is no longer listening, and `/opt/miloco` is absent from the active path.
2. On `miloco.esxi`, verify `miloco-cli service status` reports the backend running and managed.
3. Verify local and LAN health endpoints: `curl http://127.0.0.1:1810/health` on `miloco.esxi` and `curl http://miloco.esxi:1810/health` from the operator side.
4. Verify the Miloco dashboard root page returns HTML at `http://miloco.esxi:1810/`.
5. Verify OpenClaw gateway status and plugin status: `openclaw gateway status --require-rpc` and `openclaw plugins inspect miloco-openclaw-plugin`; confirm the Miloco plugin is loaded and conversation access is enabled when supported by the installed OpenClaw version.
6. Verify no secrets, RTSP URLs, API keys, cookies, service tokens, or camera images were printed to logs or the final report.
7. Close the CO with the actual outcome and update project progress and workspace memory.
