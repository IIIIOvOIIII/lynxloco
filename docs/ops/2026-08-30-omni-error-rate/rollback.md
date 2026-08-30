Rollback trigger conditions

1. Miloco backend health becomes non-OK after the repair attempt.
2. OpenClaw gateway or plugin access is degraded by the repair attempt.
3. Omni health worsens from a recoverable/configuration error to service outage, authentication failure, or repeated backend crashes.
4. Any step would require printing or storing secrets, raw provider responses, RTSP URLs, Xiaomi tokens, service tokens, or camera imagery.

Rollback steps

1. If only a retry action was performed, no server-side rollback is required; stop further retries and record the observed state.
2. If a configuration file was changed, restore the pre-change backup for the exact file changed and restart the affected service.
3. If a service restart caused degradation, restore the prior known-good service state by restarting the previously active Miloco/OpenClaw service only.
4. If a code defect is identified, do not deploy code under this Change Order; preserve the current runtime and prepare a separate exact-SHA deployment Change Order.

Rollback verification

1. Confirm Miloco /health returns OK.
2. Confirm the OpenClaw gateway and Miloco plugin are reachable.
3. Confirm the active Omni profile safe metadata matches the pre-change state, with API keys redacted.
4. Close the Change Order truthfully as Successful, Failed, or Not Executed according to the actual outcome.
