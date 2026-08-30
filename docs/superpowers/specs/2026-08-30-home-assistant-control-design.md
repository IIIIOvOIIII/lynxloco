# Home Assistant Control Integration Design

Status: written specification for user review on 2026-08-30.

## Goal

Miloco should support Home Assistant as a second smart-home control source beside Xiaomi MiOT. A user can connect one Home Assistant instance with a long-lived access token, review HA entities in a dedicated left-sidebar tab, choose which HA entities are imported into Miloco, and enable or disable control permission for imported HA devices.

The result should feel native in Miloco:

- Xiaomi devices continue to work exactly as they do today.
- HA devices have their own management surface.
- Imported HA devices can appear in the normal device list, Agent device catalog, and rule automation flow.
- HA control is never granted merely because the token is configured; the user must enable control per device.

## Background

Current Miloco control is MiOT-centered:

- Backend device/home aggregation is exposed primarily through `/api/miot/home`.
- CLI device commands read MiOT home info and call `/api/miot/devices/{did}/...`.
- OpenClaw/Hermes device catalog injection runs `miloco-cli device catalog`, so Agent visibility depends on CLI visibility.
- STATIC rule actions call MiOT proxy directly.
- The frontend Devices page calls `listDevices()`, which currently maps MiOT devices and MiOT status into the web `Device` model.

Therefore HA cannot be added as a small isolated API button. If HA is added only to the frontend or only as a backend side route, Agent control, CLI fallback, rule automation, audit, and UI state will drift. The design must introduce HA as a first-class device source and route user-facing control through a unified Miloco device layer.

Home Assistant API facts used by this design:

- The REST API is served under `/api/` on the same host/port as Home Assistant.
- Requests use `Authorization: Bearer <long_lived_access_token>`.
- `/api/states` exposes entity state.
- `/api/services` exposes available services.
- Control calls use `POST /api/services/<domain>/<service>`.
- `/api/websocket` supports an auth phase and event subscription; it is useful for later live state sync, but not required for the MVP.

References:

- <https://developers.home-assistant.io/docs/api/rest/>
- <https://developers.home-assistant.io/docs/api/websocket/>

## User-facing Requirements

### 1. Dedicated Home Assistant left-sidebar tab

Add a new desktop and mobile navigation tab dedicated to Home Assistant management.

Recommended key and labels:

- `TabKey`: `homeAssistant`
- Chinese label: `Home Assistant`
- Chinese hint: `接入并管理 HA 设备`
- English label: `Home Assistant`
- English hint: `Connect and manage HA devices`

This tab is separate from the existing `设备` tab. The distinction is intentional:

- `Home Assistant` tab manages the HA integration itself: connection, sync, import, per-device permissions.
- `设备` tab remains the user’s normal “home devices” surface and should show imported MiOT + HA devices together after the unified device layer is implemented.

### 2. HA connection lifecycle

The Home Assistant tab should show a connection card with:

- Base URL input.
- Long-lived access token input.
- Test connection button.
- Save button.
- Connected / disconnected / unauthorized / unreachable state.
- HA instance metadata when available, such as location name, version, unit system, and entity count.
- Last sync time and refresh button.

After saving, the raw token must not be returned to the browser. The UI may show only:

- `tokenConfigured=true`
- masked token state such as `••••••••`
- “replace token” affordance
- “preserve existing token” behavior when editing other fields

### 3. HA entity management lifecycle

The Home Assistant tab should show discovered HA entities after a successful connection or refresh.

Each row/card should show:

- Friendly name.
- `entity_id`.
- Domain/category.
- Current state summary.
- Room/area best effort.
- Import state.
- Control permission state.
- Safety/support status.
- Last error if a control attempt failed.

Entity states:

| State | Meaning | Where visible |
| --- | --- | --- |
| Discovered | Seen from HA `/api/states`; not imported into Miloco | HA tab only |
| Imported read-only | Included in Miloco as a readable device; no write/action capability | HA tab, Devices tab, Agent catalog as read-only |
| Imported control-enabled | Included in Miloco and allowed to expose safe write/action capabilities | HA tab, Devices tab, Agent catalog, CLI, rules |
| Blocked-risk | Known dangerous or unsupported for control | HA tab; may be imported read-only |

Default behavior:

