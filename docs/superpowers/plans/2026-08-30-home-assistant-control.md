# Home Assistant Control Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Home Assistant as a second Miloco smart-home control source, with a dedicated left-sidebar management tab, explicit entity import, and per-imported-device control permission.

**Architecture:** Add Home Assistant behind a new `miloco.home_assistant` package and expose MiOT + HA through a new source-aware `miloco.devices` layer. Existing `/api/miot/*` endpoints remain MiOT-only and backward-compatible, while web, CLI, Agent catalog, and rules migrate to `/api/devices/*` for unified device reads and controls. HA tokens are stored only in backend config, and HA service calls are translated from Miloco normalized specs rather than passed through as raw HA API calls.

**Tech Stack:** Python 3.11+ backend with FastAPI, Pydantic v2, httpx, SQLite/action-ledger helpers, and existing JSON/YAML config loading; Click-based `miloco-cli`; React 19 + Vite 6 + Vitest in node environment; TypeScript OpenClaw plugin and Python Hermes plugin.

**Spec:** `docs/superpowers/specs/2026-08-30-home-assistant-control-design.md`

## Global Constraints

- Keep Xiaomi MiOT behavior, Xiaomi device IDs, existing MiOT OAuth flow, existing RTSP camera behavior, and all existing `/api/miot/*` routes backward-compatible.
- MVP supports one Home Assistant instance in the UI, keyed as `primary`; the ID and settings shape must not block future multi-instance support.
- Home Assistant REST API access uses `GET /api/`, `GET /api/config`, `GET /api/states`, `GET /api/services`, and `POST /api/services/<domain>/<service>` with `Authorization: Bearer <token>`.
- Use REST polling for MVP. `/api/websocket` state subscription is a post-MVP enhancement and must not be required for this implementation.
- A newly discovered HA entity is not imported into Miloco automatically.
- An imported HA entity defaults to read-only. The user must explicitly enable `control_enabled` per entity before writable/action specs are exposed or accepted.
- Backend authorization is the source of truth. Frontend toggles, CLI rendering, and Agent catalog visibility must all reflect backend policy; none of them may be the only enforcement point.
- Blocked-risk domains remain non-controllable in MVP: `lock`, `alarm_control_panel`, `valve`, `water_heater`, `siren`, `button`, garage-door-like covers, and any destructive or safety-critical service.
- The Agent, CLI, and frontend may not call arbitrary Home Assistant domain/service pairs. They must use Miloco device `spec_name` / `iid` values that the backend maps to allowlisted HA services.
- HA long-lived access tokens must not appear in API responses, browser DOM after save, CLI output, logs, action ledger rows, tests, docs, or commits.
- Frontend tests stay compatible with the current Vitest `node` environment. Put DOM-free logic in exported helpers and reserve full rendered UX proof for browser/manual acceptance.
- Production deployment to `miloco.esxi` requires a separate approved CO/PAM path. Implementation is local until a release SHA is built, tested, and explicitly approved for production rollout.

---

## Implementation Boundary

### In scope

- Home Assistant connection settings:
  - `enabled`
  - `base_url`
  - long-lived access token
  - `instance_key`
  - TLS verification flag
  - request timeout
  - state cache TTL
  - per-entity import and control policy
- Backend HA client and service:
  - connection test
  - HA config metadata read
  - entity state discovery
  - service metadata discovery
  - allowlisted service calls
  - stable error codes for unconfigured, unreachable, unauthorized, timeout, invalid JSON, control disabled, unsupported domain, and service rejected
- Unified device model:
  - `source`: `miot` or `home_assistant`
  - stable HA Miloco ID: `ha:<instance_key>:<entity_id>`
  - source label
  - name, room, category, online state
  - readable properties
  - writable/action specs only when HA entity policy allows control
- MVP HA control domains:
  - `switch`: `on`
  - `light`: `on`, plus brightness and color temperature when safely derivable
  - `fan`: `on`, plus percentage and preset mode when safely derivable
  - `cover`: open, close, stop, and position only when the entity is not blocked as access-control-like
  - `climate`: target temperature and HVAC mode when valid options are exposed
  - `scene`: trigger existing scene
  - `script`: trigger existing script only through explicit target match
- Dedicated Home Assistant tab in desktop sidebar and mobile tab bar.
- Home Assistant tab management:
  - configure/test/save connection
  - refresh discovery
  - import/remove entities
  - enable/disable control permission per imported entity
  - show blocked/support status and last error
- Normal Devices tab:
  - show Xiaomi devices as before
  - show imported HA devices alongside Xiaomi devices
  - show source badges
  - hide write controls for read-only HA devices
- CLI, Agent catalog, and STATIC rule integration through the unified device layer.
- Action ledger/source metadata for HA attempts without secrets.
- Documentation and local CI coverage for the new flow.

### Out of scope for MVP

- Multiple HA instances in the UI.
- Home Assistant OAuth application flow.
- Miloco as a Home Assistant add-on.
- Creating, editing, or deleting HA automations.
- HA camera ingestion into Miloco perception.
- WebSocket-based live sync.
- Raw Home Assistant API passthrough from Agent, CLI, frontend, or rules.
- Production deployment without a fresh CO/PAM change path.

## Design Decisions

### 1. Source-aware IDs

Keep Xiaomi device IDs unchanged. Assign HA entities stable IDs in this form:

```text
ha:<instance_key>:<entity_id>
ha:primary:light.kitchen
ha:primary:climate.living_room_ac
```

Route parsing is deterministic: IDs starting with `ha:` are handled by the HA adapter; all other IDs go to the MiOT adapter. Any frontend or CLI path segment containing the full HA ID must URL-encode the ID.

### 2. Import and control permission are separate

`included=true` means the HA entity appears in normal Miloco devices, CLI list/catalog, and Agent read-only catalog. `control_enabled=true` means Miloco may expose writable/action specs and accept control calls.

For unsupported or blocked-risk domains, the backend returns `control_blocked_reason` and refuses `control_enabled=true`.

### 3. Unified API without breaking old API

New routes:

- `GET /api/devices/home?refresh=false`
- `GET /api/devices`
- `GET /api/devices/{device_id}/spec`
- `GET /api/devices/{device_id}/status`
- `POST /api/devices/{device_id}/control`
- `GET /api/devices/history`
- `GET /api/scenes`
- `POST /api/scenes/{scene_id}/trigger`
- `GET /api/home-assistant/status`
- `GET /api/home-assistant/config`
- `POST /api/home-assistant/config`
- `POST /api/home-assistant/test`
- `POST /api/home-assistant/refresh`
- `GET /api/home-assistant/entities`
- `PUT /api/home-assistant/entities/{entity_id}/policy`

Existing `/api/miot/*` routes stay in place and continue to serve MiOT-only consumers.

### 4. Normalized HA specs

Miloco synthesizes small specs from HA domain, state attributes, and service metadata. Target spec names:

- `on|wr|bool`
- `brightness|wr|uint8|[0,255]`
- `brightness_pct|w|uint8|[1,100]`
- `color-temperature|w|uint16`
- `target-temperature|wr|float|[min,max;step]|celsius`
- `hvac-mode|wr|string|Off=off,Cool=cool,Heat=heat`
- `open|x`
- `close|x`
- `stop|x`
- `position|w|uint8|[0,100]`
- `trigger|x`

Read-only imported entities may expose read specs but no `w`, `wr`, or `x` entries.

### 5. Secret-safe config

The token may be persisted in `$MILOCO_HOME/config.json` under `home_assistant.token`, using existing config precedence. API readback returns only redacted status:

- `token_configured`
- `token_mask`
- connection state
- HA metadata
- entity counts
- last sync time
- last error code/message

## File Structure

### Backend config

- Modify `backend/miloco/src/miloco/config/settings.py`: add `HomeAssistantSettings`, `HomeAssistantEntityPolicy`, URL checks, and `MilocoSettings.home_assistant`.
- Modify `backend/miloco/src/miloco/config/settings.yaml`: add default disabled HA block with blank token.
- Modify `backend/miloco/src/miloco/config/settings.schema.json`: expose the user-editable HA config contract.
- Add `backend/miloco/tests/config/test_home_assistant_settings.py`: settings and redaction tests.

