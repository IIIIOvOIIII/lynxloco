# Perception Runtime Transparency and Realtime Empty Semantics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the approved Phase 1 and Phase 2 Miloco perception follow-up in a dedicated worktree: expose the true realtime perception state, explain why no perception logs/events appear while the service is running, and repair the realtime path so usable RTSP frames produce non-empty visual semantics.

**Architecture:** Add a backend runtime-summary API that combines engine status, raw perception-log stats, meaningful-event stats, observability trace windows, and sanitized realtime Omni diagnostics. Add a compact dashboard card and frontend helper that presents this truth plainly. Then tighten the realtime caption prompt contract without relaxing rules/actions/suggestions. Keep all diagnostic content credential-safe and metadata-only.

**Tech Stack:** Python 3.11, FastAPI, Pydantic, SQLite, pytest, React 19, TypeScript, Vite, Vitest, existing Miloco deployment scripts.

**Spec:** `docs/superpowers/specs/2026-08-30-perception-log-observability-design.md`

## Global Constraints

- Work only inside the dedicated worktree `/Users/nicholasliao/clawd/xiaomi-miloco/.worktrees/perception-log-observability` on branch `feature/perception-log-observability`.
- Do not modify or rely on the dirty sibling worktree `/Users/nicholasliao/clawd/xiaomi-miloco/.worktrees/rtsp-responses-support`.
- Do not store raw camera frames, videos, audio clips, RTSP URLs, API keys, Xiaomi tokens, model raw responses, full prompt text, cookies, or browser session values.
- Do not change RTSP credential storage.
- Do not turn pure captions into meaningful events.
- Do not relax action dispatch, rule matching, suggestions, or voice-response safety.
- Do not add automatic retention deletion.
- Do not remove adjacent deduplication.
- Use TDD for implementation tasks: write a failing test first, then implement the smallest production change.
- Preserve existing public API behavior. New API surface must be additive.
- Production deployment to `miloco.esxi` requires a separate approved Software CO/PAM window after local implementation is complete.

---

## Phase 1: Runtime Transparency

### Task 1: Track Perception Log Append Runtime Stats

**Purpose:** Show the difference between "an inference cycle happened", "a raw perception row was inserted", and "a raw row was skipped by adjacent dedup".

**Files:**

- Modify: `backend/miloco/src/miloco/database/perception_repo.py`
- Modify: `backend/miloco/tests/test_perception_repo_sqlite.py`

**Interfaces:**

Add:

```py
from dataclasses import asdict, dataclass

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

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
```

Add repo methods:

```py
def runtime_stats(self) -> PerceptionLogRuntimeStats: ...
def count_since(self, since_ms: int) -> int: ...
def latest_timestamp_ms(self) -> int | None: ...
```

Keep `append(entry) -> bool` unchanged for callers.

- [ ] Step 1: Add failing tests for runtime append stats.

Add test cases to `backend/miloco/tests/test_perception_repo_sqlite.py`:

```py
def test_append_stats_track_inference_insert_empty_and_dedup(repo):
    first = PerceptionLogEntry(id="p1", timestamp=1000, descriptions={})
    second = PerceptionLogEntry(id="p2", timestamp=2000, descriptions={})

    assert repo.append(first) is True
    assert repo.append(second) is False

    stats = repo.runtime_stats()
    assert stats.today_inference_count == 2
    assert stats.today_insert_count == 1
    assert stats.last_inference_ms == 2000
    assert stats.last_insert_ms == 1000
    assert stats.last_descriptions_empty is True
    assert stats.last_append_inserted is False
    assert stats.consecutive_empty_descriptions == 2
    assert stats.consecutive_deduplicated == 1
```

```py
def test_append_stats_reset_empty_and_dedup_streaks_on_new_description(repo):
    assert repo.append(
        PerceptionLogEntry(id="p1", timestamp=1000, descriptions={})
    ) is True
    assert repo.append(
        PerceptionLogEntry(id="p2", timestamp=2000, descriptions={})
    ) is False
    assert repo.append(
        PerceptionLogEntry(id="p3", timestamp=3000, descriptions={"厨房": "有人经过"})
    ) is True

    stats = repo.runtime_stats()
    assert stats.today_inference_count == 3
    assert stats.today_insert_count == 2
    assert stats.last_inference_ms == 3000
    assert stats.last_insert_ms == 3000
    assert stats.last_descriptions_empty is False
    assert stats.last_append_inserted is True
    assert stats.consecutive_empty_descriptions == 0
    assert stats.consecutive_deduplicated == 0
```

```py
def test_count_since_and_latest_timestamp(repo):
    repo.append(PerceptionLogEntry(id="p1", timestamp=1000, descriptions={"客厅": "空"}))
    repo.append(PerceptionLogEntry(id="p2", timestamp=2000, descriptions={"客厅": "有人"}))

    assert repo.count_since(1500) == 1
    assert repo.latest_timestamp_ms() == 2000
```

- [ ] Step 2: Run the targeted test and confirm it fails.

```bash
cd backend/miloco
uv run pytest tests/test_perception_repo_sqlite.py -q
```

Expected initial failure: missing `runtime_stats`, `count_since`, or `latest_timestamp_ms`.

- [ ] Step 3: Implement the repo stats.

Implementation notes:

