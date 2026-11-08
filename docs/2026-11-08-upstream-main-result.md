# LynxLoco upstream merge — 2026-11-08

Completed source integration and publication of135upstream commits. This task did not deploy, open a CO, log into production, or modify production data/configuration.

## Source

- Upstream XiaoMi/xiaomi-miloco main: `05cdb1c77310afcfb77e64349bfb5efbeebded05`.
- Fork baseline: `a1d3be66606a63a0dc1d23d7ea066fa606644cfc`.
- Merge commit: `27e95598`; subsequent compatibility/test fixes are included in final code `1631ffb083f6a603b204e58257a39ca7fae3321a`.
- Published repository: https://github.com/IIIIOvOIIII/lynxloco, main. Remote SHA and upstream ancestry verified. Later documentation-only commits do not change this tested code.

## Compatibility work

Integrated upstream image inference/person grounding, action-to-event linkage, Mi Home authorization recovery, circuit/probe fixes, performance flow UI, native launcher packaging and related tests. Fork RTSP/HA/session authentication, explicit API protocols, keyless Responses isolation, bounded image payloads, stronger JSON/caption instructions,8-way concurrent windows and180second budget remain represented in code and regression tests.

Resolved substantive integration issues rather than choosing one branch wholesale:

- New native image mode does not replace the Responses adapter’s separate bounded image representation.
- New trace IDs and flow flags pass through the fork’s concurrent engine wrapper; cancellation/gap traces generate stable device IDs instead of raising TypeError.
- Mi Home authorization cutoff applies to MiOT sources, leaving RTSP discovery/recovery and HA independent. UI messages follow the same scope.
- Image artifact browser URLs use session cookies rather than reintroducing bearer tokens in queries.
- HA action validation and sanitized diagnostics coexist with upstream authorization/event-link handling.
- Existing whole-window/partial-window statistics remain intact alongside new upstream performance fields.

## Database history reconciliation

Application schema advances to6 because fork5 and upstream5 describe different changes. The existing fork task migration remains5;6 reconciles authentication/runtime shape before adding on-demand image artifact columns. Upgrades from both5histories preserve current task actions instead of re-copying stale rule actions.

Observability schema advances to7, retaining fork5concurrency history and converging upstream action linkage/auth-state columns. Existing upstream manual actions with NULLphase are not relabeled legacy. Native helper validation supports known4/5/7schemas and schema-checked additive marker restoration while preserving evidence. These paths were tested locally only; future deployment needs a fresh CO and runtime/data preflight.

## Validation

| Scope | Result |
| --- | --- |
| Full backend functional suite | 5859passed,1skipped |
| Isolated timing/concurrency tests | 33passed |
| Frontend | 755passed,1skipped; TypeScript/Vite build passed |
| CLI | 813passed,1skipped |
| OpenClaw plugin | 201passed; typecheck passed |
| SDK unit selection | 292passed |
| Deployment/scripts/Hermes contracts | 485passed,2skipped,15subtests passed |
| Backend production-source line coverage | 83.01% |
| Whole frontend-source line coverage | 22.64% |
| Lint | passed |

The full shared check invocation passed all application, lint and build stages, then found two deployment-contract test harnesses missing a stub for the new launcher function. Only that fixture changed; the complete contract group was rerun successfully. The original failed invocation record remains intact and is not described as an uninterrupted successful gate. Coverage floors remain the project’s81%/16%; frontend coverage still primarily represents pure functions/API/source contracts, not complete rendered browser behavior.

Dependency/deprecation and test-stub AsyncMock warnings remain nonfatal. Live credentials/devices/provider requests and rendered production UI were outside this source-merge task. No production readiness or successful database migration on the live server is claimed.

[Archived progress](archive/2026-11-08-upstream-main_PROGRESS.md) retains execution details; [validation evidence](evidence/2026-11-08-upstream-main/validation.json) records final results. Existing untracked `.codex_tmp/` and previous worktrees/artifacts were retained.