- Newly discovered HA entities are not imported automatically.
- When a user imports an entity, it defaults to read-only.
- The user can enable control permission per imported device when the device domain is supported and not blocked by safety policy.
- Disabling control permission immediately removes writable/action specs from Agent catalog and rejects control calls server-side.
- Removing an entity from Miloco hides it from the normal Devices tab and Agent catalog but does not delete anything in Home Assistant.

The UI may provide filters and bulk actions, but per-device control permission is the required MVP behavior.

### 4. Device-level control permission

Control permission is Miloco-side authorization, independent of Home Assistant’s own token permission. It must be enforced on the backend, not only in the frontend.

For each imported HA entity:

- `included`: whether the entity is imported into Miloco.
- `control_enabled`: whether Miloco may perform write/action operations.
- `control_blocked_reason`: optional reason why the toggle is disabled.
- `last_seen_at`: last discovery timestamp.
- `last_control_at`: last successful or attempted control timestamp.

When `control_enabled=false`:

- UI must not show direct write controls.
- CLI control/action calls must fail with a stable error such as `ha_control_disabled`.
- Rule STATIC actions must fail validation before runtime.
- Agent catalog must either expose read-only properties only or annotate that the device is read-only; it must not expose usable `w` or `x` specs.

When `control_enabled=true`:

- Only Miloco-mapped safe specs may be called.
- The Agent still cannot call arbitrary HA services.
- Safety-gated domains remain blocked unless a later design explicitly allows them.

### 5. Normal Devices tab behavior

The existing `设备` tab should become a unified home-device view after HA is imported:

- Xiaomi devices show as before.
- Imported HA devices appear alongside Xiaomi devices.
- Every device card has a source badge:
  - `米家`
  - `Home Assistant`
- Read-only HA devices show current status but no control toggle.
- Control-enabled HA devices show only mapped safe controls.
- A HA device whose state cannot be refreshed should degrade to a clear unavailable/offline state without breaking MiOT devices.

The `Home Assistant` tab is still the source of truth for enabling, disabling, importing, and permission management.

## Implementation Boundary

### In scope

- One Home Assistant instance in MVP, keyed as `primary`.
- Connection through user-provided long-lived access token.
- Dedicated HA left-sidebar tab.
- HA entity discovery and refresh.
- Per-entity import into Miloco.
- Per-imported-entity control permission toggle.
- Unified backend device API for MiOT + HA.
- CLI support through existing `miloco-cli device ...` commands.
- Agent catalog support through existing catalog injection path.
- Rule automation support for HA actions only when `control_enabled=true`.
- Secret-safe logs, responses, tests, and docs.

### Out of scope for MVP

- Multiple HA instances in the UI.
- Home Assistant OAuth app flow.
- Running Miloco as a Home Assistant add-on.
- Creating, editing, or deleting HA automations.
- Raw HA API passthrough from Agent, CLI, or frontend.
- HA camera ingestion into Miloco perception.
- Replacing or changing existing MiOT OAuth, MiOT scope, or MiOT camera behavior.
- Allowing high-risk HA domains to be controlled by default.

## Supported Home Assistant Domains

### Control-supported MVP domains

| HA domain | Miloco category | Supported controls |
| --- | --- | --- |
| `switch` | `switch` | `on` |
| `light` | `light` | `on`; brightness/color temperature only when safely derivable |
| `fan` | `fan` | `on`; percentage/preset mode only when safely derivable |
| `cover` | `curtain` / `cover` | open, close, stop; position only when safe and supported |
| `climate` | `aircond` / `climate` | target temperature, HVAC mode |
| `scene` | `scene` | trigger existing scene |
| `script` | `script` | trigger existing script, explicit intent only |

### Read-only MVP domains

| HA domain | Behavior |
| --- | --- |
| `sensor` | read state and unit |
| `binary_sensor` | read state and device class |
| `weather` | read state and selected attributes |
| `media_player` | read state only in MVP |
| `device_tracker` | read state only if imported |
| unknown domains | read-only fallback if imported |

### Blocked or later-design domains

These domains should not be controllable in MVP:

- `lock`
- `alarm_control_panel`
- `valve`
- `water_heater`
- `siren`
- `button`
- garage-door-like covers or other access-control covers
- any domain/service classified as destructive or safety-critical

They may be discovered and optionally imported as read-only if useful.

## Architecture

```text
Home Assistant REST API
        ↓
miloco.home_assistant
  - client
  - mapper
  - service
  - repo
  - router
        ↓
miloco.devices unified layer
  - MiOT adapter
  - HA adapter
  - source-aware control executor
        ↓
Consumers
  - Web Devices tab
  - Web Home Assistant tab
  - miloco-cli device commands
  - OpenClaw/Hermes Agent catalog
  - STATIC rule runner
  - action ledger / observability
```

