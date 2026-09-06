# Synthetic endpoint concurrency acceptance

**This is only a synthetic Responses visual-input/JSON-output contract check and a same-input concurrency smoke test. It is not representative of real camera latency, scene throughput, people recognition, pet recognition, or production accuracy.** The standalone sender does not exercise Miloco's runtime HTTP limiter. The main agent separately verifies that limiter and observes actual camera throughput for 30 minutes under the approved deployment CO.

## Fixed scope and budget

`scripts/accept-omni-concurrency.py` creates forty small PNGs entirely in memory, using deterministic seed `20260906`. There are ten red squares, ten blue circles, ten green triangles and ten blank white cards. Each request contains one actual Responses `input_image` data URL. No user camera images, Agent operations or device actions are used.

Every card has three expected labels: `object`, `color`, and Boolean `event` (true for a colored shape, false for blank). Expected answers are not included in the individual image request. The common instructions define the allowed vocabulary and JSON schema. Acceptance requires a completed Responses object with a nonempty canonical `output[].content[type=output_text].text`, exactly the three required JSON keys, correct value types, and the expected labels. Missing output, malformed JSON, duplicate keys, extra keys, Boolean coercion and incorrect labels fail. An HTTP 200 or `status=completed` alone does not pass.

Each run sends at most forty requests, with no retries or redirect following, a fixed 600-second total deadline, and at most 120 seconds per request. Concurrency must be an integer from 1 through 8. HTTP connection limits and the worker count share the same cap. Pending requests are canceled at the global deadline; unsent cards are reported separately.

The deployment sequence is one run at concurrency **8**. Only if that run fails, the main agent may invoke one fresh run at **4**, using the identical forty cards and a separate output file. The script never falls back automatically. These are at most two runs / eighty requests / twenty minutes total. Failure at 4 stops acceptance; do not add retries or change the dataset mid-run.

## Invocation and credentials

Use the existing backend Python environment. The key is read **only from standard input**, until EOF. The script does not read key environment variables and has no key argument. Do not use an on-disk key file, a shell history command containing the key, or a command that prints it. The main agent owns the approved Vault retrieval and pipe delivery.

Exact invocation, with standard input supplied by that approved credential pipe:

```sh
/Users/nicholasliao/clawd/xiaomi-miloco/backend/.venv/bin/python \
  /Users/nicholasliao/clawd/xiaomi-miloco/.worktrees/model-concurrency/scripts/accept-omni-concurrency.py \
  --base-url "$RESPONSES_BASE_URL" \
  --model "$RESPONSES_MODEL" \
  --concurrency 8 \
  --output "$EVIDENCE_DIR/synthetic-concurrency-8.json"
```

`--base-url` is the provider base, for example an approved URL ending in `/v1`; the script appends `/responses`. Query strings, fragments and embedded URL credentials are rejected. An empty stdin intentionally selects a keyless endpoint; this must match the approved endpoint. The command does not retrieve credentials itself.

If and only if the 8-run fails, use the same invocation with `--concurrency 4` and `--output "$EVIDENCE_DIR/synthetic-concurrency-4.json"`. Keep both reports for review. A successful 8-run requires no 4-run.

## Output and interpretation

The JSON file and standard output contain only dataset/scope identifiers, counts, fixed label match counts, timing, HTTP statuses, fixed error classes, concurrency measurements and per-window result metadata. They never contain the key, generated images, prompts, provider reply text, exception messages, base URL or model identifier. HTTP error bodies are discarded. An output file is replaced atomically.

- `passed=true` requires all forty labels to match, no global timeout, and `peak_inflight` to reach the requested concurrency. Merely configuring 8 while exercising fewer simultaneous requests cannot pass the 8 smoke.
- `request_count` is the number of requests actually started; `image_count` is the number of images sent (one per request).
- `completed_count` includes started requests that reached a terminal response/error, excluding global-deadline cancellation. This does not imply successful label matching.
- `matched_count` records fully matched JSON labels. `failure_count` is forty minus matches, including deadline-canceled and unsent cards.
- `not_started_count` identifies unsent cards when the deadline is reached.
- `errors` counts safe classes such as `http_429`, `request_timeout`, `missing_output`, `invalid_json`, `invalid_labels`, `label_mismatch`, `deadline`, and `deadline_not_started`.
- `concurrency_exercised` distinguishes a matched forty-card result that did not actually reach the target parallelism.
- `latency_ms` and `elapsed_ms` are synthetic endpoint smoke timings only. They must not be described as camera-performance measurements.

Exit status: **0** = passed; **1** = completed acceptance run that failed; **2** = invalid arguments or runner/output failure; **130** = operator interruption. Runner failures expose only a safe error class. An argument/runner failure is not a provider benchmark result.

## Offline verification

No real endpoint or credential is used during development. `httpx.MockTransport` decodes the actual outgoing PNG image to determine the response label and exercises asynchronous requests at 1, 4 and 8. Additional cases cover forty-request/no-retry behavior, strict JSON/provider parsing, request errors, deadline cancellation, input validation, deterministic cards, stdin-only credentials, and report/stdout redaction. A synchronous transport regression proves that unexercised configured concurrency cannot pass.

```sh
cd /Users/nicholasliao/clawd/xiaomi-miloco/.worktrees/model-concurrency
/Users/nicholasliao/clawd/xiaomi-miloco/backend/.venv/bin/python -m pytest \
  scripts/tests/test_accept_omni_concurrency.py -q --tb=short
/Users/nicholasliao/clawd/xiaomi-miloco/backend/.venv/bin/ruff check \
  --config backend/pyproject.toml \
  scripts/accept-omni-concurrency.py scripts/tests/test_accept_omni_concurrency.py
```

Development evidence: initial ten tests failed before the runner existed; the concurrency-exercised regression separately failed before that acceptance condition was added. Final offline suite: **17 passed**. Ruff and whitespace checks passed. No real-provider run or production acceptance has been performed by this implementation worker.

## Corrected production invocation after the 2026-09-06 invalid-route run

The first production orchestration omitted the persisted `/v1` base path. Both8/4 runs received immediate404 and are invalid for capacity assessment. A401/403/404 now stops remaining requests, sets `configuration_error=true` and `capacity_assessed=false`, and returns exit2. It must not automatically trigger a lower-concurrency trial. Restore any paused service before pausing for operator correction.

Production must use a non-secret metadata JSON captured from the saved runtime, containing exactly `base_url`, `model`, `api_protocol`. Preserve the entire base URL path. For this deployment the verified base is `http://ai.esxi:18090/v1` and the actual route is `/v1/responses`.

```sh
python scripts/accept-omni-concurrency.py \
  --runtime-metadata docs/evidence/2026-09-06-model-concurrency/runtime-metadata.json \
  --concurrency 8 --output synthetic-concurrency-8.json
```

Credential stdin still comes from the approved Vault pipe. If explicit `--base-url`/`--model` are also supplied they must match metadata exactly (apart from a terminal slash), otherwise the runner rejects before reading the key or sending any request. Refresh metadata under the newCO immediately before a new production trial. The original80-request budget is exhausted; no corrected production repeat has been performed without renewed user authorization.