### Backend Home Assistant package

- Create `backend/miloco/src/miloco/home_assistant/__init__.py`: package exports.
- Create `backend/miloco/src/miloco/home_assistant/schema.py`: HA DTOs, policy DTOs, status DTOs, service call DTOs, and error codes.
- Create `backend/miloco/src/miloco/home_assistant/client.py`: httpx REST client with bearer auth and stable error classification.
- Create `backend/miloco/src/miloco/home_assistant/mapper.py`: HA states/services to Miloco normalized devices/specs.
- Create `backend/miloco/src/miloco/home_assistant/service.py`: settings orchestration, discovery cache, policy application, and service-call dispatch.
- Create `backend/miloco/src/miloco/home_assistant/router.py`: FastAPI management endpoints.
- Add tests under `backend/miloco/tests/home_assistant/`.

### Backend unified device package

- Create `backend/miloco/src/miloco/devices/__init__.py`: package exports.
- Create `backend/miloco/src/miloco/devices/schema.py`: source-aware device, spec, scene, control, and result models.
- Create `backend/miloco/src/miloco/devices/service.py`: MiOT adapter, HA adapter, merged home info, and unified control dispatch.
- Create `backend/miloco/src/miloco/devices/router.py`: `/api/devices/*` and source-aware scene routes.
- Modify `backend/miloco/src/miloco/manager.py`: initialize `home_assistant_service` and `devices_service`.
- Modify `backend/miloco/src/miloco/main.py`: include HA and unified device routers.
- Add tests under `backend/miloco/tests/devices/`.

### Rules and observability

- Modify `backend/miloco/src/miloco/rule/schema.py`: add nullable action `source` and support HA action identifiers while preserving old MiOT JSON.
- Modify `backend/miloco/src/miloco/rule/service.py`: validate HA actions against unified device specs and entity policy.
- Modify `backend/miloco/src/miloco/rule/runner.py`: dispatch HA actions through unified device service.
- Create `backend/miloco/src/miloco/devices/ledger.py`: shared action-ledger helper for MiOT and HA control attempts, preserving the existing MiOT row semantics while allowing `source=home_assistant`.
- Add `backend/miloco/tests/test_rule_home_assistant.py`.

### CLI

- Modify `cli/src/miloco_cli/home_info.py`: use `/api/devices/home`.
- Modify `cli/src/miloco_cli/catalog.py`: source-aware catalog rows and HA read-only annotations.
- Modify `cli/src/miloco_cli/commands/device.py`: route spec/status/control/props/action through `/api/devices/*`.
- Create `cli/src/miloco_cli/commands/home_assistant.py`: status, test, refresh, import, remove, enable-control, disable-control.
- Modify `cli/src/miloco_cli/main.py`: register `home-assistant`.
- Add `cli/tests/test_home_assistant_commands.py`.
- Add `cli/tests/test_device_home_assistant.py`.

### Agent plugins

- Modify `plugins/skills/miloco-devices/SKILL.md`: describe Miloco smart-home devices as MiOT + HA.
- Modify `plugins/openclaw/src/services/catalog.ts`: accept source-aware catalog rows.
- Modify `plugins/openclaw/src/hooks/prompt.ts`: prompt text says no raw HA service calls.
- Modify `plugins/hermes/miloco-plugin/catalog.py`: source-aware catalog loading.
- Modify `plugins/hermes/miloco-plugin/context_injection.py`: prompt injection copy alignment.
- Modify `plugins/hermes/tests/test_catalog.py`: HA source row parsing and read-only catalog assertions.
- Modify `plugins/hermes/tests/test_context_injection.py`: injected prompt includes HA safety wording.
- Modify `plugins/openclaw/tests/catalog.test.ts`: source-aware catalog parsing.
- Modify `plugins/openclaw/tests/prompt.test.ts`: raw HA service calls are forbidden in prompt text.

### Web

- Modify `web/src/lib/types.ts`: source-aware device, HA status, HA entity, HA policy, and HA config DTOs.
- Modify `web/src/api/index.ts`: exported HA API functions and unified `listDevices` behavior.
- Modify `web/src/api/real.ts`: backend DTO mapping for `/api/devices/*` and `/api/home-assistant/*`.
- Modify `web/src/components/Sidebar.tsx`: add desktop and mobile `homeAssistant` navigation entry.
- Modify `web/src/App.tsx`: load HA status/entities and render HA tab.
- Modify `web/src/components/DevicesByRoom.tsx`: source badge and read-only HA behavior.
- Create `web/src/components/HomeAssistantPage.tsx`: connection, discovery, import, and control-permission management.
- Create `web/src/lib/homeAssistant.ts`: node-testable pure helpers for status labels, permission disabled reasons, and token masking.
- Modify `web/src/i18n/locales/zh/nav.json`: add `homeAssistant` and `homeAssistantHint`.
- Modify `web/src/i18n/locales/en/nav.json`: add `homeAssistant` and `homeAssistantHint`.
- Create `web/src/i18n/locales/zh/homeAssistant.json`: HA page Chinese copy.
- Create `web/src/i18n/locales/en/homeAssistant.json`: HA page English copy.
- Modify `web/src/i18n/locales/zh/devices.json`: source badge and read-only copy.
- Modify `web/src/i18n/locales/en/devices.json`: source badge and read-only copy.
- Add `web/tests/homeAssistant.test.ts`.
- Add `web/tests/devices-source.test.ts`.

### Docs and deployment artifacts

- Modify `README.md`
- Modify `README.zh.md`
- Modify `user_guide.md`
- Modify `user_guide_zh.md`
- Modify `knowledge/03-features/device-control.md`
- Modify `knowledge/06-dev-guide/troubleshooting.md`
- Update `docs/2026-08-30-home-assistant-control_PROGRESS.md` during execution.

## Implementation Tasks

### Task 1: Secret-safe Home Assistant settings

**Files:**

- Modify: `backend/miloco/src/miloco/config/settings.py`
- Modify: `backend/miloco/src/miloco/config/settings.yaml`
- Modify: `backend/miloco/src/miloco/config/settings.schema.json`
- Create: `backend/miloco/tests/config/test_home_assistant_settings.py`

**Interfaces:**

- Produces `HomeAssistantEntityPolicy(entity_id: str, included: bool, control_enabled: bool, last_seen_at: int | None, last_control_at: int | None, last_error: str | None)`.
- Produces `HomeAssistantSettings(enabled: bool, base_url: str, token: str, instance_key: str, verify_tls: bool, timeout_seconds: float, state_cache_ttl_seconds: int, entities: dict[str, HomeAssistantEntityPolicy])`.
- Produces `MilocoSettings.home_assistant`.

- [ ] **Step 1: Write failing settings tests**

```python
import pytest
from pydantic import ValidationError

from miloco.config.settings import HomeAssistantSettings, MilocoSettings


def test_home_assistant_settings_defaults_to_disabled():
    settings = MilocoSettings()
    assert settings.home_assistant.enabled is False
    assert settings.home_assistant.instance_key == "primary"
    assert settings.home_assistant.base_url == ""
    assert settings.home_assistant.token == ""


def test_home_assistant_settings_normalizes_trailing_slash():
    ha = HomeAssistantSettings(
        enabled=True,
        base_url="http://ha.lan:8123/",
        token="secret-token",
    )
    assert ha.base_url == "http://ha.lan:8123"


def test_home_assistant_invalid_url_does_not_leak_token():
    with pytest.raises(ValidationError) as exc:
        HomeAssistantSettings(
            enabled=True,
            base_url="ftp://ha.lan:8123",
            token="secret-token",
        )
    assert "secret-token" not in str(exc.value)
```

- [ ] **Step 2: Run tests and confirm they fail for missing settings**

Run: `cd backend && MILOCO_CONFIG_SEARCH_PATH=/tmp/miloco-ha-plan-empty MILOCO_SERVER__TOKEN='' uv run pytest miloco/tests/config/test_home_assistant_settings.py -q`

Expected: fails because `HomeAssistantSettings` or `settings.home_assistant` is not defined.

