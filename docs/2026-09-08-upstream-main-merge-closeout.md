# Lynxloco upstream merge and production closeout — 2026-09-08

Result: **Deployment and functional acceptance passed; queue capacity limitation remains**. Production candidate retained; no rollback and no concurrency fallback.

## Source and deployed components

- Upstream: XiaoMi/xiaomi-miloco main `646d19e7380c66c4a8db4ac2e14737938b376bdf`, all37commits since the previous upstream point.
- Merge: `c1018bc5de9eb77b878e0c74cd036b2ce87c787b`; deployed candidate: `cb87f6c0ea17b0ea6b70d3172d12af37da4d7568`. Candidate published to IIIIOvOIIII/lynxloco main and remote SHA verified. Subsequent closeout-only commits do not change the deployed source.
- Backend/CLI `2026.8.6.post1.dev350+gcb87f6c0e`; plugin `2026.8.6-post1.dev350` loaded from registered native path with gateway RPC healthy. OpenClaw host2026.7.1-2 retained.
- New detector SHA256 `eb55fff61225c1e4d90312a0f70f675ce19632bae1b51b948a3c8dc96765bf2f`; allfive packaged models and34plugin files match the built manifest.
- Grok4.6, Responses endpoint http://ai.esxi:18090/v1, concurrency8, active/savedHTTPtimeout180seconds, sharedwindow180seconds. Miloco config preserved byte-for-byte, schema5. RTSP, HA, dashboard auth and fork concurrency functionality preserved.

## Changes

Upstream contributes a hierarchical state store and MIoT startup alignment, updated detector, and OpenClaw2026.8-compatible notification/session handling and plugin packaging. The only merge conflict was .gitignore; both upstream and fork exclusions were retained. Independent source review found no blocking issue.

The existing native deploy previously updated only backend/CLI. It now builds and delivers the matching plugin and models through deploy.sh, with a supplementary digest-bound payload, version/hash validation, asset backups and loaded-plugin/RPC checks. Docker package selection and its allowlist are unchanged. User-directed retain mode covers partial install failures; manual rollback remains a later explicit user decision.

## Verification

Backend4477passed/1skipped; CLI663passed; web523passed/1skipped and production build; OpenClaw191passed plus TypeScript check/build; scripts24passed; native deployment19passed; existing Docker deployment contract198passed; changed Python lint and detector CPU inference passed. The initial backend invocation omitted the CI-style synthetic test service token; the affected slice and full suite passed after supplying the synthetic token. No production authentication was weakened.

CO CHG260908003 completed read-only runtime discovery and closed Successfully Closed. CO CHG260908004 is Successfully Closed after completion. It approved the exact candidate with active PAM; preflight, deploy and verification ran through the native deploy.sh with MILOCO_OPENCLAW_FAILURE_POLICY=retain. Release backup: `/opt/miloco-openclaw/backups/cb87f6c0ea17b0ea6b70d3172d12af37da4d7568`.

## Fixed ten-minute natural-input observation

Window: 2026-09-08T21:19:10+08:00 to 2026-09-08T21:29:10+08:00.

| Metric | Result |
|---|---:|
| Processed camera windows | 148 |
| Gate-skipped windows | 82 |
| Model requests | 63 |
| Successful model windows | 62 |
| Model request errors | 1 |
| Problem request-window rate | 1.59% |
| Successes per minute | 6.20 |
| Dropped complete windows | 132 |
| Drop rate under corrected definition | 47.14% |
| Actual concurrent HTTP peak | 8 |
| Healthy sampled checkpoints | 9/9 |
| Process remained stable | True |
| Enabled RTSP sources advanced frames | True |

Upstream state alignment completed for95devices, wrote226properties and recorded no alignment failures in the checked log tail. Dashboard returned200; anonymous protected camera API returned401; existing authentication setup retained. OpenClaw channels, agents, models, gateway settings and Miloco plugin settings were preserved; only its metadata section changed.

Successful cycle processing latency: median78.4s, P95145.5s. HTTP P95102.3s. The single CancelledError had179.900s cycle time plus0.101s input delay, consistent with the180s window limit; its slot wait was59.953s and HTTP stage115.976s. There were12successful cycles above120seconds. Natural Agent calls recorded0; no notification was manually sent.

RSS peaked at2207MiB in the nine samples and finished1784MiB; available memory never fell below2676MiB at sampled checkpoints. The last backend PID remained1298569. These are sampled measurements.

## Limitations

This is a deployment/functionality observation with uncontrolled household activity, not a recall/false-positive benchmark or an all-day stability result. Completed-cohort metrics exclude in-flight windows and work completed after the fixed endpoint. There were no manually triggered device actions or notification-send tests; no synthetic Grok requests were added. Any request errors or missing natural events are reported in the evidence rather than hidden by rollback or a second trial. No later rollback is authorized by this closeout.

Full sanitized evidence: [evidence directory](evidence/2026-09-08-upstream-main-merge/README.md).
