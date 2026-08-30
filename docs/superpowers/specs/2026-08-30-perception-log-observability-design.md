# Perception Runtime Transparency and Realtime Empty Semantics Design

## Background

Production read-only diagnosis on `miloco.esxi` showed a confusing but important split:

- The Miloco backend is healthy enough to serve `/health`.
- The perception engine is running, ready, and has active RTSP camera sources.
- `today_inference_count` is increasing, so realtime cycles are being executed.
- User-visible perception feeds remain empty because `/api/events` only shows meaningful events and `/api/perception/logs` only shows inserted raw perception rows.
- The realtime path has repeatedly produced semantically empty results: empty `caption`, empty `matched_rules`, empty `suggestions`, empty `speeches`, `skipped=false`, and no cycle-level exception.
- `PerceptionLogRepo.append()` performs adjacent deduplication, so repeated `{}` descriptions increment inference count but usually do not insert more rows.
- On-demand Responses calls can produce non-empty text from the same provider class, proving the endpoint can work at least for active query traffic.

The user-facing problem is therefore not simply "service down". It is a visibility and semantic-output problem:

1. The dashboard has no way to explain "engine is working, but no semantic output has been produced recently".
2. The realtime path needs a safe diagnostic surface to separate camera/gate/model/prompt/parser/dedup causes.
3. Once the cause is isolated, the realtime prompt/runtime policy should be tightened so normal visible frames produce a non-empty `caption` without relaxing event/action strictness.

## Pre-Development One-Frame Verification

Before implementation started, a read-only production verification was run under `CHG260830029` on `miloco.esxi` using the configured living-room RTSP source and current Omni profile:

- Living-room RTSP source was enabled, connected, and part of active perception.
- One real living-room frame was decoded successfully with shape `[2160, 3840, 3]`.
- The current Omni profile used `openai_responses`, model `grok-chat-auto`, and the configured `http://ai.esxi:18090/v1` base URL.
- A non-fused, single-frame visual Responses request included one image block and returned HTTP 200 in 14.366s with input/output token usage and response text.
- Miloco's structured parser still marked that response as skipped because the returned text was not valid structured perception JSON.
- A production fused-shape single-frame request included one image block and five text blocks, then timed out at the configured 30s timeout.

This refutes the "no LLM participates at all" hypothesis. The current evidence points to a prompt/response-contract and latency problem in the realtime/fused path:

1. The endpoint can receive a visual request and produce text.
2. The simpler prompt can receive non-JSON text that Miloco cannot parse into semantic fields.
3. The production fused-shape prompt can exceed the current timeout before a usable response is returned.

The implementation should therefore prioritize visibility for `provider_http_ok_but_parse_skipped` and `fused_shape_timeout` separately from pure endpoint reachability. The realtime repair target is now explicit:

- Realtime/fused Omni model request timeout must be raised to `120.0` seconds end-to-end.
- OpenAI Responses visual/image-sequence requests must require one parseable structured JSON object matching Miloco's active route schema.
- Plain-language prose outside JSON, Markdown fenced JSON, or other unstructured output must not be treated as a successful perception result.
- If a provider supports native Responses structured-output request fields, Miloco may use them through adapter-gated fixture-tested support; otherwise the prompt-level JSON-only contract plus parser diagnostics is the required minimum.

## Goals

### Phase 1 — Runtime Transparency

Make the dashboard and API truthfully show the pipeline state:

- Engine lifecycle: stopped, starting, ready, failed, or temporarily unavailable.
- Active source count and source names already feeding perception.
- Realtime cycle activity in recent windows.
- Omni call health in recent windows.
- Raw perception log inserts and adjacent dedup state.
- Meaningful event count separately from raw inference count.
- Semantic output state, especially "cycles are running but outputs are empty".

The UI should make it clear that:

- `在看家` means the service is actively watching sources.
- No activity feed entries does not necessarily mean the service is idle.
- Empty semantic output is a degraded/attention state, not the same as `待机中`.

### Phase 2 — Realtime Empty Semantics Diagnosis and Repair

Add bounded, credential-safe diagnostics for realtime Omni calls and then repair the most likely semantic-empty path:

- Capture structural request summaries, not raw prompts, raw model responses, images, audio, RTSP URLs, API keys, Xiaomi tokens, or session cookies.
- Capture field counts after parse: caption count, matched rule count, suggestion count, complete speech count, incomplete speech count, skipped flag, parser fallback flag, and error code.
- Compare realtime output structure with on-demand/probe behavior.
- Distinguish HTTP success with non-JSON text from provider timeout in production fused-shape requests.
- Increase effective realtime/fused Omni model timeout to 120 seconds so slower local visual models can complete before being classified as timeout.
- Tighten the visual caption contract so a normal visible video/image window should return a non-empty `caption`.
- Require structured JSON output for visual Responses calls; natural-language text remains a degraded parse/contract failure.
- Keep strict filtering for rules, suggestions, actions, and voice responses.

