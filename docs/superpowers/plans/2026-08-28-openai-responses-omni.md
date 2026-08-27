# OpenAI Responses Omni Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow Miloco's Omni perception path to call local visual models through the standard OpenAI `/responses` endpoint while preserving current MiMo/Qwen Chat Completions and Gemini behavior.

**Architecture:** Add a persisted explicit API protocol to model profiles, with the current model-name resolver retained only for old profiles missing the field. Declare each provider adapter's media mode so Responses receives uniformly sampled JPEG images instead of MP4/audio. Normalize Responses non-stream and SSE outputs back to Miloco's existing `{choices, usage}` internal shape, preserving downstream parsing, breaker, trace, and usage accounting.

**Tech Stack:** Python, Pydantic, httpx, OpenAI Responses HTTP/SSE contract, OpenCV, NumPy, React, TypeScript, pytest, Vitest.

**Spec:** [RTSP 摄像机与 OpenAI Responses 本地 Omni 支持设计](../specs/2026-08-28-rtsp-responses-support-design.md)

**Dependency:** None on either RTSP plan. This plan can be implemented and rolled back independently.

## Global Constraints

- Scope is Miloco Omni perception only. Do not change OpenClaw/Hermes Agent model selection.
- Supported explicit protocols are exactly `openai_chat_completions`, `openai_responses`, and `gemini_native`.
- New UI/API saves always persist a protocol. Only old profile records missing the field use legacy model-name inference.
- Never infer protocol from Base URL and never fall back to a different protocol after a call fails.
- Responses sends at most 6 evenly sampled panorama JPEGs and 6 prioritized crop JPEGs, with a hard total of 12.
- Responses sends no camera audio, MP4, `temperature`, `top_p`, tools, or provider-private fields in v1.
- Responses may omit API Key; if a nonempty key exists, send Bearer auth. Existing Chat/Gemini key behavior stays unchanged.
- Request bodies/base64/API keys are not written to ordinary logs. Existing sanitized trace rules remain in force.
- A fixture server proves Miloco's contract only. Real local-VLM E2E remains `not_measured` until an actual service passes visual preflight and a real perception window.
- Commit each task locally; do not push without an approved writable remote.

---

## Task 1: Add explicit protocol configuration with old-profile compatibility

**Files:**

- Modify: `backend/miloco/src/miloco/config/settings.py`
- Modify: `backend/miloco/src/miloco/config/settings.yaml`
- Modify: `backend/miloco/src/miloco/config/settings.schema.json`
- Modify: `backend/miloco/src/miloco/perception/engine/config.py`
- Modify: `backend/miloco/src/miloco/perception/engine/omni/provider.py`
- Modify: `backend/miloco/src/miloco/perception/engine/omni/omni_client.py`
- Modify: `cli/src/miloco_cli/config.py`
- Test: `backend/miloco/tests/perception/engine/omni/test_protocol_resolver.py`
- Modify/Test: `backend/miloco/tests/perception/engine/omni/test_provider.py`
- Modify/Test: `backend/miloco/tests/test_cli_schema_defaults.py`
- Modify/Test: `cli/tests/test_config.py`

**Interfaces:**

```python
OmniApiProtocol = Literal[
    "openai_chat_completions",
    "openai_responses",
    "gemini_native",
]


class OmniModelSettings(BaseModel):
    label: str = ""
    model: str = "xiaomi/mimo-v2.5"
    base_url: str = "https://api.xiaomimimo.com/v1"
    api_key: str = ""
    api_protocol: OmniApiProtocol | None = None


def resolve_api_protocol(
    configured: OmniApiProtocol | None,
    model: str,
) -> OmniApiProtocol:
    if configured is not None:
        return configured
    return "gemini_native" if "gemini" in model.lower() else "openai_chat_completions"


def get_adapter(
    protocol: OmniApiProtocol | None,
    model: str,
) -> OmniProviderAdapter: ...
```