### New backend package

Add `miloco/home_assistant/`:

- `client.py`: HA REST client using `httpx.AsyncClient`.
- `schema.py`: request/response models, connection status, entity scope models.
- `mapper.py`: maps HA states/services to Miloco normalized devices/specs.
- `repo.py`: persists per-entity import/control permission state.
- `service.py`: orchestrates config, discovery, cache, control, health.
- `router.py`: exposes HA management endpoints.

### New unified device package

Add `miloco/devices/`:

- `schema.py`: provider-neutral device/spec/control/scene models.
- `service.py`: merges MiOT + HA devices and routes control.
- `router.py`: exposes provider-neutral APIs.

Existing `/api/miot/*` APIs stay intact.

Recommended new APIs:

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
- `PUT /api/home-assistant/config`
- `POST /api/home-assistant/test`
- `POST /api/home-assistant/refresh`
- `GET /api/home-assistant/entities`
- `PUT /api/home-assistant/entities/{entity_id}/scope`
- `PUT /api/home-assistant/entities/scope`

Because HA entity IDs contain dots and may appear inside path segments, frontend and CLI must URL-encode them. The backend should decode and validate them once at the router boundary.

## Data Model

### HA settings

Store endpoint-level HA configuration in `$MILOCO_HOME/config.json` through existing Miloco settings precedence:

```json
{
  "home_assistant": {
    "enabled": true,
    "instance_key": "primary",
    "base_url": "http://homeassistant.local:8123",
    "token": "<long-lived-token>",
    "verify_tls": true,
    "timeout_seconds": 10,
    "state_cache_ttl_seconds": 5
  }
}
```

Rules:

- `base_url` accepts only `http` and `https`.
- host is required.
- URL fragment is rejected.
- trailing slash is normalized.
- token is write-only in web readback.
- pydantic settings for this block must hide input in validation errors.

### HA entity scope

Persist mutable entity management state in SQLite, not in `config.json`, because it changes frequently and may contain many rows.

Recommended table:

```text
ha_entity_scope
  instance_key TEXT NOT NULL
  entity_id TEXT NOT NULL
  included INTEGER NOT NULL DEFAULT 0
  control_enabled INTEGER NOT NULL DEFAULT 0
  display_name_override TEXT NULL
  room_override TEXT NULL
  last_seen_at INTEGER NULL
  last_control_at INTEGER NULL
  updated_at INTEGER NOT NULL
  PRIMARY KEY(instance_key, entity_id)
```

The service may also store a lightweight discovery cache, but cache is not authority. Home Assistant remains source of truth for entity state; Miloco is source of truth only for inclusion and control permission.

### Unified device ID

Keep MiOT IDs unchanged.

Use source-aware HA IDs:

```text
ha:<instance_key>:<entity_id>
ha:primary:light.kitchen
ha:primary:climate.living_room_ac
```

Rules:

- Existing MiOT dids remain valid and route to MiOT.
- IDs starting with `ha:` route to HA.
- HA ID parsing must be strict.
- Invalid source prefixes are rejected.
- The ID must be encoded when placed in URL path segments.

## Capability Mapping

HA does not have a MIoT-style spec. Miloco synthesizes provider-neutral specs.

Examples:

```text
on|wr|bool
brightness|wr|uint8|[0,255]
brightness_pct|w|uint8|[1,100]
target-temperature|wr|float|[min,max;step]|celsius
hvac-mode|wr|string|Off=off,Cool=cool,Heat=heat,...
open|x
close|x
stop|x
trigger|x
```

Mapping rules:

- When `included=false`, the entity is not exposed outside the HA tab.
- When `included=true` and `control_enabled=false`, emit read-only specs only.
- When `included=true` and `control_enabled=true`, emit safe writable/action specs for supported domains.
- Unknown domains emit read-only `state` and selected attributes only.
- `scene` and `script` actions are non-idempotent.
- Device/domain service mapping is static allowlist code, not derived from arbitrary LLM text.

## Control Flow

### UI direct control

```text
User toggles HA light in Devices tab
  → web calls POST /api/devices/{encoded_ha_id}/control
  → unified device service parses source
  → HA adapter checks included/control_enabled/domain allowlist
  → HomeAssistantService maps "on=true" to light.turn_on
  → POST HA /api/services/light/turn_on
  → Miloco records action ledger without token
  → UI refreshes unified device state
```

