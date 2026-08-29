# 2026-08-29 Docker ESXi Miloco Deployment Progress

## 2026-08-29 00:00 SGT

- Current work: Prepare a minimal repeatable `docker.esxi` production deployment target for Miloco after lab01/lab02 acceptance.
- Expected result: Add a project-controlled deployment path for `docker.esxi:1811` before opening the production CO.
- Result: In progress. User approved the minimal repeatable deploy-script support path, with port `1811`, direct HTTP, no Apache/FQDN/SmartDNS, no secrets, no real RTSP camera, and no real local VLM configuration in scope.
- Next step: Add TDD deployment-contract coverage, implement host profiles, commit, then open an exact-SHA CO and deploy only after `state=Implement` plus active PAM.
