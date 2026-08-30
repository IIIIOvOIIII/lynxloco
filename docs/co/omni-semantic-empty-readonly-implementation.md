Read-only investigation for the latest Miloco Omni diagnostic showing an empty structured result on `miloco.esxi`.

Steps:

1. Verify the Miloco service health and running package version.
2. Inspect bounded, recent backend logs for sanitized Omni/perception diagnostics.
3. Query Miloco local APIs with the existing service token only inside the production host shell, without printing or storing the token.
4. Inspect the runtime summary, latest perception counters, and database row counts for recent perception logs and meaningful events.
5. Classify whether the empty structured result is an expected no-event state or a contract/runtime defect.
6. Do not modify files, services, camera configuration, model configuration, secrets, database state, or OpenClaw settings under this read-only change.
