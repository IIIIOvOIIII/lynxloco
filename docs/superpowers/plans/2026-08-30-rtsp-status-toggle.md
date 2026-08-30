# RTSP Status Ribbon and Toggle UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Miloco's dashboard show `在看家` when RTSP cameras are actively feeding perception, and provide a native-like RTSP perception enable/disable switch.

**Architecture:** Keep the change frontend-only. Add a small pure helper for source-agnostic active-camera detection, wire it into `App.tsx`, and replace the RTSP text enable/disable action with a hook-free switch component that reuses the existing `onToggleRtsp` backend path.

**Tech Stack:** React 19, TypeScript, Vite, Vitest in Node environment, existing Miloco FastAPI backend APIs.

**Spec:** `docs/superpowers/specs/2026-08-30-rtsp-status-toggle-design.md`

## Global Constraints

- Do not change backend APIs, RTSP credential storage, Omni model configuration, camera probe logic, or OpenClaw gateway configuration.
- Do not print or store API keys, Xiaomi tokens, RTSP URLs, raw model responses, or camera frames.
- Count an RTSP camera as active for the top ribbon only when `sourceType === "rtsp" && enabled === true && connected === true`.
- Keep the existing MIoT `scopeCameras.some(c => c.inUse)` behavior.
- When RTSP summary data is not ready or is stale, do not show `待机中` solely because MIoT scope cameras are off; prefer the existing safe bias of not falsely reporting standby.
- Use existing RTSP backend actions through `onToggleRtsp`; do not add a new endpoint.
- Use TDD: write and run failing tests before changing production code.

---

### Task 1: Source-Agnostic Active Camera Derivation

**Files:**
- Create: `web/src/lib/perceptionActivity.ts`
- Create: `web/tests/perception-activity.test.ts`
- Modify: `web/src/App.tsx`

**Interfaces:**
- Consumes: `ScopeCamera` and `CameraSummary` from `web/src/lib/types.ts`.
- Produces:
  - `hasActivePerceptionCamera(scopeCameras: ScopeCamera[], cameraSummaries: CameraSummary[]): boolean`
  - `hasActiveRtspPerceptionCamera(cameraSummaries: CameraSummary[]): boolean`

- [ ] **Step 1: Write the failing tests**

Create `web/tests/perception-activity.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import {
  hasActivePerceptionCamera,
  hasActiveRtspPerceptionCamera,
} from "@/lib/perceptionActivity";
import type { CameraSummary, ScopeCamera } from "@/lib/types";

function scopeCamera(overrides: Partial<ScopeCamera> = {}): ScopeCamera {
  return {
    did: "miot-1",
    name: "MIoT",
    channel: 0,
    channelCount: 1,
    roomName: "Balcony",
    cloudOnline: false,
    lanReachable: false,
    awake: null,
    inUse: false,
    voiceInUse: false,
    perceptionPrompt: "",
    connected: false,
    ...overrides,
  };
}

function summary(overrides: Partial<CameraSummary> = {}): CameraSummary {
  return {
    id: "rtsp:1",
    sourceType: "rtsp",
    name: "RTSP",
    roomName: "Kitchen",
    enabled: true,
    connected: true,
    videoCodec: "h264",
    audioCodec: "aac",
    lastFrameUnixMs: 1,
    hasPassword: true,
    errorCode: null,
    errorMessage: null,
    ...overrides,
  };
}

describe("hasActivePerceptionCamera", () => {
  it("counts an enabled and connected RTSP camera even when every MIoT camera is off", () => {
    expect(hasActivePerceptionCamera([scopeCamera()], [summary()])).toBe(true);
  });

  it("does not count disconnected or disabled RTSP cameras", () => {
    expect(
      hasActivePerceptionCamera(
        [scopeCamera()],
        [
          summary({ id: "rtsp:disabled", enabled: false, connected: true }),
          summary({ id: "rtsp:offline", enabled: true, connected: false }),
        ],
      ),
    ).toBe(false);
  });

  it("preserves MIoT inUse as an active perception camera", () => {
    expect(hasActivePerceptionCamera([scopeCamera({ inUse: true })], [])).toBe(true);
  });
});

describe("hasActiveRtspPerceptionCamera", () => {
  it("ignores MIoT summaries and inactive RTSP summaries", () => {
    expect(
      hasActiveRtspPerceptionCamera([
        summary({ id: "miot-1", sourceType: "miot", hasPassword: false }),
        summary({ id: "rtsp:off", enabled: false, connected: true }),
      ]),
    ).toBe(false);
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
cd web
pnpm test tests/perception-activity.test.ts
```