- [ ] Add failing tests for every explicit protocol, invalid values, old MiMo/Qwen inference to Chat Completions, old Gemini inference to Gemini native, explicit Responses overriding any model name, and proof that Base URL never changes resolution.
- [ ] Add regression tests that the packaged default is explicitly `openai_chat_completions`, while a loaded legacy JSON profile with no field remains `None` until the resolver is called.
- [ ] Run focused tests and confirm failure.
- [ ] Add the optional field to Pydantic and `OmniConfig`; add the explicit packaged default and JSON schema enum. Add `model.omni.api_protocol` to CLI config validation/default alignment.
- [ ] Change every `get_adapter(config.model)` call to `get_adapter(config.api_protocol, config.model)`, including `omni.py`, `prompt_builder.py`, `probe.py`, and identity/fused paths. Do not leave a model-only overload that new code can accidentally use.
- [ ] Include protocol in `resolve_live_omni_config()` and the breaker reset key:

```python
resolved = replace(
    base,
    model=current.model,
    base_url=current.base_url,
    api_key=current.api_key or base.api_key,
    api_protocol=current.api_protocol,
)
triple = (
    resolve_api_protocol(resolved.api_protocol, resolved.model),
    resolved.model,
    resolved.base_url,
    resolve_omni_api_key(resolved.api_key),
)
```

- [ ] Run focused tests, provider regressions, CLI alignment tests, Ruff, and ty.
- [ ] Commit: `git add backend/miloco/src/miloco/config backend/miloco/src/miloco/perception/engine/config.py backend/miloco/src/miloco/perception/engine/omni/provider.py backend/miloco/src/miloco/perception/engine/omni/omni_client.py backend/miloco/src/miloco/perception/engine/omni/omni.py backend/miloco/src/miloco/perception/engine/omni/prompt_builder.py backend/miloco/src/miloco/perception/engine/identity backend/miloco/tests/perception/engine/omni/test_protocol_resolver.py backend/miloco/tests/perception/engine/omni/test_provider.py backend/miloco/tests/test_cli_schema_defaults.py cli/src/miloco_cli/config.py cli/tests/test_config.py && git commit -m "feat(omni): add explicit API protocol"`

## Task 2: Add protocol-directed image-sequence payload generation

**Files:**

- Modify: `backend/miloco/src/miloco/perception/engine/omni/provider.py`
- Modify: `backend/miloco/src/miloco/perception/engine/omni/prompt_builder.py`
- Modify: `backend/miloco/src/miloco/perception/engine/omni/omni.py`
- Test: `backend/miloco/tests/perception/engine/omni/test_responses_images.py`
- Modify/Test: `backend/miloco/tests/perception/engine/omni/test_prompt_builder.py`

**Interfaces:**

```python
OmniMediaMode = Literal["video_audio", "image_sequence"]


class OmniProviderAdapter(ABC):
    media_mode: OmniMediaMode = "video_audio"
    auth_required: bool = True


@dataclass(frozen=True)
class EncodedInputImage:
    source: Literal["panorama", "crop"]
    data: str
    media_type: Literal["image/jpeg"] = "image/jpeg"
    track_id: int | None = None


def encode_responses_images(
    packets: list[IdentityPacket],
    *,
    panorama_limit: int = 6,
    crop_limit: int = 6,
) -> list[EncodedInputImage]: ...
```

- [ ] Add failing tests for 0/1/6/more-than-6 panoramas, even temporal sampling, up to 6 existing prioritized crops, hard total `<=12`, deterministic order, duplicate crop suppression, JPEG validity, and input arrays remaining unmodified.
- [ ] Add a failing regression proving Chat/Gemini payloads still contain current MP4/audio/crop blocks and Responses payloads contain `images` but neither `video_base64` nor `audio_base64`.
- [ ] Run focused tests and confirm failure.
- [ ] Add `media_mode="image_sequence"` to the Responses adapter and keep all existing adapters at `video_audio`.
- [ ] Resolve the live config/adapter before prompt construction in each Omni entry point, then pass `media_mode` explicitly. Do not let `prompt_builder` re-resolve protocol from global settings.
- [ ] Sample panorama indices with the existing uniform-selection semantics, for example:

```python
def _even_indices(length: int, limit: int) -> list[int]:
    if length <= limit:
        return list(range(length))
    return sorted({round(i * (length - 1) / (limit - 1)) for i in range(limit)})
```

- [ ] Resize using the existing Omni short-edge/crop policy, encode BGR arrays as JPEG quality 85, base64 only in the final payload, and preserve panorama-before-crop ordering.
- [ ] Preserve the current identity candidate priority when truncating crops; deduplicate repeated `(track_id, image)` candidates deterministically.
- [ ] For Responses audio-only windows with zero images, do not send camera audio. Preserve text facts and mark the payload as text-only; the model call may proceed, but the visual preflight separately guarantees the configured service supports image input.
- [ ] Run prompt/provider regression tests, Ruff, and ty.
- [ ] Commit: `git add backend/miloco/src/miloco/perception/engine/omni/provider.py backend/miloco/src/miloco/perception/engine/omni/prompt_builder.py backend/miloco/src/miloco/perception/engine/omni/omni.py backend/miloco/tests/perception/engine/omni/test_responses_images.py backend/miloco/tests/perception/engine/omni/test_prompt_builder.py && git commit -m "feat(omni): build Responses image sequences"`

## Task 3: Implement the non-streaming Responses adapter and normalization

**Files:**

- Modify: `backend/miloco/src/miloco/perception/engine/omni/provider.py`
- Modify: `backend/miloco/src/miloco/perception/engine/omni/omni_client.py`
- Test: `backend/miloco/tests/perception/engine/omni/test_responses_provider.py`
- Modify/Test: `backend/miloco/tests/perception/engine/omni/test_omni_client_circuit.py`

**Request/normalization contract:**

```python
class OpenAIResponsesAdapter(OmniProviderAdapter):
    media_mode = "image_sequence"
    auth_required = False

    def endpoint(self, base_url: str, model: str, *, stream: bool) -> str:
        return f"{base_url.rstrip('/')}/responses"

    def auth_headers(self, api_key: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {api_key}"} if api_key else {}

    def build_request_body(self, messages, *, model, max_tokens, stream=False, **_):
        system, user = split_system_and_user(messages)
        return {
            "model": model,
            "instructions": system,
            "input": [{"role": "user", "content": to_responses_content(user)}],
            "max_output_tokens": max_tokens,
            "stream": stream,
        }
```

- [ ] Add failing tests asserting exact `/responses` path, `instructions`, `input_text`, multiple `input_image` data URLs, `max_output_tokens`, `stream`, no temperature/top_p/tools/video/audio, empty-key headers, and Bearer-key headers.
- [ ] Add failing parse tests for one/multiple `output_text` blocks, irrelevant output blocks, missing output, empty text, usage with/without cached tokens, and malformed top-level response.
- [ ] Run focused tests and confirm failure.
- [ ] Implement conversion from internal OpenAI-shaped messages to Responses blocks. Reject unsupported media blocks with a stable local configuration error instead of silently dropping them.
- [ ] Normalize non-stream output:

```python
text = "".join(
    part.get("text", "")
    for item in raw.get("output", [])
    for part in item.get("content", [])
    if part.get("type") == "output_text"
)
if not text.strip():
    raise OmniError("Responses output contains no output_text")

usage_in = raw.get("usage") or {}
normalized = {
    "choices": [{"message": {"content": text}}],
    "usage": {
        "prompt_tokens": int(usage_in.get("input_tokens") or 0),
        "completion_tokens": int(usage_in.get("output_tokens") or 0),
        "total_tokens": int(usage_in.get("total_tokens") or 0),
        "prompt_tokens_details": {
            "cached_tokens": int(
                (usage_in.get("input_tokens_details") or {}).get("cached_tokens") or 0
            )
        },
    },
}
```

