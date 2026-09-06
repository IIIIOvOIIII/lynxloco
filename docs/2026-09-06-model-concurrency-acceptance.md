# Miloco concurrency release acceptance

User-authorized order: production candidate uses Grok concurrency8 first; only failure triggers concurrency4 fallback. No ascending1/2/4/8 provider experiment in this run.

## Frozen acceptance before deployment

- Local correctness: real runner→processor→proxy→persistent worker→engine→delayed HTTP integration exercises8 overlapping same-camera jobs, strict cap, resize8→4 and1↔8 without state regression, ordered effects and one persisted deadline/gap trace. Existing backend/CLI/frontend/native-deployment regressions must pass.
- Endpoint semantics: forty fixed in-memory synthetic labelled cards under the deployment CO, at8 actual in-flight requests, all must match the visual/JSON contract. No retries; at most40requests/10minutes. This proves the synthetic contract, not real people/pet accuracy or representative camera latency.
- Avoid adding a second8-request pool beside live Miloco during the synthetic test: after native candidate deployment/preflight, temporarily stop only miloco-backend with the existing supervisor under the same maintenance CO, run the synthetic trial, then restart with configured8 (or4 on failure). Other gateway clients are not changed. The normal backend is restored before live observation; synthetic downtime is bounded by the10-minute deadline per authorized trial.
- Live camera observation: one fixed30-minute window at8. Health remains200; no OOM/process failure; input sources remain available; actual HTTP cap is not exceeded; same-camera windows overlap; encoded payload and waiting limits remain bounded; no stale/double action regression appears. Snapshot actual peak, do not equate configured8 with8 continuously busy. Natural low activity cannot prove full saturation; local full-path8 and endpoint smoke provide the controlled concurrency evidence.
- Effective throughput: parsed successful non-skipped camera-window completions per minute exceeds the saved pre-change last-hour61successful gateway requests/hour (about1.02/min), with original source/gate activity differences reported. This is an operational comparison, not a controlled estimate of event recall. Read model/HTTP stage times separately from queue/reorder wait.
- Reliability: combined actual-request error/deadline rate at most10% in the fixed live window. This uses the preflight6/67canceled/error baseline (about9%) as the operational reference; it is not a statistical proof of equivalence. If input has insufficient active samples, report inconclusive rather than invent success.
- Fallback: any material8way acceptance failure selects4, retaining all8way evidence. Run at most one40-card4way endpoint trial and one30-minute4way live window. If4fails too, restore prior stable runtime/config through native rollback(candidate transaction SHA), preserve new observability records, and report acceptance failure. Never repeat8or4to cherry-pick a pass.
- Normal configured rules remain operational during live camera observation. Synthetic endpoint checks do not invoke Miloco rules/Agent/device actions. No manually triggered real HA/device actions are part of this acceptance.
- Budget: at most80extra synthetic model requests and two30-minute live observation windows. Ongoing normal camera usage is measured separately. Only concurrency fields are changed in active Grok profile and matching saved profile; identity, credentials, camera/HA settings and plugin remain preserved.

## Publication and evidence

Candidate source must be committed/pushed, native build receipts bound to exactSHA, Software CO/PAM approved for miloco.esxi and ai.esxi, and verify-deploy must pass before mutation. Record baseline/candidate versions, backup path, configuration field change, source health, native install verification, synthetic JSON report, fixed-window aggregates and finalCOclosure.

Real household event recall has no labelled ground truth in a natural30-minute window. Synthetic shape labels and controlled rule/state fixtures are separate evidence; do not claim a measured household漏检率 merely because coverage or Agent count increases.