- [ ] **Step 3: Implement settings models**

```python
class HomeAssistantEntityPolicy(BaseModel):
    entity_id: str = Field(..., min_length=1)
    included: bool = False
    control_enabled: bool = False
    last_seen_at: int | None = None
    last_control_at: int | None = None
    last_error: str | None = None


class HomeAssistantSettings(BaseModel):
    model_config = ConfigDict(hide_input_in_errors=True)

    enabled: bool = False
    base_url: str = ""
    token: str = Field(default="", repr=False)
    instance_key: str = "primary"
    verify_tls: bool = True
    timeout_seconds: float = Field(default=8.0, gt=0)
    state_cache_ttl_seconds: int = Field(default=10, ge=0)
    entities: dict[str, HomeAssistantEntityPolicy] = Field(default_factory=dict)
```

- [ ] **Step 4: Implement URL and instance-key checks**

```python
@field_validator("base_url")
@classmethod
def _normalize_base_url(cls, value: str) -> str:
    value = value.strip().rstrip("/")
    if value == "":
        return ""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.fragment:
        raise ValueError("Home Assistant base_url must be http(s) with host and no fragment")
    return value


@field_validator("instance_key")
@classmethod
def _validate_instance_key(cls, value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", value):
        raise ValueError("Home Assistant instance_key must match [a-z][a-z0-9_-]{0,31}")
    return value
```

- [ ] **Step 5: Add defaults to YAML and schema**

```yaml
home_assistant:
  enabled: false
  base_url: ""
  token: ""
  instance_key: primary
  verify_tls: true
  timeout_seconds: 8.0
  state_cache_ttl_seconds: 10
  entities: {}
```

- [ ] **Step 6: Run focused and existing settings tests**

Run: `cd backend && MILOCO_CONFIG_SEARCH_PATH=/tmp/miloco-ha-plan-empty MILOCO_SERVER__TOKEN='' uv run pytest miloco/tests/config/test_home_assistant_settings.py miloco/tests/test_settings.py -q`

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add backend/miloco/src/miloco/config/settings.py backend/miloco/src/miloco/config/settings.yaml backend/miloco/src/miloco/config/settings.schema.json backend/miloco/tests/config/test_home_assistant_settings.py
git commit -m "feat: add home assistant settings"
```

### Task 2: HA schemas and REST client

**Files:**

- Create: `backend/miloco/src/miloco/home_assistant/__init__.py`
- Create: `backend/miloco/src/miloco/home_assistant/schema.py`
- Create: `backend/miloco/src/miloco/home_assistant/client.py`
- Create: `backend/miloco/tests/home_assistant/test_client.py`

**Interfaces:**

- Produces `HaErrorCode` string enum.
- Produces `HomeAssistantClient(base_url: str, token: str, timeout_seconds: float, verify_tls: bool, transport: httpx.AsyncBaseTransport | None = None)`.
- Produces async methods `ping()`, `get_config()`, `get_states()`, `get_services()`, `call_service(domain: str, service: str, data: dict[str, object])`.
- Raises `HomeAssistantError(code: HaErrorCode, message: str, status_code: int | None)`.

- [ ] **Step 1: Write failing client tests with `httpx.MockTransport`**

```python
import httpx
import pytest

from miloco.home_assistant.client import HomeAssistantClient
from miloco.home_assistant.schema import HaErrorCode, HomeAssistantError


@pytest.mark.asyncio
async def test_client_sends_bearer_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"message": "API running."})

    client = HomeAssistantClient(
        "http://ha.local:8123",
        "secret-token",
        transport=httpx.MockTransport(handler),
    )
    await client.ping()
    assert seen["auth"] == "Bearer secret-token"


@pytest.mark.asyncio
async def test_client_unauthorized_redacts_token():
    client = HomeAssistantClient(
        "http://ha.local:8123",
        "secret-token",
        transport=httpx.MockTransport(lambda request: httpx.Response(401, json={})),
    )
    with pytest.raises(HomeAssistantError) as exc:
        await client.get_states()
    assert exc.value.code == HaErrorCode.UNAUTHORIZED
    assert "secret-token" not in str(exc.value)
```

- [ ] **Step 2: Run tests and confirm missing package failure**

Run: `cd backend && uv run pytest miloco/tests/home_assistant/test_client.py -q`

Expected: fails because `miloco.home_assistant` is not implemented.

- [ ] **Step 3: Implement schema primitives**

```python
class HaErrorCode(str, Enum):
    NOT_CONFIGURED = "ha_not_configured"
    UNREACHABLE = "ha_unreachable"
    UNAUTHORIZED = "ha_unauthorized"
    TIMEOUT = "ha_timeout"
    INVALID_JSON = "ha_invalid_json"
    SERVICE_REJECTED = "ha_service_rejected"
    CONTROL_DISABLED = "ha_control_disabled"
    UNSUPPORTED_DOMAIN = "ha_unsupported_domain"