## Non-Goals

- Do not store raw camera frames, videos, audio clips, RTSP URLs, API keys, Xiaomi tokens, model raw responses, or full prompt text in the new diagnostic surface.
- Do not change RTSP credential storage.
- Do not turn every caption into a meaningful event.
- Do not automatically create rules, tasks, or device actions.
- Do not relax action dispatch safety.
- Do not remove adjacent deduplication.
- Do not add automatic retention deletion.
- Do not change deployment host topology in this development plan.
- Do not perform production deployment without a separate approved Software CO/PAM window.

## Current Code Boundary

Relevant existing backend surfaces:

- `backend/miloco/src/miloco/perception/router.py`
  - `/api/perception/engine/status`
  - `/api/perception/logs`
  - `/api/perception/on-demand-logs`
- `backend/miloco/src/miloco/perception/service.py`
  - `engine_status()`
  - `query_logs()`
- `backend/miloco/src/miloco/perception/runner.py`
  - constructs `PerceptionEngineStatus`
- `backend/miloco/src/miloco/database/perception_repo.py`
  - increments daily inference count on every append attempt
  - inserts only when `descriptions` differs from the previous entry
- `backend/miloco/src/miloco/perception/processor.py`
  - builds `PerceptionLogEntry`
  - calls `PerceptionLogRepo.append(entry)`
  - currently ignores whether the row was inserted or deduplicated
- `backend/miloco/src/miloco/perception/client.py`
  - persists meaningful events only when `event_classifier.classify()` marks a result meaningful
- `backend/miloco/src/miloco/perception/event_classifier.py`
  - meaningful when there is at least one matched rule, suggestion, or complete speech needing response
- `backend/miloco/src/miloco/observability/metrics_db.py`
  - stores cycle traces, device traces, observability events, agent runs, and action ledger
- `backend/miloco/src/miloco/observability/router.py`
  - exposes `/api/traces`, `/api/events`, `/api/actions`, and trace inspection APIs

Relevant existing frontend surfaces:

- `web/src/App.tsx`
  - loads home status, scope cameras, RTSP camera summaries, and activity feed
  - renders `StatusRibbon`, `HeroNow`, and `ActivityFeed`
- `web/src/api/real.ts`
  - defines `PerceptionEngineStatus`
  - wraps `/api/perception/engine/status`
  - maps `/api/events` into the activity feed
- `web/src/api/index.ts`
  - exports API wrappers used by components
- `web/src/components/StatusRibbon.tsx`
  - shows `在看家`, `待机中`, and readiness/error states
- `web/src/components/ActivityFeed.tsx`
  - merges meaningful events and action ledger rows

## API Design

Add an authenticated endpoint:

```http
GET /api/perception/runtime-summary
```

It returns the standard backend envelope:

```json
{
  "code": 0,
  "message": "ok",
  "data": {
    "now_ms": 1798612920000,
    "engine": {
      "running": true,
      "ready": true,
      "status": "ready",
      "message": ""
    },
    "sources": {
      "active_count": 2,
      "active_sources": [
        {
          "did": "rtsp:xxx",
          "name": "厨房摄像头",
          "device_type": "camera",
          "modalities": ["video", "audio"]
        }
      ]
    },
    "logs": {
      "today_inference_count": 174,
      "raw_total": 6,
      "raw_last_hour": 0,
      "last_inference_ms": 1798612900000,
      "last_insert_ms": 1798607040000,
      "last_descriptions_empty": true,
      "last_append_inserted": false,
      "consecutive_empty_descriptions": 185,
      "consecutive_deduplicated": 184,
      "meaningful_total": 0,
      "meaningful_last_hour": 0,
      "last_meaningful_event_ms": null
    },
    "windows": [
      {
        "minutes": 5,
        "cycle_count": 15,
        "skipped_count": 0,
        "video_pass_count": 7,
        "audio_pass_count": 0,
        "hold_pass_count": 5,
        "omni_call_count": 14,
        "omni_error_count": 1,
        "cycle_error_count": 0,
        "dropped_windows_count": 92,
        "overflow_count": 8
      },
      {
        "minutes": 15,
        "cycle_count": 45,
        "skipped_count": 0,
        "video_pass_count": 23,
        "audio_pass_count": 0,
        "hold_pass_count": 18,
        "omni_call_count": 43,
        "omni_error_count": 3,
        "cycle_error_count": 0,
        "dropped_windows_count": 820,
        "overflow_count": 80
      },
      {
        "minutes": 60,
        "cycle_count": 200,
        "skipped_count": 56,
        "video_pass_count": 90,
        "audio_pass_count": 0,
        "hold_pass_count": 68,
        "omni_call_count": 144,
        "omni_error_count": 21,
        "cycle_error_count": 0,
        "dropped_windows_count": 3227,
        "overflow_count": 341
      }
    ],
    "latest_omni": {
      "timestamp_ms": 1798612900000,
      "protocol": "openai_responses",
      "request": {
        "message_count": 2,
        "text_block_count": 2,
        "image_block_count": 1,
        "video_block_count": 0,
        "audio_block_count": 0
      },
      "response": {
        "text_length": 228,
        "parse_ok": true,
        "skipped": false,
        "caption_count": 0,
        "matched_rule_count": 0,
        "suggestion_count": 0,
        "speech_count": 0,
        "complete_speech_count": 0,
        "needs_response_speech_count": 0
      },
      "error_code": null
    },
    "semantic_state": "silent",
    "hints": [
      "engine_active",
      "recent_omni_calls",
      "semantic_output_empty",
      "raw_logs_deduplicated"
    ]
  }
}
```

