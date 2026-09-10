# LynxLoco upstream main merge progress

## 2026-09-11 00:27 +08

- Current work: Merging 111 commits from upstream/main 75aed171 into fork e8ff7243 in an isolated worktree.
- Expected result: Preserve fork integrations and deliver verified exact-source native deployment under CO.
- Result: Partial. Upstream fetched; GitHub HTTPS credential helper works. Six conflict files identified. Fork and upstream both use application schema v4 for different changes; task migration must advance to v5. No production access yet.
- Next step: Resolve conflicts, test both database histories and full affected suites, build and request exact-SHA CO.

## 2026-09-11 00:37 +0800

- Current work: Final merge qualification and exact native release preparation.
- Expected result: All upstream behavior and fork integrations coexist before production access.
- Result: Six conflicts resolved. Fork application migration advances v4 to v5 without replacing upstream-migrated task actions. New task static actions reuse fork HA cooldown checks; runtime scene alias guard retained. Focused migration/empty-input52, HA/rule/task374, MIoT subscription75, CLI680, web525+1skip, OpenClaw191+typecheck and deployment/scripts/Hermes428+2skip passed. Changed Python lint passed. Full backend final run passed: 4995 passed, 1 skipped in 176.91 seconds. Final database and HA integration review has no remaining blocking findings. Initial failures were upstream test-signature changes, stale concurrency test fake, worktree script permissions and transient model-load timing; no lowered thresholds. SDK integration tests requiring external credentials were stopped; changed subscription units passed.
- Next step: Finish backend qualification, commit/build/push, request exact-SHA CO. No production access performed.

## 2026-09-11 00:42 +0800

- Current work: Upstream merge published and release built; production approval blocked.
- Expected result: Obtain approval, deploy exact candidate and verify natural camera operation.
- Result: Partial. All 111 upstream commits through 75aed171 are ancestors of merge 71dacdcb422e8bde660efe1c4e737b7b6fec5661, published and verified on origin/main. Native backend/CLI dev463 and plugin463 built successfully with immutable schema2 receipt. CHG260911002 (id1898) is Assess, High risk/High impact, AI Denied, AI+Lynx, awaiting Lynx approval. The status endpoint supplies no detailed rejection rationale. No production SSH, preflight, deployment, restart, migration, device action or rollback occurred. CO is retained for the required human decision; no replacement CO or bypass.
- Next step: User handles CHG260911002 in ITSM. After user confirmation, freshly require Implement/active PAM, verify-deploy against the saved payload/receipt and exact 71dacdcb422e8bde660efe1c4e737b7b6fec5661, then use native deploy.sh with retain policy and perform ten-minute observation. Deployment completion remains unmeasured.

## Resume pointers

- Clean candidate checkout: `/Users/nicholasliao/clawd/xiaomi-miloco/.worktrees/upstream-main-20260911` at `71dacdcb422e8bde660efe1c4e737b7b6fec5661`. Main may advance with documentation only; do not rebuild or deploy a docs SHA by mistake.
- Immutable local CO directory in that checkout: `docs/co/2026-09-11-upstream-main/` contains payload.json, receipt.json, implementation.md, rollback.md and a read-only inspect.py.
- Exact native release: `dist/lab/71dacdcb422e8bde660efe1c4e737b7b6fec5661/`.
- Post-approval command: `python3 /Users/nicholasliao/.agents/skills/lynx-skills/itsm-co/scripts/itsm_co.py verify-deploy CHG260911002 --payload-file docs/co/2026-09-11-upstream-main/payload.json --receipt-file docs/co/2026-09-11-upstream-main/receipt.json --expected-sha 71dacdcb422e8bde660efe1c4e737b7b6fec5661 --host miloco.esxi --user root`.
- Native environment: `MILOCO_DEPLOY_RUNTIME=openclaw MILOCO_DEPLOY_PRODUCTION_HOST=miloco.esxi MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw MILOCO_OPENCLAW_FAILURE_POLICY=retain`. Use deploy.sh preflight/deploy/verify as the approved plan requires.