class HomeAssistantError(Exception):
    def __init__(self, code: HaErrorCode, message: str, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
```

- [ ] **Step 4: Implement client request helper**

```python
async def _request(self, method: str, path: str, json_body: dict[str, object] | None = None) -> object:
    try:
        resp = await self._client.request(method, path, json=json_body)
    except httpx.TimeoutException as exc:
        raise HomeAssistantError(HaErrorCode.TIMEOUT, "Home Assistant request timed out") from exc
    except httpx.HTTPError as exc:
        raise HomeAssistantError(HaErrorCode.UNREACHABLE, "Home Assistant is unreachable") from exc
    if resp.status_code in {401, 403}:
        raise HomeAssistantError(HaErrorCode.UNAUTHORIZED, "Home Assistant token was rejected", resp.status_code)
    if resp.status_code >= 400:
        raise HomeAssistantError(HaErrorCode.SERVICE_REJECTED, f"Home Assistant returned HTTP {resp.status_code}", resp.status_code)
    try:
        return resp.json()
    except ValueError as exc:
        raise HomeAssistantError(HaErrorCode.INVALID_JSON, "Home Assistant returned invalid JSON") from exc
```

- [ ] **Step 5: Run focused tests**

Run: `cd backend && uv run pytest miloco/tests/home_assistant/test_client.py -q`

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add backend/miloco/src/miloco/home_assistant backend/miloco/tests/home_assistant/test_client.py
git commit -m "feat: add home assistant rest client"
```

### Task 3: Unified device schemas and route helpers

**Files:**

- Create: `backend/miloco/src/miloco/devices/__init__.py`
- Create: `backend/miloco/src/miloco/devices/schema.py`
- Create: `backend/miloco/tests/devices/test_schema.py`

**Interfaces:**

- Produces `DeviceSource.MIOT = "miot"` and `DeviceSource.HOME_ASSISTANT = "home_assistant"`.
- Produces `UnifiedDeviceInfo`, `UnifiedSpecEntry`, `UnifiedSceneInfo`, `UnifiedDeviceControlRequest`, `UnifiedActionResult`.
- Produces `make_ha_device_id(instance_key: str, entity_id: str) -> str`.
- Produces `parse_ha_device_id(device_id: str) -> tuple[str, str] | None`.

- [ ] **Step 1: Write failing schema tests**

```python
from miloco.devices.schema import DeviceSource, make_ha_device_id, parse_ha_device_id


def test_ha_device_id_round_trips():
    did = make_ha_device_id("primary", "light.kitchen")
    assert did == "ha:primary:light.kitchen"
    assert parse_ha_device_id(did) == ("primary", "light.kitchen")


def test_miot_id_is_not_parsed_as_ha():
    assert parse_ha_device_id("123456789") is None


def test_source_values_are_stable():
    assert DeviceSource.MIOT.value == "miot"
    assert DeviceSource.HOME_ASSISTANT.value == "home_assistant"
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `cd backend && uv run pytest miloco/tests/devices/test_schema.py -q`

Expected: fails because `miloco.devices.schema` is missing.

- [ ] **Step 3: Implement source-aware models**

```python
class DeviceSource(str, Enum):
    MIOT = "miot"
    HOME_ASSISTANT = "home_assistant"


class UnifiedSpecEntry(BaseModel):
    iid: str
    type_name: str
    description: str = ""
    format: str = ""
    readable: bool = False
    writeable: bool = False
    executable: bool = False
    unit: str | None = None
    value_list: list[dict[str, object]] | None = None
    value_range: list[float] | None = None


class UnifiedDeviceInfo(BaseModel):
    did: str
    source: DeviceSource
    source_label: str
    name: str
    online: bool = False
    model: str | None = None
    room: str | None = None
    category: str | None = None
    spec: dict[str, UnifiedSpecEntry] = Field(default_factory=dict)
    included: bool = True
    control_enabled: bool = True
    read_only_reason: str | None = None
```

- [ ] **Step 4: Implement HA ID helpers**

```python
def make_ha_device_id(instance_key: str, entity_id: str) -> str:
    return f"ha:{instance_key}:{entity_id}"


def parse_ha_device_id(device_id: str) -> tuple[str, str] | None:
    parts = device_id.split(":", 2)
    if len(parts) != 3 or parts[0] != "ha" or not parts[1] or not parts[2]:
        return None
    return parts[1], parts[2]
```

- [ ] **Step 5: Run focused tests**

Run: `cd backend && uv run pytest miloco/tests/devices/test_schema.py -q`

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add backend/miloco/src/miloco/devices backend/miloco/tests/devices/test_schema.py
git commit -m "feat: add unified device schema"
```

### Task 4: HA mapper with import and control policy

**Files:**

- Create: `backend/miloco/src/miloco/home_assistant/mapper.py`
- Modify: `backend/miloco/src/miloco/home_assistant/schema.py`
- Create: `backend/miloco/tests/home_assistant/test_mapper.py`

**Interfaces:**

- Produces `HaEntityState(entity_id: str, state: str, attributes: dict[str, object])`.
- Produces `HaServiceCatalog = dict[str, set[str]]`.
- Produces `map_entity_to_device(entity: HaEntityState, services: HaServiceCatalog, policy: HomeAssistantEntityPolicy, instance_key: str) -> UnifiedDeviceInfo | None`.
- Produces `control_spec_to_service(entity_id: str, iid: str, value: object, services: HaServiceCatalog) -> HaServiceCall`.

- [ ] **Step 1: Write failing mapper tests**

```python
from miloco.config.settings import HomeAssistantEntityPolicy
from miloco.home_assistant.mapper import map_entity_to_device
from miloco.home_assistant.schema import HaEntityState


def test_discovered_entity_not_included_returns_none():
    entity = HaEntityState(entity_id="light.kitchen", state="off", attributes={"friendly_name": "厨房灯"})
    policy = HomeAssistantEntityPolicy(entity_id="light.kitchen", included=False)
    assert map_entity_to_device(entity, {"light": {"turn_on", "turn_off"}}, policy, "primary") is None


def test_imported_light_without_control_is_read_only():
    entity = HaEntityState(entity_id="light.kitchen", state="off", attributes={"friendly_name": "厨房灯"})
    policy = HomeAssistantEntityPolicy(entity_id="light.kitchen", included=True, control_enabled=False)
    device = map_entity_to_device(entity, {"light": {"turn_on", "turn_off"}}, policy, "primary")
    assert device is not None
    assert device.did == "ha:primary:light.kitchen"
    assert device.control_enabled is False
    assert all(not item.writeable and not item.executable for item in device.spec.values())


def test_imported_control_enabled_light_has_on_spec():
    entity = HaEntityState(entity_id="light.kitchen", state="off", attributes={"friendly_name": "厨房灯"})
    policy = HomeAssistantEntityPolicy(entity_id="light.kitchen", included=True, control_enabled=True)
    device = map_entity_to_device(entity, {"light": {"turn_on", "turn_off"}}, policy, "primary")
    assert device is not None
    assert device.spec["on"].writeable is True
```

- [ ] **Step 2: Run tests and confirm mapper failure**

Run: `cd backend && uv run pytest miloco/tests/home_assistant/test_mapper.py -q`

Expected: fails because mapper functions are missing.

- [ ] **Step 3: Implement entity DTO and service catalog parsing**

```python
class HaEntityState(BaseModel):
    entity_id: str
    state: str
    attributes: dict[str, object] = Field(default_factory=dict)


class HaServiceCall(BaseModel):
    domain: str
    service: str
    data: dict[str, object]
```

- [ ] **Step 4: Implement supported-domain spec synthesis**

```python
SUPPORTED_CONTROL_DOMAINS = {"switch", "light", "fan", "cover", "climate", "scene", "script"}
BLOCKED_CONTROL_DOMAINS = {"lock", "alarm_control_panel", "valve", "water_heater", "siren", "button"}


def _domain(entity_id: str) -> str:
    return entity_id.split(".", 1)[0]


def _base_device(entity: HaEntityState, policy: HomeAssistantEntityPolicy, instance_key: str) -> UnifiedDeviceInfo:
    domain = _domain(entity.entity_id)
    return UnifiedDeviceInfo(
        did=make_ha_device_id(instance_key, entity.entity_id),
        source=DeviceSource.HOME_ASSISTANT,
        source_label="Home Assistant",
        name=str(entity.attributes.get("friendly_name") or entity.entity_id),
        online=entity.state not in {"unavailable", "unknown"},
        model=f"home_assistant.{domain}",
        room=str(entity.attributes.get("area") or entity.attributes.get("room") or "未分配"),
        category=domain,
        included=policy.included,
        control_enabled=policy.control_enabled,
    )
```

- [ ] **Step 5: Gate writable specs by backend policy**

```python
def _strip_control_specs(device: UnifiedDeviceInfo, reason: str) -> UnifiedDeviceInfo:
    spec = {
        key: item.model_copy(update={"writeable": False, "executable": False})
        for key, item in device.spec.items()
    }
    return device.model_copy(update={"spec": spec, "control_enabled": False, "read_only_reason": reason})
```

- [ ] **Step 6: Run mapper tests**

Run: `cd backend && uv run pytest miloco/tests/home_assistant/test_mapper.py -q`

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add backend/miloco/src/miloco/home_assistant/schema.py backend/miloco/src/miloco/home_assistant/mapper.py backend/miloco/tests/home_assistant/test_mapper.py
git commit -m "feat: map home assistant entities to devices"
```

### Task 5: HA service and management router

**Files:**

- Create: `backend/miloco/src/miloco/home_assistant/service.py`
- Create: `backend/miloco/src/miloco/home_assistant/router.py`
- Modify: `backend/miloco/src/miloco/manager.py`
- Modify: `backend/miloco/src/miloco/main.py`
- Create: `backend/miloco/tests/home_assistant/test_router.py`

**Interfaces:**

- Produces `HomeAssistantService.status() -> HomeAssistantStatus`.
- Produces `HomeAssistantService.test_config(base_url: str, token: str, verify_tls: bool) -> HomeAssistantTestResult`.
- Produces `HomeAssistantService.save_config(update: HomeAssistantConfigUpdate) -> HomeAssistantPublicConfig`.
- Produces `HomeAssistantService.list_entities(refresh: bool = False) -> list[HomeAssistantEntityView]`.
- Produces `HomeAssistantService.update_entity_policy(entity_id: str, included: bool | None, control_enabled: bool | None) -> HomeAssistantEntityView`.
- Router uses existing `verify_token` dependency like other authenticated routers.

- [ ] **Step 1: Write failing router tests**

```python
from fastapi.testclient import TestClient

from miloco.main import app


def test_home_assistant_config_readback_masks_token(monkeypatch):
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-token"}
    response = client.post(
        "/api/home-assistant/config",
        headers=headers,
        json={"enabled": True, "base_url": "http://ha.local:8123", "token": "secret-token"},
    )
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["token_configured"] is True
    assert "secret-token" not in response.text


def test_control_policy_rejects_blocked_domain(monkeypatch):
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-token"}
    response = client.put(
        "/api/home-assistant/entities/lock.front_door/policy",
        headers=headers,
        json={"included": True, "control_enabled": True},
    )
    assert response.status_code in {200, 400}
    assert "secret-token" not in response.text
```

- [ ] **Step 2: Run tests and confirm route failure**

Run: `cd backend && MILOCO_SERVER__TOKEN=test-token uv run pytest miloco/tests/home_assistant/test_router.py -q`

Expected: fails because router is not registered.

- [ ] **Step 3: Implement public config DTOs**

```python
class HomeAssistantConfigUpdate(BaseModel):
    enabled: bool
    base_url: str
    token: str | None = None
    preserve_token: bool = False
    verify_tls: bool = True


class HomeAssistantPublicConfig(BaseModel):
    enabled: bool
    base_url: str
    instance_key: str
    token_configured: bool
    token_mask: str = "••••••••"
```

- [ ] **Step 4: Save config with existing atomic config helper**

```python
from miloco.config import get_settings, reset_settings
from miloco.utils.agent_config import update_shared_config


def save_config(self, update: HomeAssistantConfigUpdate) -> HomeAssistantPublicConfig:
    current = get_settings().home_assistant
    token = current.token if update.preserve_token else (update.token or "")
    update_shared_config(
        home_assistant={
            "enabled": update.enabled,
            "base_url": update.base_url,
            "token": token,
            "instance_key": current.instance_key,
            "verify_tls": update.verify_tls,
            "timeout_seconds": current.timeout_seconds,
            "state_cache_ttl_seconds": current.state_cache_ttl_seconds,
            "entities": {k: v.model_dump() for k, v in current.entities.items()},
        }
    )
    reset_settings()
    return self.public_config()
```

- [ ] **Step 5: Implement policy endpoint and blocked-domain guard**

```python
@router.put("/entities/{entity_id:path}/policy", response_model=NormalResponse)
async def update_entity_policy(entity_id: str, body: HomeAssistantEntityPolicyUpdate, current_user: str = Depends(verify_token)):
    view = manager.home_assistant_service.update_entity_policy(entity_id, body.included, body.control_enabled)
    return NormalResponse(data=view.model_dump())
```

- [ ] **Step 6: Register service and router**

```python
manager.home_assistant_service = HomeAssistantService()
app.include_router(home_assistant_router, prefix="/api/home-assistant")
```

- [ ] **Step 7: Run focused tests**

Run: `cd backend && MILOCO_SERVER__TOKEN=test-token uv run pytest miloco/tests/home_assistant/test_router.py -q`

Expected: pass.

- [ ] **Step 8: Commit**

```bash
git add backend/miloco/src/miloco/home_assistant/service.py backend/miloco/src/miloco/home_assistant/router.py backend/miloco/src/miloco/manager.py backend/miloco/src/miloco/main.py backend/miloco/tests/home_assistant/test_router.py
git commit -m "feat: add home assistant management api"
```

### Task 6: Unified device service and routes

**Files:**

- Create: `backend/miloco/src/miloco/devices/service.py`
- Create: `backend/miloco/src/miloco/devices/router.py`
- Modify: `backend/miloco/src/miloco/manager.py`
- Modify: `backend/miloco/src/miloco/main.py`
- Create: `backend/miloco/tests/devices/test_router.py`

**Interfaces:**

- Produces `DevicesService.home(refresh: bool = False) -> UnifiedHomeInfo`.
- Produces `DevicesService.get_spec(device_id: str) -> UnifiedDeviceInfo`.
- Produces `DevicesService.control(device_id: str, request: UnifiedDeviceControlRequest) -> UnifiedActionResult`.
- Produces `DevicesService.trigger_scene(scene_id: str) -> UnifiedActionResult`.

- [ ] **Step 1: Write failing mixed-source route tests**

```python
from fastapi.testclient import TestClient

from miloco.main import app


def test_devices_home_returns_unified_payload(monkeypatch):
    client = TestClient(app)
    response = client.get("/api/devices/home", headers={"Authorization": "Bearer test-token"})
    assert response.status_code == 200
    body = response.json()["data"]
    assert "devices" in body
    assert "scenes" in body
    assert "areas" in body


def test_ha_control_disabled_is_rejected(monkeypatch):
    client = TestClient(app)
    response = client.post(
        "/api/devices/ha%3Aprimary%3Alight.kitchen/control",
        headers={"Authorization": "Bearer test-token"},
        json={"type": "set_property", "iid": "on", "value": True},
    )
    assert response.status_code in {400, 404}
```

- [ ] **Step 2: Run tests and confirm missing route failure**

Run: `cd backend && MILOCO_SERVER__TOKEN=test-token uv run pytest miloco/tests/devices/test_router.py -q`

Expected: fails because `/api/devices/home` is missing.

- [ ] **Step 3: Wrap current MiOT home info without changing old routes**

```python
async def _miot_devices(self, refresh: bool = False) -> list[UnifiedDeviceInfo]:
    home = await self._miot_service.get_home_info(refresh=refresh)
    return [miot_device_to_unified(item) for item in home.devices]
```

- [ ] **Step 4: Merge HA imported entities**

```python
async def home(self, refresh: bool = False) -> UnifiedHomeInfo:
    miot_home = await self._miot_home(refresh=refresh)
    ha_devices = await self._ha_service.list_imported_devices(refresh=refresh)
    return UnifiedHomeInfo(
        home_name=miot_home.home_name,
        devices=[*miot_home.devices, *ha_devices],
        scenes=[*miot_home.scenes, *await self._ha_service.list_scenes()],
        areas=merge_areas(miot_home.areas, ha_devices),
    )
```

- [ ] **Step 5: Dispatch controls by parsed source**

```python
async def control(self, device_id: str, request: UnifiedDeviceControlRequest) -> UnifiedActionResult:
    parsed = parse_ha_device_id(device_id)
    if parsed is not None:
        return await self._ha_service.control(parsed[1], request)
    return await self._miot_service.control_device(device_id, request.to_miot_request())
```

- [ ] **Step 6: Register router**

```python
app.include_router(devices_router, prefix="/api/devices")
```

- [ ] **Step 7: Run route and MiOT compatibility tests**

Run: `cd backend && MILOCO_SERVER__TOKEN=test-token uv run pytest miloco/tests/devices/test_router.py miloco/tests/test_miot_filter_and_cameras.py -q`

Expected: pass.

- [ ] **Step 8: Commit**

```bash
git add backend/miloco/src/miloco/devices backend/miloco/src/miloco/manager.py backend/miloco/src/miloco/main.py backend/miloco/tests/devices/test_router.py
git commit -m "feat: add unified device api"
```

### Task 7: CLI and catalog migration

**Files:**

- Modify: `cli/src/miloco_cli/home_info.py`
- Modify: `cli/src/miloco_cli/catalog.py`
- Modify: `cli/src/miloco_cli/commands/device.py`
- Create: `cli/src/miloco_cli/commands/home_assistant.py`
- Modify: `cli/src/miloco_cli/main.py`
- Create: `cli/tests/test_device_home_assistant.py`
- Create: `cli/tests/test_home_assistant_commands.py`

**Interfaces:**

- `get_home_info(refresh: bool = False, timeout: float | None = None)` calls `/api/devices/home`.
- `miloco-cli home-assistant test --url <url> --token-stdin` reads the token from stdin.
- `miloco-cli home-assistant import <entity_id>`, `remove <entity_id>`, `enable-control <entity_id>`, and `disable-control <entity_id>` call the policy endpoint.

- [ ] **Step 1: Write failing CLI tests**

```python
from click.testing import CliRunner

from miloco_cli.main import main


def test_device_list_uses_unified_home(monkeypatch):
    calls = []

    def fake_api_get(path, timeout=None):
        calls.append(path)
        return {"code": 0, "data": {"home_name": "家", "devices": [], "scenes": [], "areas": []}}

    monkeypatch.setattr("miloco_cli.client.api_get", fake_api_get)
    result = CliRunner().invoke(main, ["device", "list"])
    assert result.exit_code == 0
    assert calls == ["/api/devices/home"]


def test_home_assistant_test_reads_token_from_stdin(monkeypatch):
    posted = {}

    def fake_api_post(path, data=None, timeout=None):
        posted["path"] = path
        posted["data"] = data
        return {"code": 0, "data": {"connected": True}}

    monkeypatch.setattr("miloco_cli.client.api_post", fake_api_post)
    result = CliRunner().invoke(
        main,
        ["home-assistant", "test", "--url", "http://ha.local:8123", "--token-stdin"],
        input="secret-token\n",
    )
    assert result.exit_code == 0
    assert posted["path"] == "/api/home-assistant/test"
    assert posted["data"]["token"] == "secret-token"
    assert "secret-token" not in result.output
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `cd cli && uv run pytest tests/test_device_home_assistant.py tests/test_home_assistant_commands.py -q`

Expected: fails because CLI still uses `/api/miot/home` and has no `home-assistant` command.

- [ ] **Step 3: Move home info fetch to unified endpoint**

```python
def _fetch(*, refresh: bool = False, timeout: float | None = None) -> dict:
    from miloco_cli.client import api_get

    path = "/api/devices/home"
    if refresh:
        path += "?refresh=true"
    resp = api_get(path, timeout=timeout)
    return resp.get("data", {})
```

- [ ] **Step 4: Route device commands through unified endpoints**

```python
resp = api_get(f"/api/devices/{quote(did, safe='')}/spec")
resp = api_post(f"/api/devices/{quote(did, safe='')}/control", data=payload)
```

- [ ] **Step 5: Add HA management command group**

```python
@click.group("home-assistant")
def home_assistant_group():
    """Home Assistant 接入：状态、测试、同步、导入与控制权限。"""


@home_assistant_group.command("enable-control")
@click.argument("entity_id")
def enable_control(entity_id):
    from miloco_cli.client import api_put

    print_result(api_put(f"/api/home-assistant/entities/{quote(entity_id, safe='')}/policy", data={"included": True, "control_enabled": True}))
```

- [ ] **Step 6: Update catalog rendering**

```text
# did|source|device_name|room|category|online|control
ha:primary:light.kitchen|home_assistant|厨房灯|厨房|light|online|read_only
```

- [ ] **Step 7: Run CLI tests**

Run: `cd cli && uv run pytest tests/test_home_info.py tests/test_catalog.py tests/test_device_home_assistant.py tests/test_home_assistant_commands.py -q`

Expected: pass.

- [ ] **Step 8: Commit**

```bash
git add cli/src/miloco_cli/home_info.py cli/src/miloco_cli/catalog.py cli/src/miloco_cli/commands/device.py cli/src/miloco_cli/commands/home_assistant.py cli/src/miloco_cli/main.py cli/tests/test_device_home_assistant.py cli/tests/test_home_assistant_commands.py
git commit -m "feat: add home assistant cli support"
```

### Task 8: Agent catalog and prompt safety

**Files:**

- Modify: `plugins/skills/miloco-devices/SKILL.md`
- Modify: `plugins/openclaw/src/services/catalog.ts`
- Modify: `plugins/openclaw/src/hooks/prompt.ts`
- Modify: `plugins/hermes/miloco-plugin/catalog.py`
- Modify: `plugins/hermes/miloco-plugin/context_injection.py`
- Modify: `plugins/hermes/tests/test_catalog.py`
- Modify: `plugins/hermes/tests/test_context_injection.py`
- Modify: `plugins/openclaw/tests/catalog.test.ts`
- Modify: `plugins/openclaw/tests/prompt.test.ts`

**Interfaces:**

- Catalog text includes a `source` column.
- Agent instructions say to use `miloco-cli device list/spec/control/props/action`.
- Agent instructions forbid raw HA service calls and invented HA entity IDs.

- [ ] **Step 1: Write failing prompt/catalog tests**

```python
from pathlib import Path


def test_miloco_device_skill_mentions_home_assistant():
    text = Path("plugins/skills/miloco-devices/SKILL.md").read_text(encoding="utf-8")
    assert "Home Assistant" in text
    assert "arbitrary HA service" in text
```

```typescript
import { describe, expect, it } from "vitest";
import { buildMilocoCatalogPrompt } from "../src/hooks/prompt";

describe("Miloco HA catalog prompt", () => {
  it("forbids raw Home Assistant service calls", () => {
    const prompt = buildMilocoCatalogPrompt("ha:primary:light.kitchen|home_assistant|厨房灯");
    expect(prompt).toContain("do not call raw Home Assistant services");
  });
});
```

- [ ] **Step 2: Run plugin tests and confirm failure**

Run: `uv run --with pytest --with httpx python -m pytest plugins/hermes/tests/ -q`

Run: `cd plugins/openclaw && pnpm test`

Expected: fails on missing HA-aware text or source parsing.

- [ ] **Step 3: Update skill wording**

```markdown
# Miloco 智能家居设备（米家 + Home Assistant）

Use `miloco-cli device list`, `miloco-cli device spec`, and `miloco-cli device control`.
Do not invent Home Assistant entity IDs.
Do not call raw Home Assistant domain/service names.
Read-only HA devices may be described to the user but must not be controlled.
```

- [ ] **Step 4: Update OpenClaw and Hermes catalog parsing**

```typescript
type CatalogRow = {
  did: string;
  source: "miot" | "home_assistant";
  name: string;
  room: string;
  category: string;
  online: boolean;
  control: "enabled" | "read_only";
};
```

- [ ] **Step 5: Run plugin tests**

Run: `uv run --with pytest --with httpx python -m pytest plugins/hermes/tests/ -q`

Run: `cd plugins/openclaw && pnpm test`

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add plugins/skills/miloco-devices/SKILL.md plugins/openclaw/src/services/catalog.ts plugins/openclaw/src/hooks/prompt.ts plugins/hermes/miloco-plugin/catalog.py plugins/hermes/miloco-plugin/context_injection.py plugins/hermes/tests plugins/openclaw
git commit -m "feat: teach agent catalog about home assistant"
```

### Task 9: Web API, sidebar tab, and Home Assistant page

**Files:**

- Modify: `web/src/lib/types.ts`
- Create: `web/src/lib/homeAssistant.ts`
- Modify: `web/src/api/index.ts`
- Modify: `web/src/api/real.ts`
- Modify: `web/src/components/Sidebar.tsx`
- Modify: `web/src/App.tsx`
- Create: `web/src/components/HomeAssistantPage.tsx`
- Modify: `web/src/components/DevicesByRoom.tsx`
- Modify: `web/src/i18n/locales/zh/nav.json`
- Modify: `web/src/i18n/locales/en/nav.json`
- Create: `web/src/i18n/locales/zh/homeAssistant.json`
- Create: `web/src/i18n/locales/en/homeAssistant.json`
- Modify: `web/src/i18n/locales/zh/devices.json`
- Modify: `web/src/i18n/locales/en/devices.json`
- Create: `web/tests/homeAssistant.test.ts`
- Create: `web/tests/devices-source.test.ts`

**Interfaces:**

- Produces frontend `HomeAssistantStatus`, `HomeAssistantEntity`, `HomeAssistantPublicConfig`, `HomeAssistantConfigUpdate`, and `HomeAssistantEntityPolicyUpdate`.
- Produces `maskHomeAssistantToken(tokenConfigured: boolean) -> string`.
- Produces `controlDisabledReason(entity: HomeAssistantEntity) -> string | null`.
- Adds `TabKey` member `homeAssistant`.
- Adds API functions `getHomeAssistantStatus`, `getHomeAssistantConfig`, `testHomeAssistantConfig`, `saveHomeAssistantConfig`, `refreshHomeAssistantEntities`, `listHomeAssistantEntities`, `updateHomeAssistantEntityPolicy`.

- [ ] **Step 1: Write failing node-safe web helper tests**

```typescript
import { describe, expect, it } from "vitest";
import { controlDisabledReason, maskHomeAssistantToken } from "@/lib/homeAssistant";
import type { HomeAssistantEntity } from "@/lib/types";

function entity(patch: Partial<HomeAssistantEntity>): HomeAssistantEntity {
  return {
    entityId: "light.kitchen",
    name: "厨房灯",
    domain: "light",
    state: "off",
    included: true,
    controlEnabled: false,
    controlSupported: true,
    controlBlockedReason: null,
    lastSeenAt: null,
    lastControlAt: null,
    lastError: null,
    ...patch,
  };
}

describe("Home Assistant helpers", () => {
  it("masks a configured token", () => {
    expect(maskHomeAssistantToken(true)).toBe("••••••••");
  });

  it("explains blocked control toggle", () => {
    expect(controlDisabledReason(entity({ controlSupported: false, controlBlockedReason: "blocked-risk" }))).toBe("blocked-risk");
  });
});
```

- [ ] **Step 2: Write failing API mapping test**

```typescript
import { describe, expect, it, vi, afterEach } from "vitest";
import { realListHomeAssistantEntities } from "@/api/real";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

it("maps snake_case HA entity fields to camelCase", async () => {
  globalThis.fetch = vi.fn(async () =>
    new Response(JSON.stringify({ code: 0, message: "ok", data: [{
      entity_id: "light.kitchen",
      friendly_name: "厨房灯",
      domain: "light",
      state: "off",
      included: true,
      control_enabled: false,
      control_supported: true,
      control_blocked_reason: null,
      last_seen_at: null,
      last_control_at: null,
      last_error: null,
    }] }), { status: 200, headers: { "Content-Type": "application/json" } })
  ) as unknown as typeof fetch;
  const rows = await realListHomeAssistantEntities();
  expect(rows[0].entityId).toBe("light.kitchen");
  expect(rows[0].controlEnabled).toBe(false);
});
```

- [ ] **Step 3: Run web tests and confirm failure**

Run: `cd web && pnpm test -- homeAssistant.test.ts devices-source.test.ts`

Expected: fails because HA types/helpers/API functions are missing.

- [ ] **Step 4: Add frontend DTOs and helpers**

```typescript
export interface HomeAssistantEntity {
  entityId: string;
  name: string;
  domain: string;
  state: string;
  included: boolean;
  controlEnabled: boolean;
  controlSupported: boolean;
  controlBlockedReason: string | null;
  lastSeenAt: number | null;
  lastControlAt: number | null;
  lastError: string | null;
}

export function maskHomeAssistantToken(tokenConfigured: boolean): string {
  return tokenConfigured ? "••••••••" : "";
}
```

- [ ] **Step 5: Add real API functions**

```typescript
export async function realListHomeAssistantEntities(): Promise<HomeAssistantEntity[]> {
  const res = await apiFetch<Normal<BackendHomeAssistantEntity[]>>("/api/home-assistant/entities");
  return res.data.map(mapHomeAssistantEntity);
}
```

- [ ] **Step 6: Add sidebar tab**

```typescript
export type TabKey =
  | "now"
  | "devices"
  | "homeAssistant"
  | "family"
  | "tasks"
  | "activity"
  | "usage";
```

- [ ] **Step 7: Render Home Assistant page in `App.tsx`**

```tsx
{activeTab === "homeAssistant" && (
  <HomeAssistantPage
    status={haStatus.data}
    entities={haEntities.data ? haEntities.data : []}
    onRefresh={() => haEntities.reload()}
  />
)}
```

- [ ] **Step 8: Add source badge and read-only display in Devices tab**

```tsx
<span className="text-caption border border-border rounded px-1.5 py-0.5">
  {device.sourceLabel}
</span>
```

- [ ] **Step 9: Run web tests and build**

Run: `cd web && pnpm test -- homeAssistant.test.ts devices-source.test.ts`

Run: `cd web && pnpm build`

Expected: pass.

- [ ] **Step 10: Commit**

```bash
git add web/src/lib/types.ts web/src/lib/homeAssistant.ts web/src/api/index.ts web/src/api/real.ts web/src/components/Sidebar.tsx web/src/App.tsx web/src/components/HomeAssistantPage.tsx web/src/components/DevicesByRoom.tsx web/src/i18n web/tests/homeAssistant.test.ts web/tests/devices-source.test.ts
git commit -m "feat: add home assistant web management"
```

### Task 10: STATIC rule dispatch through unified device service

**Files:**

- Modify: `backend/miloco/src/miloco/rule/schema.py`
- Modify: `backend/miloco/src/miloco/rule/service.py`
- Modify: `backend/miloco/src/miloco/rule/runner.py`
- Create: `backend/miloco/tests/test_rule_home_assistant.py`

**Interfaces:**

- Extends `RuleAction` with `source: Literal["miot", "home_assistant"] | None = None`.
- Existing actions without `source` remain MiOT actions.
- HA actions use `did="ha:primary:<entity_id>"` and `iid` equal to a normalized HA spec name such as `on`, `brightness_pct`, or `trigger`.
- Runner accepts an injected `devices_service` and calls it for HA actions.

- [ ] **Step 1: Write failing rule tests**

```python
import pytest

from miloco.rule.schema import RuleAction


def test_old_rule_action_defaults_to_miot():
    action = RuleAction(did="123", iid="prop.2.1", value=True)
    assert action.source in {None, "miot"}


def test_ha_rule_action_shape_is_accepted():
    action = RuleAction(source="home_assistant", did="ha:primary:light.kitchen", iid="on", value=True)
    assert action.did == "ha:primary:light.kitchen"
    assert action.iid == "on"
```

```python
@pytest.mark.asyncio
async def test_ha_rule_action_uses_devices_service(fake_rule_runner):
    result = await fake_rule_runner.execute_action(
        RuleAction(source="home_assistant", did="ha:primary:light.kitchen", iid="on", value=True)
    )
    assert result.success is True
    assert fake_rule_runner.devices_calls == [("ha:primary:light.kitchen", "on", True)]
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `cd backend && uv run pytest miloco/tests/test_rule_home_assistant.py -q`

Expected: fails because `RuleAction.source` and HA dispatch are missing.

- [ ] **Step 3: Extend schema without breaking old JSON**

```python
class RuleAction(BaseModel):
    source: Literal["miot", "home_assistant"] | None = None
    did: str = Field(..., description="Device ID; scene_id when iid is 'scene'")
    iid: str = Field(...)
```

- [ ] **Step 4: Validate HA actions in service layer**

```python
if action.source == "home_assistant":
    spec = await devices_service.get_spec(action.did)
    if action.iid not in spec.spec:
        raise ValidationException(f"HA spec {action.iid!r} is not available for {action.did}")
    entry = spec.spec[action.iid]
    if not entry.writeable and not entry.executable:
        raise ValidationException("HA action is not controllable")
```

- [ ] **Step 5: Dispatch HA through unified device service in runner**

```python
if action.source == "home_assistant" or action.did.startswith("ha:"):
    request = UnifiedDeviceControlRequest(type="set_property", iid=action.iid, value=action.value, params=action.params)
    return await self._devices_service.control(action.did, request)
```

- [ ] **Step 6: Preserve non-idempotent cooldown rules**

```python
if action.source == "home_assistant" and action.iid == "trigger" and action.cooldown_minutes is None:
    raise ValidationException("HA scene/script trigger requires cooldown_minutes")
```

- [ ] **Step 7: Run rule and compatibility tests**

Run: `cd backend && uv run pytest miloco/tests/test_rule_home_assistant.py miloco/tests/test_rule.py miloco/tests/test_perception_rule_e2e.py -q`

Expected: pass.

- [ ] **Step 8: Commit**

```bash
git add backend/miloco/src/miloco/rule/schema.py backend/miloco/src/miloco/rule/service.py backend/miloco/src/miloco/rule/runner.py backend/miloco/tests/test_rule_home_assistant.py
git commit -m "feat: route home assistant rule actions"
```

### Task 11: Documentation, packaging, and local CI

**Files:**

- Modify: `README.md`
- Modify: `README.zh.md`
- Modify: `user_guide.md`
- Modify: `user_guide_zh.md`
- Modify: `knowledge/03-features/device-control.md`
- Modify: `knowledge/06-dev-guide/troubleshooting.md`

**Interfaces:**

- Docs must explain HA long-lived token creation without storing or printing a real token.
- Docs must explain import versus control permission.
- Docs must list supported, read-only, and blocked domains.

- [ ] **Step 1: Add doc/package check**

```bash
rg -n "Home Assistant|control_enabled|ha_control_disabled" README.zh.md user_guide_zh.md knowledge/03-features/device-control.md
```

Expected before docs: command exits non-zero or finds incomplete coverage.

- [ ] **Step 2: Document user setup**

```markdown
### Home Assistant 接入

在 Home Assistant 中创建长期访问令牌后，到 Miloco 左侧 `Home Assistant` 页填写 Base URL 和令牌。
保存后 Miloco 只显示令牌已配置状态，不回显令牌明文。
```

- [ ] **Step 3: Document import and control permission**

```markdown
发现到的实体默认不导入；导入后的实体默认只读。
只有在 Home Assistant 页为某个实体打开“允许控制”后，Miloco 才会向 CLI、Agent 目录和规则引擎暴露可写或可执行规格。
```

- [ ] **Step 4: Run full local CI**

Run: `./scripts/local-ci.sh --tests`

Expected: pass, with only documented platform-specific skips.

- [ ] **Step 5: Build release artifacts**

Run: `./scripts/build.sh`

Expected: generated artifacts include backend `miloco/home_assistant`, backend `miloco/devices`, CLI `home_assistant` command, and web static assets with HA page strings.

- [ ] **Step 6: Commit**

```bash
git add README.md README.zh.md user_guide.md user_guide_zh.md knowledge/03-features/device-control.md knowledge/06-dev-guide/troubleshooting.md
git commit -m "docs: document home assistant integration"
```

### Task 12: Lab and production rollout preparation

**Files:**

- Modify: `deploy.sh`
- Create or update: `docs/2026-08-30-home-assistant-control_PROGRESS.md`
- Create: `docs/ops/2026-08-30-home-assistant-control-implementation.md`
- Create: `docs/ops/2026-08-30-home-assistant-control-rollback.md`

**Interfaces:**

- Lab validation uses `ai-lab01.esxi` and `ai-lab02.esxi` before production when a HA test endpoint is available.
- Production rollout to `miloco.esxi` is gated by an approved CO/PAM and exact release SHA.
- Rollback disables `home_assistant.enabled` first, then rolls back binary release when MiOT or core app behavior regresses.

- [ ] **Step 1: Write rollout checklist**

```markdown
## Rollout checklist

- Release SHA:
- Build artifact digest:
- Lab host:
- HA test endpoint class:
- MiOT compatibility smoke:
- HA config smoke:
- HA import and control smoke:
- Browser smoke:
- Rollback trigger:
```

- [ ] **Step 2: Add `miloco.esxi` to deployment controller**

```bash
readonly ALLOWED_HOST_4="miloco.esxi"
```

```bash
case "$1" in
    "$ALLOWED_HOST_1"|"$ALLOWED_HOST_2"|"$ALLOWED_HOST_3"|"$ALLOWED_HOST_4") ;;
    *) die 2 "host is not an approved Miloco deployment target" ;;
esac
```

```bash
"$ALLOWED_HOST_4")
    printf '%s\n' "/opt/miloco"
    ;;
```

- [ ] **Step 3: Run lab preflight on approved lab hosts**

Run: `MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw ./deploy.sh preflight --host ai-lab01.esxi`

Run: `MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw ./deploy.sh preflight --host ai-lab02.esxi`

Expected: both preflights pass, or the progress doc records the exact blocking prerequisite.

- [ ] **Step 4: Deploy to lab and verify**

Run: `MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw ./deploy.sh deploy --host ai-lab01.esxi`

Run: `MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw ./deploy.sh verify --host ai-lab01.esxi`

Expected: verify reports healthy backend and web. Repeat on `ai-lab02.esxi` when the first lab result is clean.

- [ ] **Step 5: Prepare production CO text**

```markdown
Change summary: deploy Miloco release <SHA> to miloco.esxi with Home Assistant integration.
Risk: smart-home control path expands from MiOT-only to MiOT + HA behind explicit per-entity control permission.
Validation: local CI, build artifact digest, lab smoke, browser smoke, MiOT compatibility smoke.
Rollback: set home_assistant.enabled=false, restart Miloco, verify MiOT, then restore previous release SHA when the config-only rollback does not recover service behavior.
```

- [ ] **Step 6: Production deploy after explicit approval**

Run only after approved CO/PAM: `./deploy.sh deploy --host miloco.esxi`

Expected: Miloco starts from the approved release SHA; `/api/devices/home` returns MiOT devices and imported HA devices; `/api/home-assistant/status` returns redacted status.

- [ ] **Step 7: Production browser acceptance**

Open Miloco UI and verify:

- Home Assistant tab appears in left sidebar and mobile tab bar.
- Token save/readback shows masked token only.
- Discovered HA entities can be imported.
- Imported HA entities are read-only until control is enabled.
- Enabling control exposes mapped controls and Agent catalog specs.
- Xiaomi device list and controls still work.

- [ ] **Step 8: Commit rollout docs**

```bash
git add docs/2026-08-30-home-assistant-control_PROGRESS.md docs/ops/2026-08-30-home-assistant-control-implementation.md docs/ops/2026-08-30-home-assistant-control-rollback.md deploy.sh
git commit -m "docs: plan home assistant rollout"
```

## Test Plan

### Backend

- `cd backend && MILOCO_CONFIG_SEARCH_PATH=/tmp/miloco-ha-plan-empty MILOCO_SERVER__TOKEN='' uv run pytest miloco/tests/config/test_home_assistant_settings.py -q`
- `cd backend && uv run pytest miloco/tests/home_assistant/ miloco/tests/devices/ -q`
- `cd backend && uv run pytest miloco/tests/test_rule_home_assistant.py miloco/tests/test_rule.py miloco/tests/test_perception_rule_e2e.py -q`

### CLI

- `cd cli && uv run pytest tests/test_home_info.py tests/test_catalog.py tests/test_device_home_assistant.py tests/test_home_assistant_commands.py -q`

### Web

- `cd web && pnpm test -- homeAssistant.test.ts devices-source.test.ts`
- `cd web && pnpm build`

### Plugins

- `uv run --with pytest --with httpx python -m pytest plugins/hermes/tests/ -q`
- `cd plugins/openclaw && pnpm test`

### Full local gate

- `./scripts/local-ci.sh --tests`
- `./scripts/build.sh`

### Manual acceptance

1. Fake HA endpoint:
   - configure fake HA URL/token
   - refresh discovery
   - import `light.kitchen`
   - confirm it appears read-only in Devices and catalog
   - enable control
   - call `on=true`
   - confirm fake HA received `POST /api/services/light/turn_on`
2. MiOT regression:
   - Xiaomi account remains connected
   - Xiaomi device list still renders
   - one safe Xiaomi control still routes through MiOT
3. Browser UI:
   - left sidebar has `Home Assistant`
   - HA tab manages connection and entities
   - Devices tab shows source badges
   - token is never visible after save
4. Agent catalog:
   - imported read-only HA entity has no writable/action spec
   - control-enabled HA light shows the `on` spec
   - prompt says raw HA service calls are forbidden
5. Production:
   - only after approved CO/PAM
   - exact deployed SHA recorded
   - production smoke recorded in progress doc

## Rollback Plan

- First rollback lever: set `home_assistant.enabled=false` in `$MILOCO_HOME/config.json`, restart Miloco, and verify MiOT behavior.
- Second rollback lever: set every HA entity `control_enabled=false`, restart Miloco, and verify Agent catalog has no HA writable/action specs.
- Third rollback lever: restore previous release SHA through the governed deployment controller and verify `/api/miot/home`, web login/session, and MiOT controls.
- Token retirement: remove `home_assistant.token` from `$MILOCO_HOME/config.json` and restart Miloco. Do not copy the token into rollback docs or chat.

## Post-MVP Notes

- Add `/api/websocket` state subscription only after MVP control, import policy, and Agent catalog behavior are accepted.
- Consider multiple HA instances after `primary` is proven in production.
- Consider HA camera snapshot/stream support as a separate feature because it intersects the RTSP/perception pipeline rather than ordinary device control.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-30-home-assistant-control.md`. Two execution options:

1. Subagent-Driven (recommended) - dispatch a fresh subagent per task, review between tasks, fast iteration.
2. Inline Execution - execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
