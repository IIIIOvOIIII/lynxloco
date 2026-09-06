# CHG260906005 retry evidence

Two synthetic trials (40 requests each) and two fixed 30-minute camera observations. Both failed the frozen 10% request-error/deadline limit; approved native rollback verified.

`live-concurrency-*.json` selects new-definition traces started and completed within each fixed interval. `gateway-concurrency-*.json` selects gateway audit completion times, so its cohort may differ. `input-*` and `failure-timing-*` use the same completed-trace boundary. Agent and event summaries use their own event timestamps. No image, prompt, reply, credential or account value is included.

`read-only-queries/` contains the exact aggregate queries. SSH requires a newly active governed CO; the historical CHG260906005 is closed.

The earlier 404 trials under CHG260906004 are kept in the separate preceding evidence directory and are invalid capacity evidence.
