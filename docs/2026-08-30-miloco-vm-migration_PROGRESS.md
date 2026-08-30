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

## 2026-08-30 13:45 SGT

- Current work: Published the OpenClaw gateway listener on `miloco.esxi` to the LAN wildcard address.
- Expected result: OpenClaw gateway listens on `0.0.0.0:18789`, remains healthy, and keeps the Miloco plugin loaded.
- Result: Achieved. CO `CHG260830016` reached `Implement` with active PAM and closed `Successfully Closed`. Pre-check showed default loopback bind, token auth mode present, gateway RPC healthy, Miloco plugin loaded, and Miloco backend healthy. `/root/.openclaw/openclaw.json` was backed up to `/root/openclaw-bind-CHG260830016-20260830134405/openclaw.json.pre-bind`; `gateway.bind` was set to `lan`, which OpenClaw resolves to `0.0.0.0`; the gateway was restarted. Verification showed `openclaw gateway status --require-rpc` read probe OK, `Gateway: bind=lan (0.0.0.0)`, `ss` listener `0.0.0.0:18789`, LAN HTTP `http://miloco.esxi:18789/` returning HTTP `200`, plugin `Status: loaded`, `allowConversationAccess: true`, and Miloco backend `/health` still OK.
- Next step: For browser/agent usage, authenticate with the existing OpenClaw token flow. The token value was not printed or copied into local records.

## 2026-08-30 14:27 SGT

- Current work: Diagnosing and repairing the reported `Omni 错误率 100.0%` and intermittent dashboard banner `omni 服务暂不可用（omni 响应格式异常）` on `miloco.esxi`.
- Expected result: Prove whether the issue is current config, provider behavior, runtime parser behavior, or historical dashboard statistics; stop new production errors before deploying any source fix.
- Result: Partial. CO `CHG260830019` reached `Implement` with active PAM. Live evidence showed `/api/admin/omni-config/test` passed for the saved `openai_responses` qwen profile, but runtime trace/log evidence showed actual perception calls to the same endpoint returned HTTP 200 with no visible `output_text`, producing `ValueError`, then `CircuitOpenError`, and a high 1h error-rate window. A three-color synthetic vision probe proved the current provider/model answered the red probe but produced empty visible output for blue and green, so the previous fixed-red visual preflight was a false positive for this model. The active invalid Omni profile was deactivated with a server-side backup at `/root/miloco-omni-CHG260830019-20260830142136/config.json.pre-deactivate`; latest traces after deactivation showed no new Omni calls or errors. No API key, RTSP URL, raw provider body, or camera image was recorded.
- Next step: Finish the source fix that makes Responses visual preflight require both red and blue synthetic image recognition, run focused/full checks, then create a separate exact-SHA CO before deploying the code to `miloco.esxi`.

## 2026-08-30 18:40 SGT

- Current work: Diagnosing and repairing the post-redeploy RTSP regression where `厨房摄像头` showed `配置需要处理` / `RTSP video codec could not be decoded`, and `客厅监控` visibly re-entered `正在连接摄像头`.
- Expected result: Prove whether the failures are camera/source issues, runtime RTSP session issues, or preview-only UI instability before any production mutation.
- Result: Achieved for diagnosis and local repair. Read-only CO `CHG260830031` reached `Implement` with active PAM for `miloco.esxi/root`. Live camera status showed kitchen enabled but disconnected with `unsupported_video_codec`, while a credential-safe `/api/cameras/rtsp/test` probe against the saved kitchen configuration succeeded as H264 2560x1440 with PCM_ALAW audio. The living-room source stayed connected across 30 seconds of API samples and its `last_frame_unix_ms` advanced continuously, so the observed `正在连接摄像头` regression is preview-layer churn rather than main RTSP capture loss. Local TDD fix isolated optional packet snapshot failures from the perception decode loop and made the embedded watch watchdog less aggressive so short frame gaps no longer clear the preview and reconnect. Verification passed: RTSP/camera focused backend suite `111 passed`; `web/tests/watch-mse.test.js` `19 passed`; frontend production build succeeded with only the existing large-chunk warning.
- Next step: Close the read-only CO, commit the repair, create a Software CO for exact-SHA production deployment to `miloco.esxi`, then verify kitchen reconnects and living-room preview stabilizes without exposing RTSP credentials or frame contents.