```py
def _descriptions_empty(descriptions: dict[str, object]) -> bool:
    for value in descriptions.values():
        if isinstance(value, str) and value.strip():
            return False
        if not isinstance(value, str) and value:
            return False
    return True
```

In `append()`:

```py
self._check_date_boundary()
self._today_inference_count += 1
self._runtime_stats.today_inference_count = self._today_inference_count
self._runtime_stats.last_inference_ms = entry.timestamp
empty = _descriptions_empty(entry.descriptions)
self._runtime_stats.last_descriptions_empty = empty
self._runtime_stats.consecutive_empty_descriptions = (
    self._runtime_stats.consecutive_empty_descriptions + 1 if empty else 0
)
```

On adjacent dedup:

```py
self._runtime_stats.last_append_inserted = False
self._runtime_stats.consecutive_deduplicated += 1
return False
```

On successful insert:

```py
self._runtime_stats.last_insert_ms = entry.timestamp
self._runtime_stats.last_append_inserted = True
self._runtime_stats.today_insert_count += 1
self._runtime_stats.consecutive_deduplicated = 0
```

On insert failure:

```py
self._runtime_stats.last_append_inserted = False
```

Do not increment `consecutive_deduplicated` on insert failure; failure is not dedup.

- [ ] Step 4: Verify.

```bash
cd backend/miloco
uv run pytest tests/test_perception_repo_sqlite.py -q
```

Expected: pass.

---

### Task 2: Add Meaningful Event and Observability Window Summaries

**Purpose:** Give the backend one reusable way to answer "what happened in the last 5/15/60 minutes?" without forcing the frontend to query raw observability tables.

**Files:**

- Modify: `backend/miloco/src/miloco/database/meaningful_events_dao.py`
- Modify: `backend/miloco/src/miloco/perception/service.py`
- Create: `backend/miloco/tests/test_perception_runtime_summary.py`

**Interfaces:**

Add DAO helpers:

```py
def count_since(self, since_ms: int) -> int: ...
def count_all(self) -> int: ...
def latest_timestamp_ms(self) -> int | None: ...
```

Add service helper:

```py
def _query_observability_windows(
    self,
    *,
    obs_db_path: Path | str | None,
    now_ms: int,
    windows_minutes: tuple[int, ...] = (5, 15, 60),
) -> list[dict[str, int]]:
    ...
```

The window SQL should aggregate from `traces`:

```sql
SELECT
  COUNT(*) AS cycle_count,
  SUM(CASE WHEN skipped = 1 THEN 1 ELSE 0 END) AS skipped_count,
  SUM(CASE WHEN gate_video_pass = 1 THEN 1 ELSE 0 END) AS video_pass_count,
  SUM(CASE WHEN gate_audio_pass = 1 THEN 1 ELSE 0 END) AS audio_pass_count,
  SUM(CASE WHEN gate_hold_pass = 1 THEN 1 ELSE 0 END) AS hold_pass_count,
  COALESCE(SUM(omni_call_count), 0) AS omni_call_count,
  COALESCE(SUM(omni_error_count), 0) AS omni_error_count,
  SUM(CASE WHEN cycle_error_msg IS NOT NULL AND cycle_error_msg != '' THEN 1 ELSE 0 END) AS cycle_error_count,
  COALESCE(MAX(dropped_windows_total), 0) AS dropped_windows_count,
  COALESCE(MAX(overflow_count_total), 0) AS overflow_count
FROM traces
WHERE timestamp >= ?
```

Use `connect(obs_db_path)` from `miloco.observability.metrics_db`. If `obs_db_path` is `None` or unavailable, return zeroed windows rather than failing the main dashboard.

- [ ] Step 1: Write failing backend summary tests.

Create `backend/miloco/tests/test_perception_runtime_summary.py` with isolated helper tests:

```py
def test_runtime_summary_classifies_silent_when_cycles_run_but_semantics_empty(fake_service):
    fake_service.engine_status.return_value = PerceptionEngineStatus(
        running=True,
        engine=EngineState(ready=True, status="ready", message=""),
        today_inference_count=10,
        active_sources=[{"did": "rtsp:1", "name": "厨房", "device_type": "camera"}],
    )
    fake_service.log_stats = {
        "today_inference_count": 10,
        "raw_last_hour": 0,
        "consecutive_empty_descriptions": 8,
        "consecutive_deduplicated": 7,
    }
    fake_service.windows = [
        {"minutes": 15, "cycle_count": 20, "omni_call_count": 15, "omni_error_count": 0, "cycle_error_count": 0}
    ]

    summary = build_runtime_summary_for_test(fake_service)
    assert summary.semantic_state == "silent"
    assert "semantic_output_empty" in summary.hints
```

If the repo does not have a convenient fake service pattern, implement the classification function as a pure helper:

```py
def classify_perception_runtime_state(
    *,
    running: bool,
    ready: bool,
    active_source_count: int,
    raw_last_hour: int,
    meaningful_last_hour: int,
    consecutive_empty_descriptions: int,
    recent_window: Mapping[str, int],
) -> tuple[str, list[str]]:
    ...
```

Then test that helper directly.

- [ ] Step 2: Implement classification helper.

Recommended states:

```py
if not running:
    return "inactive", ["engine_stopped"]
if not ready:
    return "not_ready", ["engine_not_ready"]
if active_source_count <= 0:
    return "no_sources", ["no_active_sources"]
error_rate = omni_error_count / max(omni_call_count, 1)
if cycle_error_count > 0 or error_rate >= 0.25:
    return "degraded", ["high_error_rate"]
if meaningful_last_hour > 0:
    return "eventing", ["meaningful_events_recent"]
if raw_last_hour > 0:
    return "describing", ["raw_descriptions_recent"]
if cycle_count > 0 and omni_call_count > 0 and consecutive_empty_descriptions >= 3:
    return "silent", ["engine_active", "recent_omni_calls", "semantic_output_empty", "raw_logs_deduplicated"]
return "collecting", ["engine_active"]
```

- [ ] Step 3: Implement meaningful-event DAO helpers and observability window helper.

Keep helper failures non-fatal. The runtime summary is an explanatory UI support endpoint; a broken observability DB should return `semantic_state=collecting` or `degraded` with an explanatory hint, not crash the dashboard.

- [ ] Step 4: Verify.

```bash
cd backend/miloco
uv run pytest tests/test_perception_runtime_summary.py -q
```

Expected: pass.

---

### Task 3: Add Sanitized Realtime Omni Diagnostic Ring

**Purpose:** Preserve enough structure to debug empty realtime outputs without preserving sensitive content.

**Files:**

- Create: `backend/miloco/src/miloco/perception/runtime_diagnostics.py`
- Modify: `backend/miloco/src/miloco/perception/engine/omni/omni_client.py`
- Modify: `backend/miloco/src/miloco/perception/engine/api.py`
- Create: `backend/miloco/tests/test_perception_runtime_diagnostics.py`

**Interfaces:**

Create:

```py
from collections import deque
from dataclasses import asdict, dataclass
from typing import Literal

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
    parse_ok: bool
    skipped: bool
    caption_count: int
    matched_rule_count: int
    suggestion_count: int
    speech_count: int
    complete_speech_count: int
    needs_response_speech_count: int
    error_code: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

class RuntimeDiagnostics:
    def __init__(self, maxlen: int = 64): ...
    def record(self, sample: RealtimeOmniDiagnostic) -> None: ...
    def latest(self, route: str | None = None) -> RealtimeOmniDiagnostic | None: ...
    def snapshot(self) -> list[RealtimeOmniDiagnostic]: ...
    def reset_for_tests(self) -> None: ...
```

Expose singleton:

```py
def get_runtime_diagnostics() -> RuntimeDiagnostics: ...
```

Add safe summary helpers:

```py
def summarize_omni_messages(messages: object) -> dict[str, int]: ...
def summarize_realtime_result(result: RealtimePerceptionResult | None) -> dict[str, int | bool]: ...
```

- [ ] Step 1: Write failing diagnostic tests.

Create `backend/miloco/tests/test_perception_runtime_diagnostics.py`:

```py
def test_summarize_omni_messages_counts_blocks_without_content():
    messages = [
        {"role": "system", "content": "secret system prompt"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "secret user prompt"},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}},
            ],
        },
    ]

    summary = summarize_omni_messages(messages)

    assert summary == {
        "message_count": 2,
        "text_block_count": 2,
        "image_block_count": 1,
        "video_block_count": 0,
        "audio_block_count": 0,
    }
    assert "secret" not in repr(summary)
    assert "AAAA" not in repr(summary)
```

```py
def test_runtime_diagnostics_is_bounded_and_returns_latest():
    diag = RuntimeDiagnostics(maxlen=2)
    diag.record(RealtimeOmniDiagnostic(timestamp_ms=1, protocol="openai_responses", route="realtime", message_count=1, text_block_count=1, image_block_count=0, video_block_count=0, audio_block_count=0, response_text_length=0, parse_ok=True, skipped=False, caption_count=0, matched_rule_count=0, suggestion_count=0, speech_count=0, complete_speech_count=0, needs_response_speech_count=0))
    diag.record(RealtimeOmniDiagnostic(timestamp_ms=2, protocol="openai_responses", route="on_demand", message_count=1, text_block_count=1, image_block_count=1, video_block_count=0, audio_block_count=0, response_text_length=42, parse_ok=True, skipped=False, caption_count=1, matched_rule_count=0, suggestion_count=0, speech_count=0, complete_speech_count=0, needs_response_speech_count=0))
    diag.record(RealtimeOmniDiagnostic(timestamp_ms=3, protocol="openai_responses", route="realtime", message_count=1, text_block_count=1, image_block_count=1, video_block_count=0, audio_block_count=0, response_text_length=20, parse_ok=True, skipped=False, caption_count=0, matched_rule_count=0, suggestion_count=0, speech_count=0, complete_speech_count=0, needs_response_speech_count=0))

    assert [s.timestamp_ms for s in diag.snapshot()] == [2, 3]
    assert diag.latest("realtime").timestamp_ms == 3
```

- [ ] Step 2: Implement the module.

The implementation must never save values from `text`, `content`, `url`, `image`, `audio`, `authorization`, or headers. It only counts shape.

- [ ] Step 3: Wire diagnostics into realtime path.

Use two lightweight integration points:

1. In `omni_client.py`, when calling `push_omni_trace(...)`, also produce request-shape and response-text-length metadata for the diagnostic helper. If the route cannot be detected there, mark `route="unknown"`.
2. In `engine/api.py`, after `_merge_results(...)`, record a `route="realtime"` semantic sample with field counts from the merged `RealtimePerceptionResult`.

The second record can update the latest unknown sample or simply add a realtime semantic sample. Prefer the simpler additive record first; the runtime summary can display the latest realtime semantic sample.

- [ ] Step 4: Verify.

```bash
cd backend/miloco
uv run pytest tests/test_perception_runtime_diagnostics.py -q
```

Expected: pass.

---

### Task 4: Expose `/api/perception/runtime-summary`

**Purpose:** Provide a single authenticated endpoint for the dashboard and for production acceptance checks.

**Files:**

- Modify: `backend/miloco/src/miloco/perception/schema.py`
- Modify: `backend/miloco/src/miloco/perception/service.py`
- Modify: `backend/miloco/src/miloco/perception/router.py`
- Modify: `backend/miloco/tests/test_perception_runtime_summary.py`

**Pydantic Models:**

Add to `schema.py`:

```py
class RuntimeEngineSummary(BaseModel):
    running: bool = False
    ready: bool = False
    status: str = "not_initialized"
    message: str = ""

class RuntimeSourceSummary(BaseModel):
    active_count: int = 0
    active_sources: list[dict[str, Any]] = Field(default_factory=list)

class RuntimeLogSummary(BaseModel):
    today_inference_count: int = 0
    raw_total: int = 0
    raw_last_hour: int = 0
    last_inference_ms: int | None = None
    last_insert_ms: int | None = None
    last_descriptions_empty: bool | None = None
    last_append_inserted: bool | None = None
    consecutive_empty_descriptions: int = 0
    consecutive_deduplicated: int = 0
    meaningful_total: int = 0
    meaningful_last_hour: int = 0
    last_meaningful_event_ms: int | None = None

class RuntimeWindowSummary(BaseModel):
    minutes: int
    cycle_count: int = 0
    skipped_count: int = 0
    video_pass_count: int = 0
    audio_pass_count: int = 0
    hold_pass_count: int = 0
    omni_call_count: int = 0
    omni_error_count: int = 0
    cycle_error_count: int = 0
    dropped_windows_count: int = 0
    overflow_count: int = 0

class RuntimeOmniSummary(BaseModel):
    timestamp_ms: int | None = None
    protocol: str | None = None
    route: str | None = None
    request: dict[str, int] = Field(default_factory=dict)
    response: dict[str, int | bool] = Field(default_factory=dict)
    error_code: str | None = None

class PerceptionRuntimeSummary(BaseModel):
    now_ms: int
    engine: RuntimeEngineSummary
    sources: RuntimeSourceSummary
    logs: RuntimeLogSummary
    windows: list[RuntimeWindowSummary] = Field(default_factory=list)
    latest_omni: RuntimeOmniSummary | None = None
    semantic_state: Literal[
        "inactive",
        "not_ready",
        "no_sources",
        "collecting",
        "eventing",
        "describing",
        "silent",
        "degraded",
    ] = "collecting"
    hints: list[str] = Field(default_factory=list)
```

- [ ] Step 1: Add failing router/service tests.

Test endpoint contract with a fake app request where `request.app.state.obs_db_path` points to a temporary initialized observability DB.

Required assertions:

```py
assert payload["code"] == 0
data = payload["data"]
assert data["engine"]["running"] is True
assert data["sources"]["active_count"] == 2
assert data["logs"]["today_inference_count"] >= 1
assert {w["minutes"] for w in data["windows"]} == {5, 15, 60}
assert data["semantic_state"] in {
    "inactive", "not_ready", "no_sources", "collecting",
    "eventing", "describing", "silent", "degraded",
}
```

Add a specific silent-state assertion:

```py
assert data["semantic_state"] == "silent"
assert "semantic_output_empty" in data["hints"]
```

- [ ] Step 2: Implement `PerceptionService.runtime_summary(...)`.

Signature:

```py
def runtime_summary(
    self,
    *,
    obs_db_path: Path | str | None = None,
    now_ms_value: int | None = None,
) -> PerceptionRuntimeSummary:
    ...
```

Implementation assembly:

1. `status = self.engine_status()`
2. `log_stats = self._log_repo.runtime_stats()`
3. `raw_total = self._log_repo.count_all()`
4. `raw_last_hour = self._log_repo.count_since(now_ms - 3600_000)`
5. `last_insert_ms = log_stats.last_insert_ms or self._log_repo.latest_timestamp_ms()`
6. `meaningful_total = self._meaningful_events_dao.count_all()`
7. `meaningful_last_hour = self._meaningful_events_dao.count_since(now_ms - 3600_000)`
8. `last_meaningful_event_ms = self._meaningful_events_dao.latest_timestamp_ms()`
9. `windows = self._query_observability_windows(...)`
10. `latest_omni = get_runtime_diagnostics().latest("realtime")`
11. `semantic_state, hints = classify_perception_runtime_state(...)`

Use defensive `try/except` around optional observability and diagnostics only. Do not hide failures in core engine/log DAO calls.

- [ ] Step 3: Add router endpoint.

In `backend/miloco/src/miloco/perception/router.py`:

```py
@router.get(
    "/runtime-summary",
    summary="Summarize realtime perception runtime state",
    dependencies=[Depends(verify_token)],
)
async def runtime_summary(request: Request):
    data = manager.perception_service.runtime_summary(
        obs_db_path=getattr(request.app.state, "obs_db_path", None),
    )
    return NormalResponse(code=0, message="ok", data=data)
```

Ensure `Request` is imported.

- [ ] Step 4: Verify targeted backend tests.

```bash
cd backend/miloco
uv run pytest \
  tests/test_perception_repo_sqlite.py \
  tests/test_perception_runtime_diagnostics.py \
  tests/test_perception_runtime_summary.py \
  -q
```

Expected: pass.

---

### Task 5: Add Frontend Runtime Summary API and Presentation Helper

**Purpose:** Keep dashboard rendering simple and testable.

**Files:**

- Modify: `web/src/lib/types.ts`
- Modify: `web/src/api/real.ts`
- Modify: `web/src/api/index.ts`
- Create: `web/src/lib/perceptionRuntime.ts`
- Create: `web/tests/perception-runtime.test.ts`

**Types:**

Add to `web/src/lib/types.ts`:

```ts
export type PerceptionSemanticState =
  | "inactive"
  | "not_ready"
  | "no_sources"
  | "collecting"
  | "eventing"
  | "describing"
  | "silent"
  | "degraded";

export interface PerceptionRuntimeSummary {
  nowMs: number;
  engine: {
    running: boolean;
    ready: boolean;
    status: string;
    message?: string;
  };
  sources: {
    activeCount: number;
    activeSources: Array<{
      did: string;
      name: string;
      deviceType?: string;
      modalities?: string[];
    }>;
  };
  logs: {
    todayInferenceCount: number;
    rawTotal: number;
    rawLastHour: number;
    lastInferenceMs: number | null;
    lastInsertMs: number | null;
    lastDescriptionsEmpty: boolean | null;
    lastAppendInserted: boolean | null;
    consecutiveEmptyDescriptions: number;
    consecutiveDeduplicated: number;
    meaningfulTotal: number;
    meaningfulLastHour: number;
    lastMeaningfulEventMs: number | null;
  };
  windows: Array<{
    minutes: number;
    cycleCount: number;
    skippedCount: number;
    videoPassCount: number;
    audioPassCount: number;
    holdPassCount: number;
    omniCallCount: number;
    omniErrorCount: number;
    cycleErrorCount: number;
    droppedWindowsCount: number;
    overflowCount: number;
  }>;
  latestOmni: {
    timestampMs: number | null;
    protocol?: string | null;
    route?: string | null;
    request: Record<string, number>;
    response: Record<string, number | boolean>;
    errorCode?: string | null;
  } | null;
  semanticState: PerceptionSemanticState;
  hints: string[];
}
```

API wrapper:

```ts
export async function realGetPerceptionRuntimeSummary(): Promise<PerceptionRuntimeSummary> {
  const res = await apiFetch<Normal<BackendRuntimeSummary>>(
    "/api/perception/runtime-summary",
  );
  return mapRuntimeSummary(res.data);
}
```

Export from `web/src/api/index.ts`:

```ts
export async function getPerceptionRuntimeSummary(homeId?: string) {
  return realGetPerceptionRuntimeSummary();
}
```

`homeId` is accepted for call-site consistency but not sent until the backend has per-home runtime partitioning.

Presentation helper:

```ts
export interface PerceptionRuntimeView {
  tone: "ok" | "info" | "warn" | "danger";
  titleKey: string;
  detailKey: string;
}

export function derivePerceptionRuntimeView(
  summary: PerceptionRuntimeSummary | null | undefined,
): PerceptionRuntimeView {
  ...
}
```

Mapping:

- `inactive` → info, service stopped.
- `not_ready` → warn, service not ready.
- `no_sources` → info, no active cameras.
- `collecting` → ok, watching and collecting.
- `eventing` → ok, meaningful events recent.
- `describing` → ok, realtime descriptions recent.
- `silent` → warn, watching but realtime semantics empty.
- `degraded` → danger, error rate high.

- [ ] Step 1: Write failing frontend tests.

Create `web/tests/perception-runtime.test.ts`:

```ts
import { describe, expect, it } from "vitest";
import { derivePerceptionRuntimeView } from "@/lib/perceptionRuntime";
import type { PerceptionRuntimeSummary } from "@/lib/types";

function summary(
  semanticState: PerceptionRuntimeSummary["semanticState"],
): PerceptionRuntimeSummary {
  return {
    nowMs: 1,
    engine: { running: true, ready: true, status: "ready", message: "" },
    sources: { activeCount: 2, activeSources: [] },
    logs: {
      todayInferenceCount: 174,
      rawTotal: 6,
      rawLastHour: 0,
      lastInferenceMs: 1,
      lastInsertMs: null,
      lastDescriptionsEmpty: true,
      lastAppendInserted: false,
      consecutiveEmptyDescriptions: 8,
      consecutiveDeduplicated: 7,
      meaningfulTotal: 0,
      meaningfulLastHour: 0,
      lastMeaningfulEventMs: null,
    },
    windows: [],
    latestOmni: null,
    semanticState,
    hints: [],
  };
}

describe("derivePerceptionRuntimeView", () => {
  it("marks silent realtime semantics as warning, not standby", () => {
    const view = derivePerceptionRuntimeView(summary("silent"));
    expect(view.tone).toBe("warn");
    expect(view.titleKey).toBe("perceptionRuntime.silentTitle");
  });

  it("marks recent meaningful activity as ok", () => {
    const view = derivePerceptionRuntimeView(summary("eventing"));
    expect(view.tone).toBe("ok");
  });
});
```

