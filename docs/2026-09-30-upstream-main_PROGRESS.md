# LynxLoco upstream integration and release — 2026-09-30

## Scope and execution plan

User request: track XiaoMi/xiaomi-miloco main, merge into LynxLoco, publish to IIIIOvOIIII/lynxloco and deploy the existing local/native installation.
Baseline origin/main e62cd64f; upstream cad239dca9b7a2dd3bf0e6565a26cf9eef6581b8,51newcommits. Use the existing isolated checkout .worktrees/upstream-main-20260911 on new branch merge/upstream-main-20260930. Existing ignored evidence and unrelated .codex_tmp are retained.

- [x] Resolve six conflicts, preserving RTSP, Responses, HA, dashboard auth and8/180concurrency semantics; review automatic rule/IoT merges. README keeps both fork and upstream release notes. Schema remains fork5; no new upstream schema step was introduced.
- [ ] Validate migration compatibility, camera/live view, rule/IoT behavior; run local backend/CLI/web/plugin/deployment suites, coverage/lint/build and a bounded independent review. Resolve observed failures only.
- [ ] Publish exact release commit to origin/main, build native artifacts, create new Software CO for miloco.esxi. On denial keep CO unchanged and stop for Lynx action.
- [ ] After fresh Implement/activePAM check, native deploy.sh preflight/backup/deploy/verify with retain policy. Preserve current configured task statuses; previous acceptance of one paused task is not authorization for additional loss. Observe10minutes natural cameras/read-only APIs; no manual device actions or synthetic provider load.
- [ ] Close accurately, publish concise release evidence, archive this progress file when complete. No durable-memory update requested.

Budget:120minutes active local work, two targeted repair cycles per failure class initially (absolute maximum5), one qualified release attempt and10minute natural observation. Stop for approval, unexpected functional/data loss, or budget exhaustion. Use currentv4 standards proportionately; retain native runtime rather than substituting Docker deployment.

## Initial integration

- Current work:51upstreamcommits fetched; merge in progress; streaming conflicts delegated with disjoint ownership.
- Expected result: minimal compatible merge and deployed published release.
- Result: Partial. No production access. Six conflicts identified. Upstream remains schema4; fork5 migration chain is preserved.
- Next step: complete local integration, tests and release qualification.

## 2026-09-30 00:49 +0800

- Current work: Merge conflicts resolved; final release qualification starting.
- Expected result: Full deploy-preflight on stable integrated source.
- Result: Targeted migration55, CLI715, frontend559+1skip, plugin191+typecheck, camera236, watch-page10 and deploy/scripts/Hermes428+2skip passed. Timed model/concurrency33passed without coverage instrumentation. First full backend run overlapped worker edits and is not final evidence; stale failures resolved, RTSP clock regression corrected by limiting MIoT watchdog timestamps to MiotCameraSource. Independent IoT/rule/DB review found no blocker; stream review pending. Native runtime retained. New v4sharedgate measures production-source-only backend (81.56baseline) and full frontend (16.09baseline), with documented81/16floors under v4allowance; no test-code coverage inflation or changed timing assertions.
- Next step: Commit integration, run final deploy-preflight, complete review, build/publish and requestCO.
