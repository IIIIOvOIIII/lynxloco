# Miloco perception runtime deployment to miloco.esxi

## Scope

Deploy the local Miloco release built from source SHA `0ed57bebd763b002cfadfecb66bb4ebd28c19609` to `miloco.esxi` through the official installer-based OpenClaw flow. The deployment updates Miloco backend, web assets, CLI, models bundle, and OpenClaw plugin package to version `2026.8.6.post1.dev219+g0ed57bebd`.

Do not migrate, print, rewrite, rotate, or disclose Xiaomi account tokens, RTSP URLs or credentials, Omni API keys, service bearer tokens, cookies, camera images, raw model prompts, or raw model responses.

## Local artifact evidence

- Source SHA: `0ed57bebd763b002cfadfecb66bb4ebd28c19609`
- `dist/install.sh` SHA256: `d5776691c25b8e5d6330ed65c4899855db713ab75e17d3ee7356759980855f55`
- `dist/miloco-linux-x86_64-2026.8.6.post1.dev219+g0ed57bebd.tar.gz` SHA256: `0ec1cc6e7603b1d19f3fcee82a0512ea00c3afa697aa09c066e3bc440973189f`
- `dist/miloco-openclaw-plugin-2026.8.6-post1.dev219.tgz` SHA256: `c657b0718591c2da742983e980b8fad56c61734af96afa8615e439c9712a95d0`

## Pre-checks

1. Use the approved PAM scope only for `miloco.esxi` as `root`.
2. Verify the local source SHA is exactly `0ed57bebd763b002cfadfecb66bb4ebd28c19609`.
3. Verify the local artifacts and SHA256 values listed above before upload.
4. On `miloco.esxi`, capture bounded non-secret pre-change evidence:
   - `miloco-cli --version`;
   - `miloco-cli service status`;
   - listeners for ports `1810` and `18789`;
   - OpenClaw gateway status;
   - Miloco OpenClaw plugin status/version;
   - disk and memory headroom.
5. Verify the previously deployed Miloco backup/cache is present, or create a new CO-specific pre-change backup before mutation:
   - backup directory: `/root/miloco-backup-<CO>-0ed57be-<timestamp>`;
   - include `/root/.openclaw/miloco/config.json` if present;
   - include `/root/.openclaw/openclaw.json` if present;
   - record only non-secret version/status summaries.
6. Stop before mutation if the current service is already unhealthy and cannot be safely distinguished from the pending deployment, or if a usable rollback path cannot be identified.

## Execution steps

1. Create a bounded staging directory on `miloco.esxi`, for example `/root/miloco-install-0ed57be-<timestamp>`.
2. Upload only these required release artifacts, not the source repository:
   - `dist/install.sh`;
   - `dist/miloco-linux-x86_64-2026.8.6.post1.dev219+g0ed57bebd.tar.gz`.
3. In the staging directory, create `http-root/v2026.8.6.post1.dev219+g0ed57bebd/` and place the Linux x86_64 bundle there. This path is required because the embedded manifest downloads from `<MILOCO_DOWNLOAD_URL>/<tag>/<bundle>`.
4. Verify remote SHA256 values for the uploaded installer and Linux bundle match the local evidence above.
5. Start a temporary loopback-only HTTP server from the staging `http-root` directory on an available local port.
6. Run the official installer agent prepare step without passing account/model/API-key arguments:
   - `MILOCO_DOWNLOAD_URL=http://127.0.0.1:<port> MILOCO_LANG=zh bash install.sh --agent-prepare --agent-platform=openclaw`
7. Run the official installer agent finish step without passing account/model/API-key arguments:
   - `MILOCO_DOWNLOAD_URL=http://127.0.0.1:<port> MILOCO_LANG=zh bash install.sh --agent-finish --agent-platform=openclaw`
8. Ensure Miloco remains published on the existing LAN backend endpoint:
   - set `server.host=0.0.0.0` and `server.url=http://miloco.esxi:1810` only if either value drifted;
   - restart/start Miloco through `miloco-cli service restart`.
9. Restart or refresh the OpenClaw gateway only if needed for the updated plugin to load.
10. Stop the temporary HTTP server and remove only the exact staging directory created for this change after verification succeeds.

## Verification steps

1. On `miloco.esxi`, verify `miloco-cli --version` reports `2026.8.6.post1.dev219+g0ed57bebd` or an equivalent local build version for source SHA `0ed57bebd`.
2. Verify `miloco-cli service status` reports the backend running and managed.
3. Verify Miloco health locally and over LAN:
   - `curl -fsS http://127.0.0.1:1810/health`;
   - `curl -fsS http://miloco.esxi:1810/health`.
4. Verify the dashboard root returns HTTP 200 HTML from `http://miloco.esxi:1810/`.
5. Verify authenticated runtime summary using the local service token without printing it:
   - engine running and ready;
   - RTSP source counts visible;
   - semantic state visible;
   - latest Omni diagnostic shape visible when present;
   - `model.omni.timeout` and `perception.engine.omni.timeout` are both `120.0`.
6. Verify the OpenClaw gateway remains healthy and `miloco-openclaw-plugin` remains loaded with conversation access enabled when supported.
7. Confirm no secrets, RTSP URLs, API keys, cookies, service tokens, camera frames, raw prompts, or raw model responses were printed to command output, CO notes, logs captured for this change, or final reporting.