### `semantic_state` Values

Use a small enum rather than a free-form message:

- `inactive`: engine not running.
- `not_ready`: engine running but not ready.
- `no_sources`: ready but no active perception sources.
- `collecting`: sources are active, but recent cycles have not produced enough evidence yet.
- `eventing`: meaningful events have appeared recently.
- `describing`: raw perception descriptions are being inserted recently, but no meaningful events appeared.
- `silent`: cycles and Omni calls are happening, but semantic result fields are repeatedly empty or deduplicated.
- `degraded`: cycle or Omni error rate is high enough that semantic output cannot be trusted.

Suggested thresholds:

- Recent window is the 15-minute window for state classification.
- `degraded` when `omni_error_count / max(omni_call_count, 1) >= 0.25` or `cycle_error_count > 0`.
- `eventing` when `meaningful_last_hour > 0`.
- `describing` when `raw_last_hour > 0`.
- `silent` when `cycle_count > 0`, `omni_call_count > 0`, `consecutive_empty_descriptions >= 3`, and no raw/meaningful inserts appeared in the recent window.
- `collecting` otherwise when the engine is ready and sources are active.

These thresholds are deliberately conservative. They should explain the current production symptom without turning every quiet minute into an alarm.

## Backend Runtime State

Extend `PerceptionLogRepo` with an in-memory runtime stats object. This is intentionally not a migration:

```py
@dataclass
class PerceptionLogRuntimeStats:
    today_inference_count: int = 0
    today_insert_count: int = 0
    last_inference_ms: int | None = None
    last_insert_ms: int | None = None
    last_descriptions_empty: bool | None = None
    last_append_inserted: bool | None = None
    consecutive_empty_descriptions: int = 0
    consecutive_deduplicated: int = 0
```

`append(entry)` keeps returning `bool`:

- Increment inference count and update `last_inference_ms` before dedup.
- Compute `descriptions_empty = not any(v.strip() for v in entry.descriptions.values() if isinstance(v, str))`.
- Increment or reset `consecutive_empty_descriptions`.
- If adjacent dedup triggers, set `last_append_inserted=false`, increment `consecutive_deduplicated`, and return `False`.
- On successful insert, set `last_insert_ms`, increment `today_insert_count`, reset `consecutive_deduplicated`, and return `True`.
- On insert exception, set `last_append_inserted=false` but do not pretend it was deduplicated.

Add read helpers:

```py
def runtime_stats(self) -> PerceptionLogRuntimeStats: ...
def count_since(self, since_ms: int) -> int: ...
def latest_timestamp_ms(self) -> int | None: ...
```

The in-memory stats reset on process restart, while SQLite-backed `count_since()` and `latest_timestamp_ms()` preserve persistent evidence.

## Realtime Diagnostic Ring

Add a small process-local diagnostic ring under perception, for example:

```py
backend/miloco/src/miloco/perception/runtime_diagnostics.py
```

The ring stores at most 64 sanitized records:

```py
@dataclass(frozen=True)
class RealtimeOmniDiagnostic:
    timestamp_ms: int
    protocol: str | None
    route: Literal["realtime", "on_demand", "probe", "unknown"]
    message_count: int
    text_block_count: int
    image_block_count: int
    video_block_count: int
    audio_block_count: int
    response_text_length: int
    response_json_like: bool
    parse_ok: bool
    skipped: bool
    caption_count: int
    matched_rule_count: int
    suggestion_count: int
    speech_count: int
    complete_speech_count: int
    needs_response_speech_count: int
    error_code: str | None
```

