# Dashboard Auth Progress

## 2026-08-30 23:26 HKT

- Current work: Confirmed the native dashboard-auth direction after the user approved方案 B.
- Expected result: Produce a written design specification before implementation.
- Result: Achieved. Design document created at `docs/superpowers/specs/2026-08-30-dashboard-auth-design.md`.
- Next step: Commit the design specification, ask the user to review/approve it, then write a detailed implementation plan.

## 2026-08-30 23:34 HKT

- Current work: Wrote the detailed implementation plan after the user approved the written dashboard-auth specification.
- Expected result: Produce a task-by-task plan that a future worker can execute without re-discovering auth boundaries, file ownership, tests, and deployment gates.
- Result: Achieved locally. Plan created at `docs/superpowers/plans/2026-08-30-dashboard-auth.md`; self-review found and tightened CSRF/session interfaces and dependency-test examples before handoff.
- Next step: Commit and push the implementation plan, then wait for the user to choose Subagent-Driven or Inline execution.

## 2026-08-31 01:13 +08

- Current work: Completed Task 8 local integration, documentation alignment, and release-readiness checks for native dashboard authentication.
- Expected result: A locally verified source tree ready for final review before the production CO.
- Result: Partial — the auth-focused backend gate passed (87 tests, including auth, SSE, devices, settings, and the schema-v3 migration); CLI auth/client/commands passed (192 tests); frontend focused tests passed (13 tests), along with typecheck and production build; the clean-room smoke served `web/dist` through a temporary backend on `127.0.0.1:1819`, confirmed root HTML contains neither a synthetic service token nor the retired placeholder, and returned `needs_setup=true` / `authenticated=false` from `/api/auth/status`. `git diff --check` passed. `./scripts/local-ci.sh --tests` completed Hermes tests (187 passed, 2 skipped) and installer shell syntax, but its backend-wide stage failed with 299 tests because it deliberately sets `MILOCO_SERVER__TOKEN=''` while many non-auth legacy endpoint tests issue unauthenticated requests; a representative `admin/debug` case now correctly raises `Authentication required` under that empty-token environment. That broad test migration is outside the dashboard-auth Task 8 scope. The passing focused gates above are the safe equivalent for this change.
- Next step: Final review the committed source tree, then open a production CO for `miloco.esxi`, back up runtime data, deploy the exact reviewed SHA through the approved path, and verify setup/login plus service-token callers in production.

## 2026-08-31 01:26 +08

- Current work: Completed Task 8 fix round 1 for the red repository-wide regression gate and the unsafe Vite development proxy.
- Expected result: `./scripts/local-ci.sh --tests` is green, legacy endpoint tests authenticate with synthetic service credentials, explicit auth-negative tests stay anonymous, and no development browser proxy can add `server.token`.
- Result: Achieved. The full local-CI gate passed all six checks: backend tests, Hermes tests (187 passed, 2 skipped), and installer shell syntax. Backend legacy endpoint clients now receive a synthetic local-CI Bearer credential by default while auth/SSE/camera boundary suites remain opt-out and retain their anonymous/wrong-token assertions. Vite is loopback-only, preserves browser cookie/CSRF auth, has no token-reading/injection hook, and no longer exposes `pnpm dev`. Targeted Vite auth-boundary tests, typecheck, and production build passed.
- Next step: Final review the updated committed source tree, then open a production CO for `miloco.esxi`, back up runtime data, deploy the exact reviewed SHA through the approved path, and verify setup/login plus service-token callers in production.

## 2026-08-31 01:35 +08

- Current work: Completed Task 8 fix round 2 to align the repository backend CI job with the authenticated legacy-client test boundary.
- Expected result: Push-time CI and the local wrapper both use a synthetic test-only credential while runtime `server.token` remains empty-token fail-closed.
- Result: Achieved. `.github/workflows/ci.yml` now supplies an isolated config search path and `local-ci-service-token` only to the backend test step. A faithful local invocation of that exact environment and `uv run pytest -v` passed 4033 tests with 1 expected skip. The explicit empty-service-token auth dependency test still passed (4 tests), and `./scripts/local-ci.sh --tests` passed all six checks.
- Next step: Final review the updated committed source tree, then open a production CO for `miloco.esxi`, back up runtime data, deploy the exact reviewed SHA through the approved path, and verify setup/login plus service-token callers in production.

## 2026-08-31 01:53 +08

- Current work: Completed the final whole-branch review and the single permitted final-review fix wave for dashboard authentication.
- Expected result: The branch is ready to merge after final scoped re-review.
- Result: Partial. The final fix wave resolved the reviewed CSRF multi-tab instability, public password endpoint abuse controls, frontend stale-auth transition, logout CSRF enforcement, and related router test gaps in commit `faebbbe0912d6ce67f018998ac47d726d8b019df`; the fixer reported backend auth, related SSE/camera suites, frontend auth/users tests, typecheck/build, scoped Ruff, `git diff --check`, and full `./scripts/local-ci.sh --tests` all green. The scoped re-review confirmed those original findings were addressed, but found one new fix-introduced important defect: sanitized Pydantic validation responses still include non-JSON-serializable `ctx.error`, so whitespace-only setup username validation can turn the intended HTTP 422 into HTTP 500.
- Next step: Request one explicit extra final-review fix authorization, then patch the validation-error sanitizer, add a focused regression, rerun auth-focused tests and `./scripts/local-ci.sh --tests`, and re-review the small fix before merge/push.

## 2026-08-31 07:33 +08

- Current work: Completed the user-approved extra targeted final-review fix for validation-error serialization.
- Expected result: Pydantic validation errors remain JSON-serializable, raw request inputs stay redacted, the whitespace-only setup username case returns HTTP 422 instead of HTTP 500, and the branch is ready for merge/push preparation.
- Result: Achieved. Commit `c9aea50f4a94147565d04e4698b704e473f55c56` recursively sanitizes validation error details, omits non-serializable nested objects such as `ctx.error`, preserves raw `input` redaction, and adds a focused regression for whitespace-only setup usernames. The implementer reported the focused regression, backend auth suite, scoped Ruff, full `./scripts/local-ci.sh --tests`, and `git diff --check` all green. Scoped re-review confirmed the sanitizer finding is addressed and found no new Critical/Important breakage.
- Next step: Prepare the branch for merge/push publication. Production deployment to `miloco.esxi` remains a separate CO/PAM-gated action with pre-deploy data backup and browser/CLI smoke verification.

## 2026-08-31 09:20 +08

- Current work: Prepared the merged dashboard-auth release for production deployment after publishing `main`.
- Expected result: The production deployment controller targets the current standalone Miloco VM rather than the retired Docker-host deployment, without touching application configuration or runtime data paths.
- Result: Achieved in the working tree. The first RED deployment-contract run failed because the controller still allowed the old `docker.esxi:1811` production profile and rejected `miloco.esxi`. The minimal fix changes the production target to `miloco.esxi`, keeps the production root at `/opt/miloco`, uses port `1810`, and preserves the `4096m` memory profile. The focused RED-to-GREEN tests passed, and the full deployment contract passed with `196 passed`.
- Next step: Commit and push the deployment-target correction, run the full release gate on the exact final SHA, then open a CO/PAM change for `miloco.esxi` that includes a pre-deploy `/opt/miloco/state` backup and explicit post-deploy proof that existing Miloco configuration remains present after dashboard-auth first-admin setup becomes available.
