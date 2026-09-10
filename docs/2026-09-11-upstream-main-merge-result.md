# LynxLoco upstream merge — release prepared, deployment awaiting approval

As of 2026-09-11 00:42 +0800, upstream integration and publication are complete. Production deployment is not complete: CHG260911002 requires Lynx approval after AI denial. No production access occurred.

## Published source

- Repository: https://github.com/IIIIOvOIIII/lynxloco
- Merge candidate: `71dacdcb422e8bde660efe1c4e737b7b6fec5661`
- Upstream: XiaoMi/xiaomi-miloco main `75aed171`, all 111 commits since the previous tracked upstream point. Git ancestry and remote main SHA verified.
- Backend/CLI: `2026.8.6.post1.dev463+g71dacdcb4`; OpenClaw plugin: `2026.8.6-post1.dev463`.
- This report and progress updates are documentation only and do not change the deployment candidate.

## Integration decisions

- Retain RTSP, Responses image mode, Home Assistant control policy, dashboard authentication and 8-way concurrent window processing.
- Preserve fork schema v4 authentication/token-usage reconciliation; migrate upstream task runtime fields as fork application schema v5. Already-migrated task actions are retained on repeated startup and missing-version recovery.
- Apply the fork action validator to the new task action endpoint, and include the HA scene alias in the execution cooldown guard. Tests reproduced the policy bypass before the fix.
- Adapt upstream test calls to fork media arguments and the concurrency fixture to the new effectively-enabled-rule API. No test threshold was weakened.

## Validation

| Scope | Result |
| --- | --- |
| Backend, including database migrations, auth, camera, HA and 4/8-way concurrency | 4995 passed, 1 skipped |
| CLI | 680 passed |
| Web | 525 passed, 1 skipped; production build passed |
| OpenClaw plugin | 191 passed; typecheck and build passed |
| Changed MIoT subscription units | 75 passed |
| Native/Docker deployment, scripts and Hermes | 428 passed, 2 skipped |
| Changed Python files | Lint passed |
| Native deploy.sh build | Passed, exact-SHA payload and receipt created |
| Independent merge review | No remaining blocking findings |

Backend e2e/agent directories require a running environment and are excluded from local qualification. Live SDK integration tests requiring unavailable external credentials were stopped; changed MIoT subscription units passed separately. Existing warning output and one transient model-load timing failure were observed; the complete final backend run passed without changing thresholds.

## Approval and deployment boundary

CO `CHG260911002` (id1898): `Assess`, risk `High`, impact `High`, `AI Denied`, approver `AI+Lynx`, awaiting Lynx approval. The status endpoint provides no detailed rejection rationale. The existing CO remains for user handling; there was no replacement or bypass.

No production SSH, deployment preflight, restart, database migration or camera/device changes were performed. Production version, health and retention of settings have not been freshly measured in this task. The planned deployment preserves concurrency8, timeout/shared-window180 and the user's retain-on-failure policy, with no automatic rollback or four-way fallback.

After user approval, recheck live Implement/active PAM and exact payload binding, deploy the candidate through native deploy.sh, verify the release/assets/auth/configuration/database, and observe ten minutes of natural camera activity. Close the CO only after the actual result is known. See [progress](2026-09-11-upstream-main-merge_PROGRESS.md) for local artifacts and continuation details.
