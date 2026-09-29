# LynxLoco upstream integration and release — 2026-09-30

## Scope and execution plan

User request: track XiaoMi/xiaomi-miloco main, merge into LynxLoco, publish to IIIIOvOIIII/lynxloco and deploy the existing local/native installation.
Baseline origin/main e62cd64f; upstream cad239dca9b7a2dd3bf0e6565a26cf9eef6581b8,51newcommits. Use the existing isolated checkout .worktrees/upstream-main-20260911 on new branch merge/upstream-main-20260930. Existing ignored evidence and unrelated .codex_tmp are retained.

- [x] Resolve six conflicts, preserving RTSP, Responses, HA, dashboard auth and8/180concurrency semantics; review automatic rule/IoT merges. README keeps both fork and upstream release notes. Schema remains fork5; no new upstream schema step was introduced.
- [x] Validate migration compatibility, camera/live view, rule/IoT behavior; run local backend/CLI/web/plugin/deployment suites, coverage/lint/build and a bounded independent review. Resolve observed failures only.
- [x] Publish exact release commit to origin/main, build native artifacts, create new Software CO for miloco.esxi. On denial keep CO unchanged and stop for Lynx action.
- [x] After fresh Implement/activePAM check, native deploy.sh preflight/backup/deploy/verify with retain policy. Preserve current configured task statuses; previous acceptance of one paused task is not authorization for additional loss. Observe10minutes natural cameras/read-only APIs; no manual device actions or synthetic provider load.
- [x] Close accurately, publish concise release evidence, archive this progress file when complete. No durable-memory update requested.

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

## 2026-09-30 01:07 +0800

- Current work: Published exact final release; approved production preflight.
- Expected result: Native update preserving current configuration/task state.
- Result: Final cf5f9dae4a25d3b35138d2f938acbad1a319d78a gatepassed (backend82.10%lines, frontend16.09%), native2026.9.11.post1.dev322 built and Linuxcamera library matched. All51upstreamcommits included. GitHubOAuth lackedworkflow permission andSSHfailed; existing Vault ref webapp.external.github_com.api_token published successfully without changing account configuration. Remote main exactSHA verified. CHG260930001(id2029) AIAccepted/Implement/activePAM; fresh exactgatepassed. Read-only baseline showsdev463,healthy,4active/1paused,9rules,schema5,2enabledRTSPconnected,8/180. Currentconfig has additional savedprofiles and must be preserved as-read. Initial localpreflight stopped on generated backend/.coverage; moved that task-owned artifact into ignored .tmp/coverage/backend.data, no sourcechange. No productionmutation yet.
- Next step: Finish nativepreflight, deploy exactcandidate withretainpolicy and natural10minacceptance; archive thisprogress aftercloseout.

## 2026-09-30 01:14 +0800

- Current work: Exactcf5f9dae native deployment completed; natural observation active.
- Expected result: Preservecurrentconfig/taskstatus and show working sources for10minutes.
- Result: Deployment/verify/post-deployHTTPgate passed. Backend/CLI2026.9.11.post1.dev322+gcf5f9dae4,plugin2026.9.11-post1.dev322,models/pluginfiles and installedLinuxcamera library verified. Configbytes/authusers/legacyrulefields/OpenClawcore preserved (onlymeta changed).8/180andsharedwindow180 retained.4active1paused/9rules/schema5unchanged. HAconnected;2enabledRTSPconnected; engineready andpeak8. Backup /opt/miloco-openclaw/backups/cf5f9dae4a25d3b35138d2f938acbad1a319d78a. Observation1790701950899-1790702550899ms is ongoing.
- Next step: Completeobservation,aggregateactualoutcome,closeCO001accurately,publish/archivereport. Noadditionalserverchanges planned.

## 2026-09-30 01:25 +0800

- Current work: Release and scoped acceptance complete; CO closed.
- Expected result: Published/deployed exactcandidate with currentconfiguration/taskstates preserved.
- Result: Achieved. CHG260930001 SuccessfullyClosed. Sourcecf5f9dae4a25d3b35138d2f938acbad1a319d78a, backend/CLI2026.9.11.post1.dev322/plugin2026.9.11-post1.dev322 verified.10min 2026-09-30T01:12:30.899+08:00 to 2026-09-30T01:22:30.899+08:00:97requests/0errors,52and45successfulcamera windows,14health200samples,PID1467329stable,peak8,198cycles/98skipped/76dropped(27.74%). RSSpeak/final2194.8MiB,minimumavailable2690.9MiB. Config/auth/rules/taskstates preserved,4active1paused9rules unchanged. All18acceptancechecks passed. No rollback/split/reactivation/manualdevicecommands or syntheticproviderrequests.
- Next step: None for this release. Archive thisprogress and publish concise evidence. Backup retained at /opt/miloco-openclaw/backups/cf5f9dae4a25d3b35138d2f938acbad1a319d78a. ClosedCO grants no furtherproductionaccess. No durablememory update was requested or performed.
