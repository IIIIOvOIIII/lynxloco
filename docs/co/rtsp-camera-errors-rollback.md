# Rollback plan for Miloco RTSP camera error repair

Rollback is only needed if a production mutation is performed and worsens service health or camera runtime state.

1. If only read-only diagnostics are performed, no rollback is required.
2. If only the existing Miloco service is restarted and health does not recover, restart the same service once more through the installed supervisor/CLI path and verify `/health`.
3. If a configuration mutation is made, restore the CO-specific pre-change backup of `/root/.openclaw/miloco` or the narrower backed-up `config.json` file, depending on the mutation scope.
4. If a code/package deployment is performed and validation fails, reinstall the previously deployed package artifacts or rebuild the exact previously deployed source SHA recorded during pre-checks, using the same official installer/sync path. Do not overwrite persistent `/root/.openclaw/miloco` configuration unless the failed change explicitly mutated it.
5. Verify local and LAN Miloco health, OpenClaw gateway/plugin health, and sanitized camera summaries after rollback.
6. Stop and report if the rollback path would require unknown RTSP credentials or broader host cleanup outside this CO.

