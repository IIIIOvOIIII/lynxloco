# 2026-08-29 Docker ESXi Miloco Deployment Progress

## 2026-08-29 00:00 SGT

- Current work: Prepare a minimal repeatable `docker.esxi` production deployment target for Miloco after lab01/lab02 acceptance.
- Expected result: Add a project-controlled deployment path for `docker.esxi:1811` before opening the production CO.
- Result: In progress. User approved the minimal repeatable deploy-script support path, with port `1811`, direct HTTP, no Apache/FQDN/SmartDNS, no secrets, no real RTSP camera, and no real local VLM configuration in scope.
- Next step: Add TDD deployment-contract coverage, implement host profiles, commit, then open an exact-SHA CO and deploy only after `state=Implement` plus active PAM.

## 2026-08-29 10:20 SGT

- Current work: Implemented the approved minimal `docker.esxi` deployment target in the existing Miloco deployment controller.
- Expected result: Keep ai-lab01/ai-lab02 behavior unchanged while adding a production profile for `docker.esxi` using `/opt/miloco`, HTTP port `1811`, Compose project `miloco`, and production image names.
- Result: Achieved locally. The focused docker.esxi deployment-profile tests passed, followed by the full deployment-contract suite (`184 passed`), script syntax validation, whitespace/diff checks, and project-level local CI (`全部通过 (6 项)`, with known macOS-only skips).
- Next step: Commit the deploy-target support, build the exact final SHA archive, then create a production CO and deploy only after approval and active PAM.

## 2026-08-29 10:31 SGT

- Current work: Deployed Miloco to production host `docker.esxi` under CO `CHG260829002`.
- Expected result: Exact deployed source SHA `5fd053e620636c32dd0f7c439d25bd40a743d965` runs from `/opt/miloco` on HTTP port `1811`, with no Apache/DNS/certificate/RTSP/VLM secret changes.
- Result: Achieved. CO was approved by AI, PAM became active for `docker.esxi/root`, `deploy.sh preflight` passed, `deploy.sh deploy docker.esxi` completed, remote acceptance passed (`22 passed`), `deploy.sh verify docker.esxi` returned the expected SHA, `deploy.sh status docker.esxi` reported `image=miloco:5fd053e620636c32dd0f7c439d25bd40a743d965 health=healthy`, and `http://docker.esxi:1811/health` returned `{"status":"ok"}`. CO `CHG260829002` was closed as `Successfully Closed`.
- Next step: Optional follow-up is to provide real RTSP camera/VLM configuration for production validation; the current production deployment is direct HTTP only on port `1811`.