- [ ] Change the runtime key gate to require a key only when `adapter.auth_required` is true. Existing Chat/Gemini adapters remain required; Responses does not.
- [ ] Keep response normalization before `fire_record`, `extract_usage`, response parsing, and trace publication so downstream modules need no protocol branch.
- [ ] Ensure `bad_response` reaches the existing circuit breaker classification and no raw response/request image is logged.
- [ ] Run focused tests, current provider/client/circuit tests, Ruff, and ty.
- [ ] Commit: `git add backend/miloco/src/miloco/perception/engine/omni/provider.py backend/miloco/src/miloco/perception/engine/omni/omni_client.py backend/miloco/tests/perception/engine/omni/test_responses_provider.py backend/miloco/tests/perception/engine/omni/test_omni_client_circuit.py && git commit -m "feat(omni): call OpenAI Responses API"`

## Task 4: Parse Responses SSE without changing downstream streaming consumers

**Files:**

- Modify: `backend/miloco/src/miloco/perception/engine/omni/provider.py`
- Modify: `backend/miloco/src/miloco/perception/engine/omni/omni_client.py`
- Modify/Test: `backend/miloco/tests/perception/engine/omni/test_collect_stream.py`
- Create: `backend/miloco/tests/perception/engine/omni/test_responses_stream.py`

**SSE behavior:**

- `response.output_text.delta`: return its `delta` text.
- `response.completed`: normalize `response.usage` and return usage without text.
- `response.failed`, `response.incomplete`, and `error`: raise a protocol error carrying only stable code/message.
- Unknown event types: ignore and increment a debug counter; do not log event payloads.

- [ ] Add failing tests for fragmented delta events, completed usage, event order, CRLF/blank/comment lines, an explicit `event:` line plus `data:`, unknown events, malformed JSON, failed/incomplete/error events, and clean stream closure.
- [ ] Add a failing test proving `call_omni_stream()` yields exactly the same text fragments and fills `usage_out` in the same internal keys as current providers.
- [ ] Run focused tests and confirm failure.
- [ ] Extend `_iter_sse_chunks` to preserve event type when the server uses `event:` lines while still accepting the JSON `type` field used by Responses. Continue accepting current Chat/Gemini `data:` streams.
- [ ] Let the Responses adapter parse its own standard event types. Convert terminal failures into existing error categories; do not retry or fall back inside the adapter.
- [ ] Ensure `_collect_stream_response()` can join Responses deltas and include completed usage for non-stream callers when an adapter forces/uses SSE.
- [ ] Run focused stream tests and all existing Omni streaming tests, Ruff, and ty.
- [ ] Commit: `git add backend/miloco/src/miloco/perception/engine/omni/provider.py backend/miloco/src/miloco/perception/engine/omni/omni_client.py backend/miloco/tests/perception/engine/omni/test_collect_stream.py backend/miloco/tests/perception/engine/omni/test_responses_stream.py && git commit -m "feat(omni): parse Responses streaming events"`

## Task 5: Make preflight prove image support and treat `/models` as optional

**Files:**

- Modify: `backend/miloco/src/miloco/perception/engine/omni/probe.py`
- Add fixture: `backend/miloco/src/miloco/perception/engine/omni/assets/visual_probe_red.jpg`
- Modify/Test: `backend/miloco/tests/perception/engine/omni/test_probe.py`
- Modify/Test: `backend/miloco/tests/admin/test_omni_config_preflight.py`

**Interface:**

```python
async def probe_omni(
    model: str,
    base_url: str,
    api_key: str,
    api_protocol: OmniApiProtocol | None,
) -> dict[str, Any]: ...
```

