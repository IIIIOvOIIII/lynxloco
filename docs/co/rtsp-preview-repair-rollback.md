Rollback Miloco RTSP preview/runtime repair on `miloco.esxi` if deployment or validation fails.

1. Stop the rollout immediately if Miloco health, OpenClaw gateway/plugin health, API authentication, or camera status worsens after installation.
2. Reinstall the prior deployed Miloco release from exact Git commit `0ed57bebd763b002cfadfecb66bb4ebd28c19609` by building that commit in an isolated local worktree and running the same `scripts/sync-to-remote.sh --local-build` install flow against `miloco.esxi`.
3. Restart only the existing Miloco/OpenClaw managed services needed to restore the prior package set.
4. Do not restore or overwrite the persistent Miloco configuration, Xiaomi account state, RTSP camera definitions, model API keys, or database unless separate evidence proves the deployment changed them.
5. Re-run the same post-rollback checks: Miloco `/health`, Miloco CLI service status, OpenClaw gateway/plugin status, and safe camera status sampling.

Secrets and privacy boundary: do not print, persist, or copy RTSP URLs, usernames, passwords, service tokens, Xiaomi tokens, model API keys, raw camera frames, or raw model responses during rollback.