Expected: FAIL because `@/lib/perceptionActivity` does not exist.

- [ ] **Step 3: Implement the helper**

Create `web/src/lib/perceptionActivity.ts`:

```ts
import type { CameraSummary, ScopeCamera } from "@/lib/types";

export function hasActiveRtspPerceptionCamera(
  cameraSummaries: CameraSummary[],
): boolean {
  return cameraSummaries.some(
    (camera) =>
      camera.sourceType === "rtsp" &&
      camera.enabled &&
      camera.connected,
  );
}

export function hasActivePerceptionCamera(
  scopeCameras: ScopeCamera[],
  cameraSummaries: CameraSummary[],
): boolean {
  return (
    scopeCameras.some((camera) => camera.inUse) ||
    hasActiveRtspPerceptionCamera(cameraSummaries)
  );
}
```

- [ ] **Step 4: Wire App status ribbon**

Modify `web/src/App.tsx`:

```ts
import { hasActivePerceptionCamera } from "@/lib/perceptionActivity";
```

Replace the current `allCamerasOff={...scopeCameras.data.every(...)}` expression with:

```tsx
allCamerasOff={
  !scopeCameras.loading &&
  !scopeCameras.error &&
  !!scopeCameras.data &&
  !cameraSummaries.loading &&
  !cameraSummaryState.fatalError &&
  !!cameraSummaries.data &&
  !hasActivePerceptionCamera(scopeCameras.data, cameraSummaries.data)
}
```

- [ ] **Step 5: Verify green**

Run:

```bash
cd web
pnpm test tests/perception-activity.test.ts
```

Expected: PASS.

---

### Task 2: RTSP Perception Switch Component

**Files:**
- Modify: `web/src/components/HeroNow.tsx`
- Modify: `web/src/i18n/locales/zh/rtspCamera.json`
- Modify: `web/src/i18n/locales/en/rtspCamera.json`
- Create: `web/tests/rtsp-perception-switch.test.tsx`

**Interfaces:**
- Consumes: `CameraSummary`.
- Produces exported hook-free component:

```ts
export function RtspPerceptionSwitch(props: {
  camera: CameraSummary;
  busy: boolean;
  disabled?: boolean;
  label: string;
  busyLabel: string;
  ariaLabel: string;
  title: string;
  onToggle: (camera: CameraSummary, enabled: boolean) => void | Promise<void>;
}): React.JSX.Element
```

- [ ] **Step 1: Write the failing component tests**

Create `web/tests/rtsp-perception-switch.test.tsx`:

```tsx
import { describe, expect, it, vi } from "vitest";

import { RtspPerceptionSwitch } from "@/components/HeroNow";
import type { CameraSummary } from "@/lib/types";

function camera(overrides: Partial<CameraSummary> = {}): CameraSummary {
  return {
    id: "rtsp:1",
    sourceType: "rtsp",
    name: "Kitchen RTSP",
    roomName: "Kitchen",
    enabled: false,
    connected: false,
    videoCodec: null,
    audioCodec: null,
    lastFrameUnixMs: null,
    hasPassword: true,
    errorCode: null,
    errorMessage: null,
    ...overrides,
  };
}

describe("RtspPerceptionSwitch", () => {
  it("exposes switch state from the RTSP enabled flag", () => {
    const element = RtspPerceptionSwitch({
      camera: camera({ enabled: true }),
      busy: false,
      label: "感知",
      busyLabel: "启用前正在测试连接…",
      ariaLabel: "停用 Kitchen RTSP 的 RTSP 感知",
      title: "停用 RTSP 感知",
      onToggle: vi.fn(),
    });

    expect(element.type).toBe("button");
    expect(element.props.role).toBe("switch");
    expect(element.props["aria-checked"]).toBe(true);
    expect(element.props.disabled).toBe(false);
  });

  it("clicking requests the opposite enabled state through the existing callback", () => {
    const rtsp = camera({ enabled: false });
    const onToggle = vi.fn();
    const element = RtspPerceptionSwitch({
      camera: rtsp,
      busy: false,
      label: "感知",
      busyLabel: "启用前正在测试连接…",
      ariaLabel: "启用 Kitchen RTSP 的 RTSP 感知",
      title: "启用 RTSP 感知",
      onToggle,
    });

    element.props.onClick();

    expect(onToggle).toHaveBeenCalledWith(rtsp, true);
  });

  it("does not call the toggle callback while busy", () => {
    const onToggle = vi.fn();
    const element = RtspPerceptionSwitch({
      camera: camera({ enabled: false }),
      busy: true,
      label: "感知",
      busyLabel: "启用前正在测试连接…",
      ariaLabel: "启用 Kitchen RTSP 的 RTSP 感知",
      title: "启用 RTSP 感知",
      onToggle,
    });

    element.props.onClick();

    expect(element.props.disabled).toBe(true);
    expect(onToggle).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
cd web
pnpm test tests/rtsp-perception-switch.test.tsx
```

Expected: FAIL because `RtspPerceptionSwitch` is not exported.

- [ ] **Step 3: Add i18n keys**

Add these keys under `rtspCamera`:

Chinese:

```json
"perception": "感知",
"perceptionOn": "感知已启用",
"perceptionOff": "感知已停用",
"toggleAriaEnable": "启用 {{name}} 的 RTSP 感知",
"toggleAriaDisable": "停用 {{name}} 的 RTSP 感知",
"toggleTitleEnable": "启用后 Miloco 会测试并接入这路 RTSP 摄像头进行感知",
"toggleTitleDisable": "停用后 Miloco 不再处理这路 RTSP 摄像头"
```

English:

```json
"perception": "Perception",
"perceptionOn": "Perception enabled",
"perceptionOff": "Perception disabled",
"toggleAriaEnable": "Enable RTSP perception for {{name}}",
"toggleAriaDisable": "Disable RTSP perception for {{name}}",
"toggleTitleEnable": "Enable this RTSP camera for Miloco perception after a connection test",
"toggleTitleDisable": "Disable Miloco perception for this RTSP camera"
```

- [ ] **Step 4: Implement and integrate the switch**

In `web/src/components/HeroNow.tsx`, export `RtspPerceptionSwitch` near `RtspCameraSection`. The component should return a `button` with `role="switch"`, `aria-checked={camera.enabled}`, `disabled={busy || disabled}`, and call `onToggle(camera, !camera.enabled)` when not busy/disabled.

Replace the current text enable/disable button in the RTSP card action area with:

```tsx
<RtspPerceptionSwitch
  camera={camera}
  busy={busy}
  disabled={!onToggle}
  label={t("rtspCamera.perception")}
  busyLabel={t("rtspCamera.enableTesting")}
  ariaLabel={t(
    camera.enabled ? "rtspCamera.toggleAriaDisable" : "rtspCamera.toggleAriaEnable",
    { name: camera.name },
  )}
  title={t(camera.enabled ? "rtspCamera.toggleTitleDisable" : "rtspCamera.toggleTitleEnable")}
  onToggle={(target, enabled) => void runToggle(target, enabled)}
/>
```

