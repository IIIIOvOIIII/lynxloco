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