- [ ] Step 2: Implement mapping and API conversion.

Follow existing backend snake_case to frontend camelCase mapping style in `web/src/api/real.ts`.

- [ ] Step 3: Verify.

```bash
cd web
pnpm test tests/perception-runtime.test.ts -q
```

Expected: pass.

---

### Task 6: Render Dashboard Runtime Card and Poll It Safely

**Purpose:** Make the production symptom visible in the UI without flooding the activity feed.

**Files:**

- Create: `web/src/components/PerceptionRuntimeCard.tsx`
- Modify: `web/src/App.tsx`
- Modify: `web/src/i18n/locales/zh/common.json`
- Modify: `web/src/i18n/locales/en/common.json`
- Modify: `web/tests/perception-runtime.test.ts`

**Component Props:**

```ts
export function PerceptionRuntimeCard(props: {
  summary: PerceptionRuntimeSummary | null | undefined;
  loading?: boolean;
  error?: Error | null;
  onReload?: () => void;
}): React.JSX.Element | null
```

**UI copy, zh:**

```json
{
  "perceptionRuntime.title": "感知运行状态",
  "perceptionRuntime.inactiveTitle": "感知服务未运行",
  "perceptionRuntime.notReadyTitle": "感知服务还没准备好",
  "perceptionRuntime.noSourcesTitle": "没有启用的感知摄像头",
  "perceptionRuntime.collectingTitle": "正在看家，等待新线索",
  "perceptionRuntime.eventingTitle": "正在看家，最近有事件",
  "perceptionRuntime.describingTitle": "正在看家，最近有画面描述",
  "perceptionRuntime.silentTitle": "正在看家，但实时语义为空",
  "perceptionRuntime.degradedTitle": "感知链路不稳定",
  "perceptionRuntime.summaryLine": "今日 {{inferences}} 次推理；近 1 小时 {{raw}} 条描述、{{events}} 条事件",
  "perceptionRuntime.modelLine": "近 {{minutes}} 分钟 {{calls}} 次模型调用，{{errors}} 次错误",
  "perceptionRuntime.sourcesLine": "{{count}} 个摄像头在感知",
  "perceptionRuntime.reload": "刷新状态"
}
```

**UI copy, en:**

```json
{
  "perceptionRuntime.title": "Perception runtime",
  "perceptionRuntime.inactiveTitle": "Perception service is stopped",
  "perceptionRuntime.notReadyTitle": "Perception service is not ready",
  "perceptionRuntime.noSourcesTitle": "No perception cameras are enabled",
  "perceptionRuntime.collectingTitle": "Watching and waiting for new signals",
  "perceptionRuntime.eventingTitle": "Watching with recent events",
  "perceptionRuntime.describingTitle": "Watching with recent scene descriptions",
  "perceptionRuntime.silentTitle": "Watching, but realtime semantics are empty",
  "perceptionRuntime.degradedTitle": "Perception pipeline is unstable",
  "perceptionRuntime.summaryLine": "{{inferences}} inferences today; {{raw}} descriptions and {{events}} events in the last hour",
  "perceptionRuntime.modelLine": "{{calls}} model calls and {{errors}} errors in the last {{minutes}} minutes",
  "perceptionRuntime.sourcesLine": "{{count}} cameras feeding perception",
  "perceptionRuntime.reload": "Refresh status"
}
```

- [ ] Step 1: Extend frontend tests for display data.

Add tests that pick the 15-minute window:

```ts
it("uses the 15 minute window for model-call display", () => {
  const s = summary("silent");
  s.windows = [
    { minutes: 5, cycleCount: 5, skippedCount: 0, videoPassCount: 1, audioPassCount: 0, holdPassCount: 1, omniCallCount: 4, omniErrorCount: 0, cycleErrorCount: 0, droppedWindowsCount: 0, overflowCount: 0 },
    { minutes: 15, cycleCount: 20, skippedCount: 0, videoPassCount: 8, audioPassCount: 0, holdPassCount: 5, omniCallCount: 15, omniErrorCount: 1, cycleErrorCount: 0, droppedWindowsCount: 0, overflowCount: 0 },
  ];

  expect(selectRuntimeDisplayWindow(s)?.minutes).toBe(15);
});
```

- [ ] Step 2: Implement `PerceptionRuntimeCard`.

The component should:

- Render nothing when summary is absent and not loading/error.
- Render a subtle loading skeleton when loading.
- Render warning text and a reload button on error.
- Use `derivePerceptionRuntimeView(summary)` for tone/title.
- Prefer the 15-minute window for the model line; fallback to the last available window.

- [ ] Step 3: Wire in `App.tsx`.

Add:

```ts
const perceptionRuntime = useAsync(
  () => getPerceptionRuntimeSummary(homeId),
  [homeId],
  { errorLabel: t("app.loadPerceptionRuntimeFail", "Failed to load perception runtime") },
);
```

