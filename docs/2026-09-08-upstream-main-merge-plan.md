# Lynxloco upstream merge and production release plan

**Goal:** Merge XiaoMi/xiaomi-miloco main at 646d19e7380c66c4a8db4ac2e14737938b376bdf into IIIIOvOIIII/lynxloco, publish and deploy through the native deploy.sh.

**Architecture:** Preserve fork customization and merge upstream history. Extend the existing native release only as needed to ship the changed detector model and OpenClaw plugin with backup and verification; do not migrate runtime or change model provider settings.

**Spec:** User request in this task; retain concurrency=8 and timeout/window=180 and user-controlled rollback from the preceding accepted deployment.

**Tech stack:** Python/uv, TypeScript/pnpm, native OpenClaw/supervisor, existing digest-bound deploy.sh.

## Constraints and budget

- Preserve RTSP, Responses, Home Assistant, dashboard authentication, corrected metrics, 1–8 concurrency UI, and shared 180-second window.
- No automatic rollback, no four-way fallback. On failure retain evidence and backup for user decision.
- No production SSH before approved CO/PAM. One exact candidate deployment after verification, one 10-minute post-start natural camera observation; no manual device actions or synthetic provider load. Local tests/build up to two repair attempts per failure class. Stop for required human approval.
- Existing untracked .codex_tmp/ is user-owned and excluded from commits.

## Tasks

- [x] Merge the 37 new upstream commits; resolve .gitignore by retaining both sets of rules. Review automatic merges around startup/shutdown, MIoT and packaging.
- [x] Extend native delivery to include exact-built OpenClaw plugin and detector/model payload. Preserve current runtime config; back up old assets, verify installed asset digest/plugin version, keep explicit rollback possible. Add focused deployment tests for successful asset installation and retain-on-failure.
- [x] Run backend, CLI, web and OpenClaw regressions plus native deployment tests; build exact committed release using MILOCO_DEPLOY_RUNTIME=openclaw ./deploy.sh build. Review source merge and archive contents.
- [x] Push merged candidate to origin/main and verify remote SHA. Create exact-SHA Software CO for miloco.esxi with read-only preflight, backup, native install/restart and acceptance scope. Pause if human approval required.
- [x] After approval and live exact-SHA gate, deploy via deploy.sh with retain policy; verify code/plugin/model, health, unchanged configuration, 8/180 settings, RTSP progress and successful natural perception over ten minutes.
- [x] Record outcome, close CO truthfully, publish closeout documents, update progress and memory.

Final result: deployedcb87f6c0 (backend/CLIdev350,plugin350,newdetector), functional acceptance passed with62/63successful requests,8-waypeak,health200,stable2RTSP. Queue drops47.14% remain. No rollback. BothCO003/004SuccessfullyClosed. Full [closeout](2026-09-08-upstream-main-merge-closeout.md).
