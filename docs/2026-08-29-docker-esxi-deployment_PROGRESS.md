# 2026-08-29 Docker ESXi Miloco Deployment Progress

## 2026-08-29 00:00 SGT

- Current work: Prepare a minimal repeatable `docker.esxi` production deployment target for Miloco after lab01/lab02 acceptance.
- Expected result: Add a project-controlled deployment path for `docker.esxi:1811` before opening the production CO.
- Result: In progress. User approved the minimal repeatable deploy-script support path, with port `1811`, direct HTTP, no Apache/FQDN/SmartDNS, no secrets, no real RTSP camera, and no real local VLM configuration in scope.
- Next step: Add TDD deployment-contract coverage, implement host profiles, commit, then open an exact-SHA CO and deploy only after `state=Implement` plus active PAM.

## 2026-08-29 10:20 SGT

- Current work: Implemented the approved minimal `docker.esxi` deployment target in the existing Miloco deployment controller.
- Expected result: Keep ai-lab01/ai-lab02 behavior unchanged while adding a production profile for `docker.esxi` using `/opt/miloco`, HTTP port `1811`, Compose project `miloco`, and production image names.
- Result: Achieved locally. The focused docker.esxi deployment-profile tests passed, followed by the full deployment-contract suite (`184 passed`), script syntax validation, whitespace/diff checks, and project-level local CI (`全部通过 (6 项)`, with known macOS-only skips).
- Next step: Commit the deploy-target support, build the exact final SHA archive, then create a production CO and deploy only after approval and active PAM.

## 2026-08-29 10:31 SGT

- Current work: Deployed Miloco to production host `docker.esxi` under CO `CHG260829002`.
- Expected result: Exact deployed source SHA `5fd053e620636c32dd0f7c439d25bd40a743d965` runs from `/opt/miloco` on HTTP port `1811`, with no Apache/DNS/certificate/RTSP/VLM secret changes.
- Result: Achieved. CO was approved by AI, PAM became active for `docker.esxi/root`, `deploy.sh preflight` passed, `deploy.sh deploy docker.esxi` completed, remote acceptance passed (`22 passed`), `deploy.sh verify docker.esxi` returned the expected SHA, `deploy.sh status docker.esxi` reported `image=miloco:5fd053e620636c32dd0f7c439d25bd40a743d965 health=healthy`, and `http://docker.esxi:1811/health` returned `{"status":"ok"}`. CO `CHG260829002` was closed as `Successfully Closed`.
- Next step: Optional follow-up is to provide real RTSP camera/VLM configuration for production validation; the current production deployment is direct HTTP only on port `1811`.

## 2026-08-30 11:45 SGT

- Current work: Deployed the production RTSP H.264 transcoding and `4096m` memory-profile repair to `docker.esxi` under CO `CHG260830010`.
- Expected result: The production profile must enforce a 4 GiB container memory limit and the live RTSP browser path must use shared H.264 software transcoding for RTSP H.264 sources, with exact-SHA deploy verification and no secret-bearing evidence.
- Result: Achieved. Exact deployed SHA is `500d8bbea466f6c7d53b0488083ff4c1b0c0cf74`; the candidate archive SHA-256 is `fc538bc8d884dd78d23a487a5f7e6e93798dc40a6e07c5b84111b93f900b3f48`. `deploy.sh verify docker.esxi` returned the exact SHA, `deploy.sh status docker.esxi` returned `image=miloco:500d8bbea466f6c7d53b0488083ff4c1b0c0cf74 health=healthy`, and the external health endpoint returned HTTP 200. Docker inspect proved `HostConfig.Memory=4294967296`, `health=healthy`, `OOMKilled=false`, and restart count `0`. Two enabled RTSP streams were verified through the authenticated live API as `mode=transcoding`, `input_codec=h264`, `output_codec=h264`; both produced decodable output frames without the saturated-green signature. CO `CHG260830010` was closed `Successfully Closed`.
- Next step: Keep the production memory profile at `4096m`. If more RTSP streams are added, use bounded stats plus per-stream `stream/state` evidence before raising limits again.