No field may contain:

- Prompt text.
- Response text.
- Base64 image or audio data.
- RTSP URL.
- API key.
- Xiaomi account data.
- Cookie or token values.

The diagnostic ring answers:

- Did the realtime request include visual input blocks?
- Did the model return any response text?
- Did the response text look like a JSON object/array before parser fallback?
- Did parser succeed?
- Did parsed semantic fields all end up empty?

That lets the next debugging step distinguish:

- model/provider returned no text;
- parser rejected the text;
- prompt/schema induced empty valid JSON;
- visual payload was not actually present;
- downstream dedup hid repeated descriptions.

## Frontend UX Design

Add a compact "感知运行状态" card near the current dashboard status area.

Recommended placement:

- Home overview tab.
- Under `StatusRibbon` and before the main realtime camera panel.
- Keep it visually quiet unless the state is `silent` or `degraded`.

The card should show plain-language rows:

- 服务状态: `运行中 / 未就绪 / 已停止`
- 摄像头: `2 个在感知`
- 推理: `15 分钟内 43 次模型调用`
- 日志: `今日 174 次推理，最近 1 小时 0 条新描述`
- 事件: `最近 1 小时 0 条有意义事件`
- 当前判断:
  - `正在看家，暂未发现需要记录的事件`
  - `正在看家，但模型返回的实时语义为空`
  - `模型调用错误率偏高`

The activity feed should continue to represent meaningful events/actions only. Do not overload it with raw cycle noise. The new runtime card explains why the activity feed can be empty while the engine remains active.

### Polling

Fetch `/api/perception/runtime-summary`:

- on overview page mount;
- after engine restart/stop/start actions;
- after RTSP enable/disable;
- every 30 seconds while the overview page is visible.

Avoid sub-5-second polling. The engine interval is several seconds and the state is explanatory, not a live video meter.

## Realtime Prompt/Policy Repair

After Phase 1 exposes the empty semantic state, Phase 2 tightens realtime output policy:

- `caption` remains a visual-only field.
- For a normal visible video/image window, `caption` should be non-empty and describe current visible state.
- If the model sees no usable visual content, it may return an empty caption or an explicit "画面不可判断" style caption depending on current parser constraints.
- `matched_rules`, `suggestions`, `speeches`, and actions remain strict and must not be fabricated.
- Parser fallback captions that contain raw parse-failure snippets must still be suppressed and must not update `_last_captions`.

The smallest safe implementation point is `backend/miloco/src/miloco/perception/engine/omni/field_registry.py`, because the caption schema and field instruction are centrally rendered from `CAPTION`.

Add tests in the existing prompt-builder suite to ensure:

- video route keeps `caption`;
- audio-only route does not include `caption`;
- caption spec says a usable visible window should return a non-empty description;
- structured-output instructions remain explicit enough for Responses-style image-sequence requests;
- strict `matched_rules` and `suggestions` instructions remain unchanged.

## Testing Strategy

Backend:

- Unit tests for `PerceptionLogRepo` runtime stats and adjacent dedup counters.
- Unit tests for runtime diagnostic sanitization and field counts.
- Router/service tests for `/api/perception/runtime-summary`.
- Prompt-builder tests for caption contract.
- Existing Omni provider/probe tests must continue to pass.

Frontend:

- Type/API wrapper tests for runtime summary parsing.
- Pure helper tests for semantic-state presentation.
- Component-level tests only if the existing test environment supports them without adding heavy dependencies; otherwise keep view logic in a pure helper and smoke-check build.

Suggested local verification:

```bash
cd backend/miloco
uv run pytest \
  tests/test_perception_repo_sqlite.py \
  tests/test_perception_runtime_summary.py \
  tests/perception/engine/omni/test_prompt_builder.py
```

```bash
cd web
pnpm test \
  tests/perception-runtime.test.ts \
  tests/perception-activity.test.ts
pnpm build
```

## Production Acceptance

Production deployment is not part of this document. When implementation is complete and the user approves production rollout:

1. Create a Software CO for `miloco.esxi`.
2. Deploy the exact implementation SHA through the repo deployment path.
3. Verify `/health`.
4. Verify `/api/perception/engine/status`.
5. Verify `/api/perception/runtime-summary` with sanitized evidence only.
6. Confirm dashboard shows a truthful runtime card.
7. If `semantic_state=silent`, confirm the card explains that realtime model output is empty rather than implying the service is idle.
8. If the prompt repair succeeds, confirm recent runtime summaries show non-empty captions or raw log inserts when cameras have usable visible frames.

Do not print or store secrets in CO notes, shell logs, memory files, screenshots, or final summaries.