- [ ] Add failing tests for Responses with no key, Bearer key, `/models` success, `/models` 404/405 followed by successful visual call, text-only `/responses` rejection, missing output text, invalid usage warning, auth failure, timeout, rate limit/`Retry-After`, and no protocol fallback.
- [ ] Use a tiny deterministic red JPEG asset and ask for exactly one short visual fact about its dominant color. Require a nonempty `output_text`; additionally require a case-insensitive `red` result so a text-only endpoint cannot pass by returning a generic acknowledgment.
- [ ] Run focused tests and confirm failure.
- [ ] For Responses, first attempt `/models` only as optional discovery. Treat 404/405 as unsupported discovery, not a test failure; all other auth/rate/network errors remain classified.
- [ ] Send the actual image through `OpenAIResponsesAdapter` to `/responses`. Parse with the same adapter as runtime.
- [ ] Return a warning field when usage is absent but visual text is valid. Return `bad_response` when the response shape/text/color proof fails.
- [ ] Keep the current two-stage Chat and Gemini probe behavior unchanged.
- [ ] Run probe/admin preflight tests, Ruff, and ty.
- [ ] Commit: `git add backend/miloco/src/miloco/perception/engine/omni/probe.py backend/miloco/src/miloco/perception/engine/omni/assets/visual_probe_red.jpg backend/miloco/tests/perception/engine/omni/test_probe.py backend/miloco/tests/admin/test_omni_config_preflight.py && git commit -m "feat(omni): add Responses visual preflight"`

## Task 6: Persist and expose protocol through admin API, CLI, and web

**Files:**

- Modify: `backend/miloco/src/miloco/admin/router.py`
- Modify/Test: `backend/miloco/tests/admin/test_omni_config.py`
- Modify/Test: `backend/miloco/tests/admin/test_omni_config_preflight.py`
- Modify: `cli/src/miloco_cli/commands/admin.py`
- Create: `cli/tests/test_omni_protocol_commands.py`
- Modify: `web/src/lib/types.ts`
- Modify: `web/src/api/real.ts`
- Modify: `web/src/components/UsageOmniConfig.tsx`
- Modify: `web/src/i18n/locales/zh/usage.json`
- Modify: `web/src/i18n/locales/en/usage.json`
- Modify/Test: `web/tests/real.test.ts`
- Create: `web/tests/omni-protocol-form.test.ts`

**Public data contract:**

```typescript
export type OmniApiProtocol =
  | "openai_chat_completions"
  | "openai_responses"
  | "gemini_native";

export interface OmniModelConfig {
  label: string;
  model: string;
  base_url: string;
  api_protocol: OmniApiProtocol;
  protocol_inferred: boolean;
  api_key_masked: string;
  has_key: boolean;
}
```

- [ ] Add failing backend tests that old records are returned with resolved protocol plus `protocol_inferred=true`, all PUT/save operations require and persist an explicit protocol, activate/deactivate/delete preserve it, Base URL changes never reuse an old key, and Responses accepts blank key while Chat/Gemini retain current key validation.
- [ ] Add failing CLI tests for creating/testing/selecting a profile with `--api-protocol`, exact enum validation, blank-key Responses, and masked output.
- [ ] Add failing web API/form tests for serialization, protocol selection, Responses key optionality, explanatory copy, hidden/disabled temperature/top-p applicability, and visual-test labeling.
- [ ] Run focused backend, CLI, and web tests and confirm failure.
- [ ] Include `api_protocol` in every admin profile serializer/body. On new or edited saves, reject a missing value rather than relying on model-name inference.
- [ ] Keep label uniqueness and credential cross-URL isolation unchanged. Add protocol to the active-config change key so hot updates reset breaker state.
- [ ] Add CLI `--api-protocol [openai_chat_completions|openai_responses|gemini_native]` to the existing Omni admin profile flow; do not add a second config store.
- [ ] In `UsageOmniConfig.tsx`, add an explicit protocol selector. When Responses is selected, make Key optional, show “图片序列视觉感知”, state that camera audio is not sent, and label Test as a visual preflight. Keep current behavior for other protocols.
- [ ] Include protocol in the identity used to detect/update an existing profile so equal model/Base URL but different protocol is not silently overwritten.
- [ ] Run full relevant backend admin tests, full CLI tests, full web tests/typecheck/build, Ruff, and ty.
- [ ] Commit: `git add backend/miloco/src/miloco/admin/router.py backend/miloco/tests/admin/test_omni_config.py backend/miloco/tests/admin/test_omni_config_preflight.py cli/src/miloco_cli/commands/admin.py cli/tests/test_omni_protocol_commands.py web/src/lib/types.ts web/src/api/real.ts web/src/components/UsageOmniConfig.tsx web/src/i18n/locales/zh/usage.json web/src/i18n/locales/en/usage.json web/tests/real.test.ts web/tests/omni-protocol-form.test.ts && git commit -m "feat(omni): manage Responses model profiles"`

