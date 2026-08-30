# Miloco docker.esxi retirement and miloco.esxi full install progress

## 2026-08-30 13:23 SGT

- Current work: Preparing governed production change to retire the standalone Miloco Docker deployment on `docker.esxi:1811` and install the modified Miloco build on independent VM `miloco.esxi` through the official OpenClaw installer flow.
- Expected result: Fixed release artifact and written execution boundary before any production mutation.
- Result: Achieved. Local modified release was built from Git SHA `6c6b00fd6e9b12aea29d70a897cc2d505b8b8694`; `dist/install.sh` SHA256 is `6402aa63f2a163e9ea13a820b2344220a9782d6e4c0c9b500a67d8c3fe70b776`, and Linux x86_64 platform bundle SHA256 is `81bf38c21710f40979ebc7a70fb43bbd8db448a3c05439361f06e320c1f72b68`.
- Next step: Create an ITSM CO covering both `docker.esxi` and `miloco.esxi`, wait for Implement/PAM, then execute decommission/install/verification without migrating Xiaomi/API/RTSP secrets unless separately approved.

## 2026-08-30 13:25 SGT

- Current work: Created and approved the governed production change.
- Expected result: CO reaches `Implement` with active PAM for `docker.esxi/root` and `miloco.esxi/root` before any remote mutation.
- Result: Achieved. CO `CHG260830015` was AI-approved with `risk=Medium`, `impact=Medium`, `state=Implement`, and `pam_status=active`. Exact-SHA deploy gate passed for both hosts with expected SHA `6c6b00fd6e9b12aea29d70a897cc2d505b8b8694`.
- Next step: Retire the old Docker deployment and install the modified full OpenClaw-backed release on `miloco.esxi`.

## 2026-08-30 13:36 SGT

- Current work: Executed the old Docker retirement and new VM install.
- Expected result: `docker.esxi:1811` no longer serves Miloco, and `miloco.esxi` runs the modified Miloco build with OpenClaw plugin support.
- Result: Achieved. On `docker.esxi`, the `miloco` Compose container was removed, Miloco image tags were removed if unused, port `1811` no longer listened, and `/opt/miloco` was moved out of the active path into `/root/lynx-demise-CHG260830015-20260830132653/opt/miloco`. On `miloco.esxi`, OpenClaw CLI/Gateway `2026.7.1-2` was installed, the modified Miloco release `2026.8.6.post1.dev192+g6c6b00fd6` was installed from the local release mirror, backend config was adjusted to `server.host=0.0.0.0` and `server.url=http://miloco.esxi:1810`, Miloco backend was restarted, OpenClaw gateway was restarted, and the temporary release mirror/staging directory was removed.
- Next step: Verify live state, close the CO, and record credential/configuration limitations.

## 2026-08-30 13:38 SGT

- Current work: Completed final verification and CO closeout.
- Expected result: New service is reachable and OpenClaw plugin is active; old service remains down.
- Result: Achieved. `curl http://miloco.esxi:1810/health` returned `{"status":"ok"}` from both VM-local and operator-side LAN checks; the Miloco dashboard root returned HTTP `200` with HTML content type without printing page HTML. `miloco-cli service status` reported `running=true`, `managed=true`, and server URL `http://miloco.esxi:1810`. `openclaw gateway status --require-rpc` reported a running systemd user gateway with read probe OK. `openclaw plugins inspect miloco-openclaw-plugin` reported `Status: loaded`, version `2026.8.6-post1.dev192`, and `allowConversationAccess: true`. On `docker.esxi`, the Miloco container/image inventory for the `miloco` project was empty, port `1811` was not listening, and `curl http://docker.esxi:1811/health` failed to connect as expected. CO `CHG260830015` was closed `Successfully Closed`.
- Next step: Xiaomi account binding, RTSP camera entries, and Omni API key are intentionally not migrated because they contain secrets. Reconfigure them on `miloco.esxi` manually, or run a separate explicitly approved credential/state migration.