### Agent control

```text
Agent receives "打开厨房灯"
  → miloco-devices skill
  → device catalog contains imported HA device only if in scope
  → Agent calls miloco-cli device control ha:primary:light.kitchen on true
  → CLI calls unified /api/devices/{id}/control
  → backend enforces control_enabled and allowlist
```

Agent must not call HA APIs directly.

### Rule automation

Old MiOT actions remain valid:

```json
{"did": "123456", "iid": "prop.2.1", "value": true}
```

New HA actions may use the same external shape with HA IDs and synthesized spec names:

```json
{"did": "ha:primary:light.kitchen", "iid": "on", "value": true}
```

Rule execution rules:

- If `did` starts with `ha:`, route through the unified device executor.
- If `did` does not start with `ha:`, preserve existing MiOT behavior.
- Rule creation/update validates that the HA entity is imported and control-enabled.
- Rule creation/update rejects unsupported or high-risk HA domains.
- Non-idempotent HA `scene`/`script` actions require cooldown.

## Frontend Design

### Navigation

Modify the current tab model:

- `web/src/components/Sidebar.tsx`
- `web/src/App.tsx`
- mobile tab rendering in the same component
- `web/src/i18n/locales/zh/nav.json`
- `web/src/i18n/locales/en/nav.json`
- `web/src/lib/navIcons.tsx` if a new icon is added

Add `homeAssistant` as a top-level tab, likely between `设备` and `家庭`.

### Home Assistant tab layout

Recommended sections:

1. Connection card
   - status
   - base URL
   - token input
   - test/save/refresh
2. Sync summary
   - discovered entities
   - imported entities
   - control-enabled devices
   - unsupported/high-risk count
3. Entity manager
   - search
   - domain filter
   - room/area filter if available
   - show discovered/imported/control-enabled/read-only status
4. Per-entity controls
   - import toggle
   - control permission toggle
   - unsupported/high-risk reason
   - last sync/error status

### Devices tab changes

The existing Devices tab should not become a HA settings page. It should only consume unified imported devices and display them with source badges.

For HA read-only devices:

- show status and properties
- hide write controls
- show “只读” badge

For HA control-enabled devices:

- show mapped safe controls only
- no raw service picker

## Backend Error Handling

Stable HA errors:

| Code | Meaning |
| --- | --- |
| `ha_not_configured` | HA is not enabled or missing URL/token |
| `ha_unreachable` | Cannot connect to HA |
| `ha_unauthorized` | HA returned 401/403 |
| `ha_timeout` | HA request timed out |
| `ha_invalid_response` | HA returned invalid JSON or unexpected shape |
| `ha_entity_not_found` | entity_id not found in current HA states |
| `ha_not_imported` | entity not included in Miloco |
| `ha_control_disabled` | entity imported but control permission disabled |
| `ha_domain_unsupported` | domain has no control mapper |
| `ha_domain_blocked` | domain is safety-blocked |
| `ha_service_rejected` | HA rejected service call |

Errors must not include:

- token
- Authorization header
- full HA request body if it could include secrets
- unrelated HA config

## Observability and Audit

Action ledger should record:

- source: `home_assistant`
- instance_key
- entity_id
- normalized Miloco device ID
- action/spec name
- value length or sanitized value when safe
- success/failure
- stable error code
- latency

Action ledger must not record:

- token
- Authorization header
- raw HA bearer value
- complete config payload

Health/status should expose:

- configured
- connected
- entity count
- imported count
- control-enabled count
- last refresh time
- last error code/message

## Security Model

The HA long-lived token is powerful, so Miloco must assume compromise impact is meaningful.

Controls:

- Token is write-only in frontend.
- Token is hidden in pydantic errors.
- Token is redacted from CLI errors and logs.
- Browser never receives raw token after save.
- Agent never receives raw token.
- HA service calls are generated only by backend allowlist mapping.
- Per-device `control_enabled` is enforced server-side.
- High-risk domains are blocked for control in MVP.
- A dedicated HA user/token should be recommended in docs.

Important limitation:

- Miloco control permission is an application-level guard. It does not reduce the permission of the HA token inside Home Assistant. If the token itself has broad rights, a compromise of the Miloco backend host is still equivalent to compromise of that HA token.

## Backward Compatibility

Must preserve:

- Existing `/api/miot/*` endpoints.
- Existing Xiaomi account binding flow.
- Existing MiOT home scope.
- Existing MiOT camera and RTSP camera behavior.
- Existing MiOT CLI commands for users who only have Xiaomi devices.
- Existing MiOT rules whose actions do not include a `source`.
- Existing OpenClaw/Hermes behavior when no HA config exists.

Default no-HA behavior:

- `home_assistant.enabled=false`.
- HA tab shows setup prompt.
- Unified devices API returns MiOT-only data.
- Agent catalog remains MiOT-only.
- No additional HA network calls occur.

## Testing Requirements

### Backend unit tests

- HA settings URL validation.
- Token redaction in validation and client errors.
- HA ID parse/format.
- Entity scope persistence.
- Control permission enforcement.
- Domain allowlist/blocklist mapping.
- Mapper behavior for switch/light/fan/cover/climate/sensor/scene/script.

### Backend integration tests

Use a fake HA server that implements:

- `GET /api/`
- `GET /api/config`
- `GET /api/states`
- `GET /api/services`
- `POST /api/services/<domain>/<service>`

Verify:

- connection test succeeds/fails correctly.
- unauthorized response maps to `ha_unauthorized`.
- imported read-only devices show no writable specs.
- control-disabled devices reject control.
- control-enabled switch/light maps to correct HA service call.
- rejected HA service returns `ha_service_rejected`.
- MiOT-only behavior remains unchanged.

### CLI tests

- `miloco-cli device list` shows imported HA devices.
- `miloco-cli device spec ha:primary:light.kitchen` shows synthesized specs.
- `miloco-cli device control ha:primary:light.kitchen on true` calls unified endpoint.
- control-disabled HA device returns `ha_control_disabled`.
- token never appears in stdout/stderr.

### Web tests

- Sidebar includes Home Assistant tab.
- Home Assistant tab renders connection card.
- Saved config readback masks token.
- Entity manager shows import and control permission toggles.
- Control permission toggle is disabled for blocked domains.
- Devices tab shows imported HA device with source badge.
- Read-only HA device has no direct control UI.

### Agent/plugin tests

- `miloco-devices` skill no longer claims non-MiOT devices are unsupported.
- Catalog includes imported HA devices.
- Catalog emits read-only specs when `control_enabled=false`.
- Agent instructions require CLI device commands and forbid raw HA API calls.
- Dangerous actions remain confirmation-gated.

### Rule tests

- Existing MiOT rule fixtures still pass.
- HA rule action validates `included=true` and `control_enabled=true`.
- HA switch/light rule executes through unified device executor.
- HA scene/script actions require cooldown.
- Blocked domains are rejected at create/update time.

## Deployment and Acceptance

Local/lab acceptance should happen before production:

1. Local fake HA acceptance.
2. Optional real HA lab endpoint if available.
3. `ai-lab01.esxi` / `ai-lab02.esxi` deployment validation when requested.
4. Production `miloco.esxi` deployment only under a separate approved CO/PAM process.

Production smoke criteria:

- Miloco UI opens.
- Existing Xiaomi devices remain visible.
- Existing Xiaomi control still works.
- Home Assistant tab exists in sidebar.
- HA config can be tested and saved without token readback.
- HA discovered entities appear in HA tab.
- Importing one safe HA switch/light makes it appear in Devices tab.
- With control disabled, Miloco rejects control.
- After enabling control for that device, a safe control action succeeds.
- Agent catalog includes the imported HA device with correct access level.
- Logs and CLI output do not contain the HA token.

## Rollback

- Disable HA with `home_assistant.enabled=false`.
- Clear HA token from `$MILOCO_HOME/config.json` if credential retirement is needed.
- Keep `/api/miot/*` untouched so Xiaomi-only behavior remains available.
- If unified devices API causes frontend/CLI regression, temporarily point consumers back to MiOT-only endpoints while HA is disabled.
- Entity scope rows can remain in SQLite; they have no effect while HA is disabled.

## Spec Decisions

The following decisions are intentionally fixed for MVP:

1. One HA instance only, key `primary`.
2. Dedicated `Home Assistant` left-sidebar tab is required.
3. Discovery, import, and control permission are separate concepts.
4. Imported HA devices default to read-only.
5. Control permission is per device and enforced backend-side.
6. Agent and CLI must use Miloco device commands, never raw HA service calls.
7. High-risk HA domains are not controllable in MVP.
8. REST polling is the MVP data path; WebSocket state subscription is deferred.
