# Model concurrency settings and performance cards

RESULT: COMPLETE

## Boundary

- Canonical workdir: `/Users/nicholasliao/clawd/xiaomi-miloco/.worktrees/model-concurrency`.
- Repository: `feature/model-concurrency`, initial HEAD `5847873d1ee90978759b92e4c532555d734d29e7`.
- Scoped implementation only. No staging, commits, pushes, production access, credentials, or subagents.
- Main owns observability/backend integration; runtime worker owns perception. Their existing/concurrent edits were preserved.
- Main explicitly expanded web scope to `PerfKpiCards.tsx`, performance types/translations and related tests.

## Changes

- `backend/miloco/src/miloco/config/settings.py`: `model.omni.concurrency`, default 1, strict integer 1–8; applies equally to saved profiles.
- `backend/miloco/src/miloco/admin/router.py`: public active/profile values, persisted save/rename/activate roundtrip, missing request field preserves previous value, old profiles default 1. Explicit concurrency editing of current model skips preflight only when model/URL/protocol/key stay identical; activation and changed connection still validate. Preserves breaker health and existing timeout on concurrency save.
- `backend/miloco/tests/admin/test_omni_config.py`: API/schema range/type checks, roundtrip 8/4, legacy omission, saved profiles retained through other writes, active-only update without probe/deactivation, changed connection still preflighted.
- `web/src/components/UsageOmniConfig.tsx`, `web/src/api/real.ts`, `web/src/lib/types.ts`, `web/src/i18n/locales/{en,zh}/usage.json`: accessible numeric control with 1–8 limits, prefilled saved value, validation, save payload, profile/current display and cost/load helper. Existing model-list loading on edit is retained; no new inference test or activation call on save.
- `web/src/components/PerfKpiCards.tsx`, `web/src/lib/types.ts`, `web/src/i18n/locales/{en,zh}/perf.json`: corrected camera-window/expected-window counts plus batch subtitle, old-history warning, nullable drop rate shown as unavailable, success P95 restricted to actual successful Omni samples, slowest-camera timing explanation, completed agent runs wording. Optional fields retain old API compatibility; zero corrected counts are not replaced with legacy counts.
- `web/tests/usageOmniConfig.test.tsx`, `web/tests/perfKpiCards.test.tsx`: input SSR and real API transport contract, corrected/legacy/mixed performance rendering.

## Verification

- Backend RED: new 15 concurrency tests failed before implementation for missing concurrency, invalid values accepted, and unwanted active-edit probe.
- Frontend RED: 2 model tests failed before implementation (control absent; outgoing concurrency dropped); 5 performance tests failed against old card behavior including null becoming 0.0%.
- Initial backend run without synthetic auth failed at existing authentication boundary. Parent diagnosed test harness requirement; no production auth was modified.
- Backend GREEN, from `backend`:
  `MILOCO_SERVER__TOKEN=miloco-local-test PYTHONPATH=miloco/src:miot/src /Users/nicholasliao/clawd/xiaomi-miloco/backend/.venv/bin/python -m pytest miloco/tests/admin/test_omni_config.py -q --tb=short`
  → **77 passed**, existing Starlette/httpx deprecation warning.
- Frontend GREEN, from `web`:
  `npm test -- --run tests/perfKpiCards.test.tsx tests/usageOmniConfig.test.tsx tests/omni-protocol-form.test.ts tests/i18n.test.ts`
  → **156 passed, 1 skipped** (existing skip); Node localStorage experimental warning.
- `npm run build` → passed; existing >500 kB bundle warning.
- Scoped `ruff check` on the three changed Python files → passed.
- `git diff --check` → passed.

## Remaining verification

- Actual browser interaction and production/provider behavior are not measured by this worker. UI evidence is rendered markup and real API serialization; backend API tests use isolated config and mocked external preflight.
- Main should independently review and integrate with runtime/metrics workers, then perform the agreed integrated acceptance.