## Task 7: Prove contract integration, regress providers, and close the batch

**Files:**

- Create: `backend/miloco/tests/integration/responses_fixture_server.py`
- Create: `backend/miloco/tests/integration/test_responses_perception.py`
- Create: `scripts/responses-vlm-smoke.sh`
- Modify: `docs/2026-08-28-rtsp-responses-support_PROGRESS.md`
- Modify: `docs/superpowers/specs/2026-08-28-rtsp-responses-support-design.md`

- [ ] Implement a strict local fixture server with `/models` optional mode and `/responses` non-stream/SSE modes. It must reject missing `input_image`, more than 12 images, audio/video blocks, sampling/tool fields, wrong auth behavior, and wrong path.
- [ ] Add a perception integration test that constructs a real `IdentityPacket`, builds up to 12 JPEG inputs, calls the fixture through httpx, normalizes output/usage, and feeds the text to the existing response parser. Cover non-stream, stream, no-key, Bearer-key, breaker failure, and trace sanitization.
- [ ] Add regression assertions for MiMo, Qwen, and Gemini request/response fixtures.
- [ ] Add a real-service smoke script using environment variables `MILOCO_RESPONSES_BASE_URL`, `MILOCO_RESPONSES_MODEL`, and optional `MILOCO_RESPONSES_API_KEY`. It must run visual preflight plus one synthetic perception packet and print only protocol/model, latency, image count, output presence, and token counts.
- [ ] Run `cd backend && uv run pytest miloco/tests/perception/engine/omni miloco/tests/admin/test_omni_config.py miloco/tests/admin/test_omni_config_preflight.py miloco/tests/integration/test_responses_perception.py -q`.
- [ ] Run `cd cli && uv run pytest -q`; run `cd web && npm test && npm run typecheck && npm run build`.
- [ ] Run `cd backend && uv run task check`, `cd backend && uv run task lint`, and `./scripts/local-ci.sh --tests`.
- [ ] Run a credential/base64 leak scan over captured logs and trace fixtures. Assert API keys and `data:image/jpeg;base64,` payloads are absent from ordinary logs; sanitized structured trace metadata may retain image count/media type only.
- [ ] If an actual local VLM endpoint is available, run `./scripts/responses-vlm-smoke.sh` and record service/version, visual preflight, real normalized output, latency, and usage without secrets. If not, record real local-VLM E2E as `not_measured`; fixture success remains contract proof only.
- [ ] Update the design status to include `Responses 感知已实施` only when mandatory automated gates pass. Do not state compatibility with all local VLM servers from one fixture or one real service.
- [ ] Commit: `git add backend/miloco/tests/integration/responses_fixture_server.py backend/miloco/tests/integration/test_responses_perception.py scripts/responses-vlm-smoke.sh docs/2026-08-28-rtsp-responses-support_PROGRESS.md docs/superpowers/specs/2026-08-28-rtsp-responses-support-design.md && git commit -m "test(omni): verify Responses perception contract"`

## Completion Gate

- Existing MiMo, Qwen, and Gemini tests pass without protocol inference changes for legacy records.
- New/edited profiles persist an explicit protocol; Base URL never selects it.
- Responses request path/body contains standard text/images only, with image limits enforced.
- Empty-key and Bearer-key modes work as specified.
- Non-stream and SSE outputs normalize to current `choices`/usage and reach the existing response parser, breaker, trace, and usage paths.
- Visual preflight rejects the strict text-only fixture and tolerates missing `/models` when `/responses` works.
- Web and CLI expose protocol without leaking keys.
- Contract fixture E2E passes; real local-VLM E2E is evidenced or explicitly `not_measured`.
