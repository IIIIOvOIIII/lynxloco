# Miloco 180-second / 8-way implementation and acceptance plan

User instruction supersedes the earlier 120-second, 8-to-4 fallback and automatic rollback policy. Approved design: raise production window age to180seconds, set active Grok profile request timeout180seconds and concurrency8, evaluate once, retain the resulting production state regardless of acceptance result. Only the user decides whether to roll back.

## Scope and sequence

- [x] Add a boundary test showing a150-second-old window remains actionable and a window older than180seconds does not; change the shared runtime window-age budget from120to180. Runner lifetime, predecessor wait, result applicability and request limiter all use that common budget. The existing model timeout parameter is explicitly set to180 in production active and matching saved Grok profile; do not rely on the default120 HTTP timeout.
- [x] Add an opt-in native deployment failure policy that records and retains failed candidate state without calling restore. Keep the existing rollback command available only for later explicit user approval. Test simulated partial install failure with retain enabled and verify the normal default behavior remains covered.
- [x] Run focused runtime/lifecycle and native deployment regressions, build an immutable committed/pushed candidate and receipts, create a new exact-SHA Software CO for miloco.esxi and read-only ai.esxi audit attribution.
- [x] After CO approval, refresh non-secret runtime model metadata and native preflight. Deploy via deploy.sh with retain-on-failure policy. Set active and matching saved Grok configuration to concurrency8/timeout180 using locked atomic backup/write/readback; restart current backend with native CLI if required. Never invoke rollback or reduce to4 in this cycle.
- [x] Observe one fixed30-minute real-camera window with unchanged input/model/queue settings, starting after healthy configured service. No extra synthetic requests: the same endpoint/visual payload was already40/40 at8 in the previous approved cycle. Normal camera/rule/Agent traffic is authorized; no manually triggered device action. Report actual HTTP peak, successful completed windows/minute, request errors/deadlines, drop counts, stage timings, video-frame metadata, Agent/event counts, health and memory.
- [x] Retain prior operational comparison (>1.02successful windows/minute and <=10%request-error/deadline rate) only to classify the result. Neither failure nor inconclusive input triggers rollback. If deployment or health fails, retain state and backups, report the problem for user decision. No further trial without renewed scope.
- [x] Publish final evidence, actual deployed SHA/config and CO closeout. The final response summarizes results and leaves rollback decision to the user.

## Implementation choices and bounds

A shared180-second window plus explicit180-second HTTP timeout is selected over changing only HTTP timeout (outer120-second cancellation would still win) or changing only window age (HTTP could still stop at120seconds). Queue depth and reasoning strength are not changed in this comparison.

Budget: one candidate deployment, one30-minute live observation, zero additional synthetic model requests; local focused tests/build. Any repair attempt is bounded by the existing two-attempt-per-failure-class rule. The no-rollback instruction applies to both acceptance orchestration and the native deployment exception path. A rollback may be executed later only after explicit user confirmation and valid CO/PAM.

Local verification: new150/180-second boundary RED then GREEN; 101 focused runtime/inference/client/settings tests passed. Native deployment suite12passed, including partial-install retention without restore and later explicit rollback; explicit180-second HTTP timeout propagation covered. Initial three failures were old121-second expired fixtures, updated to181seconds under the new user-selected boundary.

Final result: CHG260906006 Successfully Closed; deployed96c018f5df06e401e7849bad271699913e5d6fb5. One30minute8/180window passed:153/154successful,0.65%errors,5.10success/min,actualpeak8,health200.15successfulcyclesand2HTTPrequests exceeded120seconds. Candidate retained; no rollback. Full result: [closeout](2026-09-06-model-concurrency-180s-closeout.md).