Change `runToggle` to accept the explicit next value:

```ts
const runToggle = async (camera: CameraSummary, enabled: boolean) => {
  if (!onToggle || busyId) return;
  setBusyId(camera.id);
  try {
    await onToggle(camera, enabled);
  } finally {
    setBusyId(null);
  }
};
```

- [ ] **Step 5: Verify green**

Run:

```bash
cd web
pnpm test tests/rtsp-perception-switch.test.tsx
```

Expected: PASS.

---

### Task 3: Frontend Regression Gates and Commit

**Files:**
- Verify all modified frontend files.
- Commit only the implementation/test files for this feature.

**Interfaces:**
- Consumes: Tasks 1 and 2.
- Produces: One source commit to deploy.

- [ ] **Step 1: Run focused tests**

```bash
cd web
pnpm test tests/perception-activity.test.ts tests/rtsp-perception-switch.test.tsx tests/perception-camera-view.test.ts tests/rtsp-polling.test.ts
```

Expected: PASS.

- [ ] **Step 2: Run type/build checks**

```bash
cd web
pnpm build
```

Expected: PASS.

- [ ] **Step 3: Run source hygiene**

```bash
git diff --check
```

Expected: PASS.

- [ ] **Step 4: Commit implementation**

Stage only the owned feature files:

```bash
git add \
  web/src/lib/perceptionActivity.ts \
  web/tests/perception-activity.test.ts \
  web/src/App.tsx \
  web/src/components/HeroNow.tsx \
  web/src/i18n/locales/zh/rtspCamera.json \
  web/src/i18n/locales/en/rtspCamera.json \
  web/tests/rtsp-perception-switch.test.tsx
git commit -m "fix(web): count rtsp cameras in watching state"
```

Expected: commit succeeds without staging unrelated progress or CO documents.

---

### Task 4: Production Deployment to miloco.esxi

**Files:**
- Create/update deployment CO notes under `docs/ops/2026-08-30-standby-activation/`.
- Update `docs/2026-08-30-miloco-vm-migration_PROGRESS.md`.

**Interfaces:**
- Consumes: exact implementation commit SHA from Task 3.
- Produces: deployed Miloco build on `miloco.esxi`.

- [ ] **Step 1: Build release artifacts**

Use the repository's existing source-release build flow for the exact source SHA. Record non-secret artifact names and SHA-256 hashes in the deployment notes.

- [ ] **Step 2: Create Software CO**

Create an ITSM OpenClaw CO for `miloco.esxi/root` with:

- exact source SHA in the implementation plan,
- official installer-based deployment approach,
- rollback to previous installed Miloco version/config backup,
- no credential values in payload or notes.

- [ ] **Step 3: Wait for approval**

Run `poll` until the CO is `state=Implement` with `pam_status=active`. If it requires Lynx approval, stop and ask the user.

- [ ] **Step 4: Verify exact-SHA deployment gate**

Run `itsm_co.py verify-deploy` with the CO number, payload file, receipt file, exact SHA, host `miloco.esxi`, and user `root`.

- [ ] **Step 5: Deploy through the official installer flow**

Upload only required release artifacts to a bounded temporary staging directory on `miloco.esxi`, serve the artifacts over loopback, and run the existing `install.sh --agent-prepare` and `install.sh --agent-finish` flow.

- [ ] **Step 6: Production verification**

Run bounded, sanitized checks:

- `miloco-cli service status` shows `running=true`.
- `http://127.0.0.1:1810/health` returns OK.
- `http://miloco.esxi:1810/health` returns OK.
- Dashboard root returns HTTP 200 HTML.
- Sanitized APIs show engine `running=true`, `ready=true`, active RTSP `enabled && connected` count at least 1, and derived ribbon state `在看家`.

- [ ] **Step 7: Cleanup and close CO**

Remove only exact temporary staging directories, close the CO truthfully, and update progress/memory before final response.