## 2026-08-30 12:14 SGT

- Current work: Repaired the remaining user-visible RTSP browser green-screen issue observed through Computer Use on `http://docker.esxi:1811/` after the prior H.264 transcode deployment.
- Expected result: Keep the production memory profile at `4096m`, avoid opening new RTSP connections or changing camera/model configuration, and add a browser-safe fallback that fixes the real Chrome rendering path while preserving MIoT/default H.264 behavior.
- Result: Achieved locally, pending production CO/deploy. The fix adds a bounded `LiveJpegStreamHub` that listens to existing RTSP decoded-frame fan-out, coalesces pending frames, limits preview cadence, and emits JPEG bytes for `?format=jpeg`. The generic WebSocket route now selects that hub only for JPEG format, and the watch page selects JPEG/canvas fallback for RTSP cameras when WebCodecs is unavailable or the page is non-secure LAN HTTP. Local verification passed: new failing-first backend and frontend tests, `backend/miloco/tests/camera` (`173 passed`), scoped Ruff, focused Web tests, full Web Vitest (`406 passed`, `1 skipped`), TypeScript typecheck, production Web build, `git diff --check`, and `./scripts/local-ci.sh --tests`.
- Next step: Commit the exact candidate SHA, build the immutable release archive, open a new production CO for `docker.esxi/root`, deploy only after approval/PAM, verify `4096m` memory remains effective, then use Computer Use against the live Chrome page as the final acceptance gate.

## 2026-08-30 12:20 SGT

- Current work: Corrected the production-deploy candidate after CO `CHG260830011` failed during remote acceptance before service cutover.
- Expected result: Preserve the actual production service state, close the failed CO truthfully, and produce a new exact candidate whose packaged acceptance tests include the new JPEG hub dependency.
- Result: Achieved locally. `CHG260830011` was closed `Failed`; the failure occurred during remote acceptance because an integration fixture app had not overridden the newly required JPEG stream hub dependency, so no successful production deployment was completed. The fixture was fixed by overriding `_get_live_jpeg_stream_hub` with a `LiveJpegStreamHub` in `test_rtsp_live_view.py`. Verification passed for the exact failing fixture test (`2 passed`), the combined camera + RTSP live-view suite (`185 passed`, `1 skipped`), scoped Ruff, and `git diff --check`.
- Next step: Commit the fixture correction, rebuild a new immutable candidate, open a new exact-SHA production CO, and deploy only after approval/PAM.

## 2026-08-30 12:27 SGT

- Current work: Deployed the corrected RTSP JPEG browser-fallback candidate to `docker.esxi` and performed live browser acceptance under CO `CHG260830012`.
- Expected result: Production should run exact SHA `6fa4cf528ac1308c7f122f6216f2f580507fed25` on `http://docker.esxi:1811/`, retain the `4096m` container memory limit, remain healthy, and render RTSP live views in Chrome without the green-screen artifact.
- Result: Achieved. The candidate archive SHA-256 was `4a12bb21d90dfa5baedbc7a9a4bce893cd507bd43d75b16acc0d75f09725fa9f`. `deploy.sh preflight docker.esxi` passed, `deploy.sh verify docker.esxi` returned the exact deployed SHA, `deploy.sh status docker.esxi` returned `image=miloco:6fa4cf528ac1308c7f122f6216f2f580507fed25 health=healthy`, and `/health` returned `{"status":"ok"}`. Docker inspect proved `HostConfig.Memory=4294967296`, `health=healthy`, `OOMKilled=false`, and restart count `0`. Computer Use validation against the live Chrome page showed both RTSP cards rendering normal color frames after reload, and a single RTSP live-view popup also rendered a normal color frame after an 8-second wait. CO `CHG260830012` was closed `Successfully Closed`.
- Next step: Treat the production browser green-screen defect as fixed. Keep the RTSP browser fallback scoped to RTSP live-view rendering; perception and MIoT/default H.264 paths remain unchanged.