Add safe polling:

```ts
useEffect(() => {
  if (activeTab !== "now") return;
  const id = window.setInterval(() => {
    void perceptionRuntime.reload();
  }, 30_000);
  return () => window.clearInterval(id);
}, [activeTab, perceptionRuntime.reload]);
```

If `useAsync.reload` is not stable, adapt to the existing app pattern by adding a local `runtimeTick` dependency. The important acceptance condition is 30-second polling while the overview is visible.

Render below `StatusRibbon`:

```tsx
<PerceptionRuntimeCard
  summary={perceptionRuntime.data}
  loading={perceptionRuntime.loading}
  error={perceptionRuntime.error}
  onReload={perceptionRuntime.reload}
/>
```

- [ ] Step 4: Verify.

```bash
cd web
pnpm test tests/perception-runtime.test.ts tests/perception-activity.test.ts -q
pnpm build
```

Expected: tests and build pass.

---

## Phase 2: Realtime Empty Semantics Diagnosis and Repair

### Task 7: Tighten Realtime Caption Contract

**Purpose:** Repair the current failure mode where valid realtime calls can return empty semantic fields for usable RTSP frames, while preserving strictness for rule hits, suggestions, and actions.

**Files:**

- Modify: `backend/miloco/src/miloco/perception/engine/omni/field_registry.py`
- Modify: `backend/miloco/tests/perception/engine/omni/test_prompt_builder.py`
- Modify only if tests prove parser adjustment is needed: `backend/miloco/src/miloco/perception/engine/omni/response_parser.py`

**Policy:**

- For video/image routes with usable visible content, `caption` should be non-empty.
- For unusable visual input, model may leave caption empty or use the existing safe degraded wording if parser already supports it.
- Keep audio-only routes free of `caption`.
- Do not instruct the model to invent events, suggestions, rule hits, or actions.

- [ ] Step 1: Add failing prompt-builder tests.

Append tests near existing caption/schema tests:

```py
def test_video_caption_spec_requires_non_empty_caption_for_usable_visual_window():
    from miloco.perception.engine.omni.field_registry import render_field_spec
    from miloco.perception.engine.types import SceneDescriptor

    spec = render_field_spec(SceneDescriptor(route="video", has_audio=False))

    assert "## caption" in spec
    assert "可见" in spec
    assert "非空" in spec
    assert "matched_rules" not in spec.split("## caption", 1)[1].split("##", 1)[0]
```

```py
def test_audio_only_route_still_omits_caption_contract():
    from miloco.perception.engine.omni.prompt_builder import render_schema
    from miloco.perception.engine.types import SceneDescriptor

    schema = render_schema(SceneDescriptor(route="audio", has_audio=True))
    assert '"caption"' not in schema
```

- [ ] Step 2: Update `CAPTION.spec_md`.

Add one early bullet under `## caption`:

```md
- 本轮只要有可用、可判断的画面内容，caption 必须非空；安静、无人、无异常也要描述当前可见状态。只有画面不可见、严重花屏/遮挡、无法判断时才可留空或说明不可判断。
```

Do not edit `MATCHED_RULES`, `SUGGESTIONS`, or action instructions unless a test demonstrates a direct contradiction.

- [ ] Step 3: Verify targeted prompt tests.

```bash
cd backend/miloco
uv run pytest tests/perception/engine/omni/test_prompt_builder.py -q
```

Expected: pass.

---

### Task 8: Add Runtime Empty-Semantics Acceptance Tests

**Purpose:** Guard against the specific regression: realtime output is syntactically successful but semantically empty forever.

**Files:**

- Modify: `backend/miloco/tests/perception/engine/test_pipeline.py`
- Modify: `backend/miloco/tests/test_perception_runtime_summary.py`

**Acceptance Invariant:**

When a realtime result contains no captions/rules/suggestions/speeches, the runtime diagnostics and summary must make that visible as `silent` after repeated cycles.

- [ ] Step 1: Add an engine-level diagnostic test.

Use existing fake Omni output patterns in `backend/miloco/tests/perception/engine/test_pipeline.py`. Add a test that simulates a successful empty output and verifies that a merged realtime result has zero field counts and records a diagnostic sample.

Expected assertions:

```py
latest = get_runtime_diagnostics().latest("realtime")
assert latest is not None
assert latest.parse_ok is True
assert latest.skipped is False
assert latest.caption_count == 0
assert latest.matched_rule_count == 0
assert latest.suggestion_count == 0
assert latest.speech_count == 0
```

- [ ] Step 2: Add a summary-state test for repeated empty results.

Use `PerceptionLogRepo.append()` with repeated `{}` descriptions or call the classification helper directly:

```py
state, hints = classify_perception_runtime_state(
    running=True,
    ready=True,
    active_source_count=2,
    raw_last_hour=0,
    meaningful_last_hour=0,
    consecutive_empty_descriptions=5,
    recent_window={"cycle_count": 10, "omni_call_count": 10, "omni_error_count": 0, "cycle_error_count": 0},
)
assert state == "silent"
assert "semantic_output_empty" in hints
```

- [ ] Step 3: Verify.

```bash
cd backend/miloco
uv run pytest \
  tests/perception/engine/test_pipeline.py \
  tests/test_perception_runtime_summary.py \
  -q
```

Expected: pass.

---

### Task 9: Full Local Verification

**Purpose:** Confirm backend, frontend, and build remain coherent before any deployment discussion.

- [ ] Step 1: Backend targeted suite.

```bash
cd backend/miloco
uv run pytest \
  tests/test_perception_repo_sqlite.py \
  tests/test_perception_runtime_diagnostics.py \
  tests/test_perception_runtime_summary.py \
  tests/perception/engine/omni/test_prompt_builder.py \
  tests/perception/engine/test_pipeline.py \
  -q
```

- [ ] Step 2: Frontend targeted suite.

```bash
cd web
pnpm test \
  tests/perception-runtime.test.ts \
  tests/perception-activity.test.ts \
  -q
```

- [ ] Step 3: Frontend production build.

```bash
cd web
pnpm build
```

- [ ] Step 4: Whole-repo hygiene.

```bash
git status --short
git diff --check
```

Expected:

- `git diff --check` has no whitespace errors.
- Manual review finds no unresolved plan/spec placeholders.
- `git status --short` includes only intended files.

---

### Task 10: Commit and Handoff

**Purpose:** Keep the implementation branch reviewable and ready for production CO when accepted.

- [ ] Step 1: Review changed files.

```bash
git status --short
git diff --stat
git diff -- docs/superpowers/specs/2026-08-30-perception-log-observability-design.md docs/superpowers/plans/2026-08-30-perception-log-observability.md
```

- [ ] Step 2: Commit from the worktree root.

```bash
git add \
  backend/miloco/src/miloco/database/perception_repo.py \
  backend/miloco/src/miloco/database/meaningful_events_dao.py \
  backend/miloco/src/miloco/perception/runtime_diagnostics.py \
  backend/miloco/src/miloco/perception/schema.py \
  backend/miloco/src/miloco/perception/service.py \
  backend/miloco/src/miloco/perception/router.py \
  backend/miloco/src/miloco/perception/engine/omni/omni_client.py \
  backend/miloco/src/miloco/perception/engine/api.py \
  backend/miloco/src/miloco/perception/engine/omni/field_registry.py \
  backend/miloco/tests/test_perception_repo_sqlite.py \
  backend/miloco/tests/test_perception_runtime_diagnostics.py \
  backend/miloco/tests/test_perception_runtime_summary.py \
  backend/miloco/tests/perception/engine/omni/test_prompt_builder.py \
  backend/miloco/tests/perception/engine/test_pipeline.py \
  web/src/lib/types.ts \
  web/src/api/real.ts \
  web/src/api/index.ts \
  web/src/lib/perceptionRuntime.ts \
  web/src/components/PerceptionRuntimeCard.tsx \
  web/src/App.tsx \
  web/src/i18n/locales/zh/common.json \
  web/src/i18n/locales/en/common.json \
  web/tests/perception-runtime.test.ts \
  docs/superpowers/specs/2026-08-30-perception-log-observability-design.md \
  docs/superpowers/plans/2026-08-30-perception-log-observability.md \
  docs/2026-08-30-perception-log-observability_PROGRESS.md
git commit -m "feat: expose perception runtime state"
```

For this planning-only commit, stage only the docs and progress files:

```bash
git add \
  docs/superpowers/specs/2026-08-30-perception-log-observability-design.md \
  docs/superpowers/plans/2026-08-30-perception-log-observability.md \
  docs/2026-08-30-perception-log-observability_PROGRESS.md
git commit -m "docs: plan perception runtime observability"
```

- [ ] Step 3: Optional push.

```bash
git push -u origin feature/perception-log-observability
```

If push is blocked by credentials or network, keep the local branch and report the exact branch/worktree path.

---

## Production Rollout Plan After Implementation

Do not execute this section during implementation unless the user explicitly approves deployment.

- [ ] Create a Software CO for `miloco.esxi`.
- [ ] Deploy the exact implementation SHA using the repository deployment path, not an ad-hoc full-bundle copy.
- [ ] Verify service health:

```bash
curl -fsS http://127.0.0.1:8111/health
```

- [ ] Verify runtime summary through the authenticated backend path, redacting any auth token and avoiding RTSP/model secrets:

```bash
curl -fsS -H 'Authorization: Bearer <redacted>' \
  http://127.0.0.1:8111/api/perception/runtime-summary
```

- [ ] Confirm dashboard root is reachable from LAN.
- [ ] Use browser/computer-use validation only for visual UI acceptance; do not expose credentials in screenshots or final notes.
- [ ] Acceptance outcome:
  - If `semantic_state` becomes `describing` or `eventing`, realtime semantics are flowing.
  - If `semantic_state` remains `silent`, the service is still active but the dashboard now explains the exact failure class; collect sanitized `latest_omni` field counts for the next targeted fix.

## Rollback Plan

If production deployment later causes dashboard/API instability:

- Revert to the previous deployed SHA through the existing deployment workflow.
- Keep the SQLite main DB and observability DB intact; this plan adds no required destructive migration.
- If the new endpoint fails but core service remains healthy, temporarily hide the frontend card by reverting frontend assets first.
- Close the CO with actual deployed/rolled-back SHA and sanitized evidence.
