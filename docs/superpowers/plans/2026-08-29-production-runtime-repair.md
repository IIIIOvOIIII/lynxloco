# Miloco Production Runtime Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make active RTSP cameras appear in the unified perception area, make direct-HTTP H.264 playback wait for JMuxer readiness, and make every Omni connection test prove the selected protocol's visual capability with accurate live health.

**Architecture:** Build a pure source-neutral perception view model in the web layer while preserving MIoT-only command paths. Put the JMuxer readiness race behind a dependency-free public ES module with an explicit ready/cancel lifecycle. Reuse the existing synthetic red JPEG and provider adapters for all visual preflights, then pass visual-request context into the existing HTTP error classifier and stream live health into the model table.

**Tech Stack:** React 19, TypeScript, Vitest, browser ES modules, JMuxer 2.0.5, Python 3.11+, FastAPI, httpx, pytest, Docker Compose, immutable Git-SHA deployment.

**Spec:** `docs/superpowers/specs/2026-08-29-production-runtime-repair-design.md`

## Global Constraints

- `/api/perception/devices` is authoritative for live-perception membership and count.
- `/api/cameras` enriches perception devices by exact ID; do not infer source type from names, rooms, URLs, or prefixes.
- `/api/miot/scope/cameras` remains MIoT-only; RTSP IDs must never reach MIoT bulk, voice, prompt, or feed commands.
- The existing RTSP management area remains the only add/edit/enable/disable/delete surface.
- Direct HTTP remains supported; MSE/JMuxer is a required path, not a temporary workaround.
- Do not change WebCodecs behaviour, H.265 behaviour, the camera WebSocket protocol, or RTSP lifecycle/security.
- The first H.264 access unit must not be fed before JMuxer `onReady`, and must be fed exactly once after readiness.
- Every model test uses the selected protocol with a generated synthetic image; a text-only 200 cannot produce green status.
- Supported protocol values remain exactly `openai_chat_completions`, `openai_responses`, and `gemini_native`.
- Never log or persist RTSP URLs, camera credentials, API keys, authorisation headers, raw frames, base64 images, or provider response bodies.
- The existing credential identity boundary remains intact; protocol changes do not silently reuse a key.
- Do not add recording, NVR, replay, audio-to-Responses, Apache, HTTPS, certificates, SmartDNS, or an FQDN.
- Do not restore automatic retention deletion or same-SHA image rebuilding.
- Do not run the mutating `task lint` command.
- Do not push to `XiaoMi/xiaomi-miloco`; keep commits local unless an authorised writable remote is provided.
- Local tests, AI-lab fixture acceptance, real browser/camera proof, and production acceptance are separate evidence levels.

---

### Task 1: Build and render the source-neutral perception view model

**Files:**
- Create: `web/src/lib/perceptionCameraView.ts`
- Create: `web/tests/perception-camera-view.test.ts`
- Modify: `web/src/lib/types.ts:260-285`
- Modify: `web/src/App.tsx:140-169, 308-355`
- Modify: `web/src/components/HeroNow.tsx:1-260, 485-760`

**Interfaces:**
- Consumes: `PerceptionCamera[]` from `/api/perception/devices`, credential-safe `CameraSummary[]` from `/api/cameras`, `ScopeCamera[]` from the MIoT scope endpoint, and `feedDid(did, channel, multiChannel)` from `web/src/lib/cameraChannel.ts`.
- Produces: `buildPerceptionCameraViews(perceptionCameras, cameraSummaries, scopeCameras): PerceptionCameraView[]`; `HeroNow.perceptionCameras`; one unified active-card list whose MIoT entries retain existing controls and whose RTSP/unenriched entries have no MIoT command controls.

- [ ] **Step 1: Write the failing pure view-model tests**

  Create `web/tests/perception-camera-view.test.ts` with fixtures that prove exact-ID joining, stable de-duplication, RTSP classification, MIoT scope attachment, and visible unenriched rows:

  ```ts
  import { describe, expect, it } from "vitest";
  import { buildPerceptionCameraViews } from "../src/lib/perceptionCameraView";
  import type { CameraSummary, PerceptionCamera, ScopeCamera } from "../src/lib/types";

  const perception: PerceptionCamera[] = [
    { did: "miot-1", name: "MIoT live", roomName: "Study" },
    { did: "rtsp-1", name: "RTSP live", roomName: "Garage" },
    { did: "unknown-1", name: "Metadata delayed", roomName: "Hall" },
    { did: "rtsp-1", name: "duplicate must not replace first" },
  ];

  const summaries: CameraSummary[] = [
    {
      id: "miot-1", sourceType: "miot", name: "MIoT summary", roomName: "Study",
      enabled: true, connected: true, videoCodec: "h264", audioCodec: null,
      lastFrameUnixMs: 1, hasPassword: false, errorCode: null, errorMessage: null,
    },
    {
      id: "rtsp-1", sourceType: "rtsp", name: "RTSP summary", roomName: "Garage",
      enabled: true, connected: true, videoCodec: "h264", audioCodec: null,
      lastFrameUnixMs: 1, hasPassword: true, errorCode: null, errorMessage: null,
    },
  ];

  const scope = [{
    did: "miot-1", name: "MIoT scope", channel: 0, channelCount: 1,
    roomName: "Study", cloudOnline: true, lanReachable: true, awake: true,
    inUse: true, connected: true, voiceInUse: false,
    perceptionPrompt: "",
  }] as ScopeCamera[];

  describe("buildPerceptionCameraViews", () => {
    it("uses perception membership and exact IDs for source enrichment", () => {
      const views = buildPerceptionCameraViews(perception, summaries, scope);
      expect(views.map((view) => view.id)).toEqual(["miot-1", "rtsp-1", "unknown-1"]);
      expect(views[0]).toMatchObject({ sourceType: "miot", miotScope: scope[0] });
      expect(views[1]).toMatchObject({ sourceType: "rtsp", miotScope: null });
      expect(views[2]).toMatchObject({ sourceType: null, summary: null, miotScope: null });
    });

    it("does not classify by a misleading name or room", () => {
      const views = buildPerceptionCameraViews(
        [{ did: "plain-id", name: "rtsp camera", roomName: "miot room" }],
        [],
        [],
      );
      expect(views[0].sourceType).toBeNull();
    });
  });
  ```

- [ ] **Step 2: Run the focused test and confirm RED**

  Run:

  ```bash
  cd web && npm test -- tests/perception-camera-view.test.ts
  ```

  Expected: fail because `web/src/lib/perceptionCameraView.ts` does not exist.

- [ ] **Step 3: Add the view type and minimal builder**

  Add this interface to `web/src/lib/types.ts` next to the existing camera types:

  ```ts
  export interface PerceptionCameraView {
    id: string;
    name: string;
    roomName?: string;
    sourceType: CameraSourceType | null;
    connected: boolean | null;
    summary: CameraSummary | null;
    miotScope: ScopeCamera | null;
  }
  ```

  Create `web/src/lib/perceptionCameraView.ts` with this exact join policy:

  ```ts
  import type {
    CameraSummary,
    PerceptionCamera,
    PerceptionCameraView,
    ScopeCamera,
  } from "@/lib/types";
  import { feedDid } from "@/lib/cameraChannel";

  export function buildPerceptionCameraViews(
    perceptionCameras: PerceptionCamera[],
    cameraSummaries: CameraSummary[],
    scopeCameras: ScopeCamera[],
  ): PerceptionCameraView[] {
    const summaryById = new Map(cameraSummaries.map((camera) => [camera.id, camera]));
    const scopeById = new Map(
      scopeCameras.map((camera) => [
        feedDid(camera.did, camera.channel, camera.channelCount > 1),
        camera,
      ]),
    );
    const seen = new Set<string>();
    const views: PerceptionCameraView[] = [];

    for (const camera of perceptionCameras) {
      if (seen.has(camera.did)) continue;
      seen.add(camera.did);
      const summary = summaryById.get(camera.did) ?? null;
      const miotScope = scopeById.get(camera.did) ?? null;
      views.push({
        id: camera.did,
        name: camera.name || summary?.name || camera.did,
        roomName: camera.roomName || summary?.roomName || undefined,
        sourceType: summary?.sourceType ?? (miotScope ? "miot" : null),
        connected: summary?.connected ?? null,
        summary,
        miotScope,
      });
    }
    return views;
  }
  ```

- [ ] **Step 4: Run the builder tests and confirm GREEN**

  Run:

  ```bash
  cd web && npm test -- tests/perception-camera-view.test.ts
  ```

  Expected: pass.

- [ ] **Step 5: Thread the authoritative list into `HeroNow`**

  In `web/src/App.tsx`, pass the already-loaded perception list without adding another request:

  ```tsx
  <HeroNow
    persons={persons.data}
    perceptionCameras={cameras.data}
    scopeCameras={scopeCameras.data}
    cameraSummaries={cameraSummaries.data}
    // existing props remain unchanged
  />
  ```

  Extend `HeroNow` props and build the unified list with `useMemo`:

  ```tsx
  import type { PerceptionCamera, PerceptionCameraView } from "@/lib/types";
  import { buildPerceptionCameraViews } from "@/lib/perceptionCameraView";

  interface Props {
    perceptionCameras: PerceptionCamera[];
    // existing props remain unchanged
  }

  const perceptionViews = useMemo(
    () => buildPerceptionCameraViews(perceptionCameras, cameraSummaries, scopeCameras),
    [perceptionCameras, cameraSummaries, scopeCameras],
  );
  const activeMiotIds = useMemo(
    () => new Set(
      perceptionViews
        .filter((view) => view.sourceType === "miot" && view.miotScope)
        .map((view) => view.id),
    ),
    [perceptionViews],
  );
  const benchCams = useMemo(
    () => scopeCameras.filter((camera) =>
      !activeMiotIds.has(synthFeedDid(camera.did, camera.channel, camera.channelCount > 1)),
    ),
    [scopeCameras, activeMiotIds],
  );
  ```

  Pass `perceptionViews` and `benchCams` to `CameraSection`. Remove the old `connected`-derived `streamingCams`; the perception API now owns active membership.

- [ ] **Step 6: Render mixed live cards while keeping MIoT controls isolated**

  Change `CameraSectionProps` to accept `perceptionViews: PerceptionCameraView[]` and render each authoritative row. Reuse `CamCardWithToggle` only when `view.sourceType === "miot" && view.miotScope`; otherwise render this private source-neutral card:

  ```tsx
  function SourceNeutralCamCard({ camera }: { camera: PerceptionCameraView }) {
    const { t } = useTranslation();
    return (
      <article className="w-[min(82vw,420px)] shrink-0 snap-start rounded-xl border border-border bg-bg-primary p-3">
        <LivePlayerPlaceholder
          cameraName={camera.name}
          roomName={camera.roomName}
          cameraId={camera.id}
          className="mb-3"
        />
        <div className="flex flex-wrap items-center gap-1.5 text-caption text-text-secondary">
          <span className="text-body text-text-primary">{camera.name}</span>
          {camera.sourceType === "rtsp" && (
            <span className="rounded border border-border px-1.5 py-0.5 text-text-tertiary">
              {t("rtspCamera.sourceRtsp")}
            </span>
          )}
          {camera.roomName && <span>{camera.roomName}</span>}
        </div>
      </article>
    );
  }
  ```

  Use `perceptionViews.length` for `hero.perceivingCount`. Keep MIoT bulk-control calculations on `scopeCameras` only:

  ```tsx
  const perceptionCount = perceptionViews.length;
  const miotActiveCount = scopeCameras.filter((camera) => camera.inUse).length;
  const allOn = scopeCameras.length > 0 && miotActiveCount === scopeCameras.length;
  const allOff = miotActiveCount === 0;
  const atCapacity = miotActiveCount >= maxStreamCams;
  ```

  Replace the old `total === 0` render gate, which is based only on MIoT scope, with independent live-card and MIoT-bench gates. This is required for an RTSP-only installation:

  ```tsx
  const hasLiveCards = perceptionViews.length > 0;
  const hasMiotRows = scopeCameras.length > 0;

  {!hasLiveCards && !hasMiotRows ? (showEmpty ? <CameraEmptyState /> : null) : (
    <>
      {hasLiveCards ? <PerceptionCardRow /> : <NoStreamingState />}
      {benchCams.length > 0 && <MiotBench />}
    </>
  )}
  ```

  The names above may be kept inline rather than extracted as components, but the two booleans and their semantics are fixed: an active RTSP row must render even when `scopeCameras` is empty, while bulk controls and the bench remain MIoT-only.

  Map active cards as follows:

  ```tsx
  {perceptionViews.map((view) =>
    view.sourceType === "miot" && view.miotScope ? (
      <CamCardWithToggle
        key={view.id}
        cam={view.miotScope}
        // preserve the existing MIoT voice, prompt, and toggle props
      />
    ) : (
      <SourceNeutralCamCard key={view.id} camera={view} />
    ),
  )}
  ```

  Keep `RtspCameraSection` unchanged as the management surface.

- [ ] **Step 7: Add source-wiring regression assertions**

  Extend `web/tests/perception-camera-view.test.ts` with source-level assertions, matching the repository's existing low-dependency test style:

  ```ts
  import { readFileSync } from "node:fs";
  import { fileURLToPath } from "node:url";

  it("threads perception devices into HeroNow and keeps RTSP outside MIoT bulk targets", () => {
    const app = readFileSync(fileURLToPath(new URL("../src/App.tsx", import.meta.url)), "utf8");
    const hero = readFileSync(
      fileURLToPath(new URL("../src/components/HeroNow.tsx", import.meta.url)),
      "utf8",
    );
    expect(app).toContain("perceptionCameras={cameras.data}");
    expect(app).toContain("persons.error ?? cameras.error");
    expect(hero).toContain("buildPerceptionCameraViews(");
    expect(hero).toContain("perceptionViews.length");
    expect(hero).toContain("hasLiveCards");
    expect(hero).toContain('view.sourceType === "miot" && view.miotScope');
    expect(hero).not.toContain("onToggleCameras([view.id]");
  });
  ```

- [ ] **Step 8: Run Task 1 verification and commit**

  Run:

  ```bash
  cd web && npm test -- tests/perception-camera-view.test.ts tests/rtsp-polling.test.ts
  cd web && npm run typecheck
  cd web && npm run build
  git diff --check
  ```

  Commit only Task 1 files:

  ```bash
  git add web/src/lib/perceptionCameraView.ts web/src/lib/types.ts web/src/App.tsx \
    web/src/components/HeroNow.tsx web/tests/perception-camera-view.test.ts
  git commit -m "fix(web): include RTSP in perception display"
  ```

---

### Task 2: Gate the first MSE feed on JMuxer readiness

**Files:**
- Create: `web/public/watch-mse.js`
- Create: `web/tests/watch-mse.test.js`
- Modify: `web/public/watch.html:68-154, 317-348, 436-488, 543-615, 690-819`
- Modify: `web/tests/watch-page.test.ts`

**Interfaces:**
- Consumes: `window.JMuxer`, one `<video>` node, the existing H.264 Annex-B first access unit, and browser timer functions.
- Produces: `createReadyJmuxer({ JMuxer, node, onError, timeoutMs, timers }): { ready: Promise<JMuxerInstance>, cancel(): void, instance: JMuxerInstance }`; a player generation token that rejects stale decoder results.

- [ ] **Step 1: Write RED lifecycle tests for a ready/cancel JMuxer session**

  Create `web/tests/watch-mse.test.js`:

  ```js
  import { afterEach, describe, expect, it, vi } from "vitest";
  import { createReadyJmuxer } from "../public/watch-mse.js";

  let options;
  let instance;

  class FakeJMuxer {
    constructor(value) {
      options = value;
      instance = this;
    }
    feed = vi.fn();
    destroy = vi.fn();
  }

  afterEach(() => {
    vi.useRealTimers();
    options = undefined;
    instance = undefined;
  });

  describe("createReadyJmuxer", () => {
    it("does not resolve or feed before onReady", async () => {
      let resolved = false;
      const session = createReadyJmuxer({ JMuxer: FakeJMuxer, node: {} });
      session.ready.then(() => { resolved = true; });
      await Promise.resolve();
      expect(resolved).toBe(false);
      expect(instance.feed).not.toHaveBeenCalled();
      options.onReady();
      expect(await session.ready).toBe(instance);
    });

    it("times out once and destroys the partial instance", async () => {
      vi.useFakeTimers();
      const session = createReadyJmuxer({
        JMuxer: FakeJMuxer,
        node: {},
        timeoutMs: 3000,
      });
      const rejection = expect(session.ready).rejects.toMatchObject({ code: "jmuxer_ready_timeout" });
      await vi.advanceTimersByTimeAsync(3000);
      await rejection;
      expect(instance.destroy).toHaveBeenCalledOnce();
      options.onReady();
      expect(instance.destroy).toHaveBeenCalledOnce();
    });

    it("cancels once and ignores a late ready callback", async () => {
      const session = createReadyJmuxer({ JMuxer: FakeJMuxer, node: {} });
      const rejection = expect(session.ready).rejects.toMatchObject({ code: "jmuxer_cancelled" });
      session.cancel();
      await rejection;
      options.onReady();
      expect(instance.destroy).toHaveBeenCalledOnce();
    });

    it("destroys a ready instance when the player is later torn down", async () => {
      const session = createReadyJmuxer({ JMuxer: FakeJMuxer, node: {} });
      options.onReady();
      await session.ready;
      session.cancel();
      session.cancel();
      expect(instance.destroy).toHaveBeenCalledOnce();
    });

    it("clears the readiness timer when JMuxer construction throws", () => {
      vi.useFakeTimers();
      class ThrowingJMuxer {
        constructor() { throw new Error("constructor failed"); }
      }
      expect(() => createReadyJmuxer({ JMuxer: ThrowingJMuxer, node: {} })).toThrow(
        "constructor failed",
      );
      expect(vi.getTimerCount()).toBe(0);
    });
  });
  ```

- [ ] **Step 2: Run the helper tests and confirm RED**

  Run:

  ```bash
  cd web && npm test -- tests/watch-mse.test.js
  ```

  Expected: fail because `web/public/watch-mse.js` does not exist.

- [ ] **Step 3: Implement the dependency-free readiness helper**

  Create `web/public/watch-mse.js`:

  ```js
  function sessionError(code, message) {
    return Object.assign(new Error(message), { code });
  }

  export function createReadyJmuxer({
    JMuxer,
    node,
    onError,
    timeoutMs = 3000,
    timers = globalThis,
  }) {
    let instance;
    let readySettled = false;
    let destroyed = false;
    let rejectReady;
    let signalReady;
    let timeoutId;

    const readySignal = new Promise((resolve) => { signalReady = resolve; });
    const destroyOnce = () => {
      if (destroyed) return;
      destroyed = true;
      instance?.destroy?.();
    };
    const ready = new Promise((resolve, reject) => {
      rejectReady = reject;
      timeoutId = timers.setTimeout(() => {
        if (readySettled) return;
        readySettled = true;
        try { destroyOnce(); } finally {
          reject(sessionError("jmuxer_ready_timeout", "JMuxer did not become ready in time"));
        }
      }, timeoutMs);
      readySignal.then(() => {
        if (readySettled) return;
        readySettled = true;
        timers.clearTimeout(timeoutId);
        resolve(instance);
      });
    });

    try {
      instance = new JMuxer({
        node,
        mode: "video",
        flushingTime: 0,
        fps: 25,
        debug: false,
        onReady: () => signalReady(),
        onError,
      });
    } catch (error) {
      readySettled = true;
      timers.clearTimeout(timeoutId);
      throw error;
    }

    return {
      instance,
      ready,
      cancel() {
        timers.clearTimeout(timeoutId);
        destroyOnce();
        if (!readySettled) {
          readySettled = true;
          rejectReady(sessionError("jmuxer_cancelled", "JMuxer readiness was cancelled"));
        }
      },
    };
  }
  ```

- [ ] **Step 4: Run the helper tests and confirm GREEN**

  Run:

  ```bash
  cd web && npm test -- tests/watch-mse.test.js
  ```

  Expected: pass.

- [ ] **Step 5: Add player generation and teardown cancellation**

  At the top of the module script in `watch.html`, import the helper:

  ```js
  import { createReadyJmuxer } from "/watch-mse.js";
  ```

  Extend the decoder state:

  ```js
  let jmuxer = null;
  let mseSession = null;
  let playerGeneration = 0;
  let mseFpsRaf = null;
  let lastMseTs = 0;
  ```

  At the start of `tearDown()`, invalidate outstanding decoder work before destroying the current instance:

  ```js
  playerGeneration += 1;
  if (mseSession) {
    try { mseSession.cancel(); } catch {}
    mseSession = null;
    jmuxer = null;
  } else {
    try { jmuxer?.destroy?.(); } catch {}
    jmuxer = null;
  }
  ```

- [ ] **Step 6: Prevent an old decoder promise from changing a new connection**

  Replace the `ensureDecoderInFlight` assignment in `onMessage` with a captured generation and promise identity:

  ```js
  const generation = playerGeneration;
  const configurePromise = ensureDecoder(naluCopy, ts, generation);
  ensureDecoderInFlight = configurePromise;
  configurePromise
    .then(() => {
      if (generation === playerGeneration) setState(t("decoding"));
    })
    .catch((e) => {
      if (generation !== playerGeneration || e?.code === "jmuxer_cancelled") return;
      console.error("configure error", e);
      setState(t("decoderConfigFailed", e.message), true);
    })
    .finally(() => {
      if (ensureDecoderInFlight === configurePromise) ensureDecoderInFlight = null;
    });
  ```

  Change `ensureDecoder(firstKeyNAL, firstKeyTs, generation)` and forward the generation only to the MSE branch. WebCodecs logic otherwise remains byte-for-byte equivalent.

- [ ] **Step 7: Await readiness before the first feed**

  Replace JMuxer construction in `ensureMseDecoder` with:

  ```js
  async function ensureMseDecoder(firstKeyNAL, firstKeyTs, generation) {
    if (typeof window.JMuxer === "undefined") throw new Error(t("mseLibMissing"));
    const canvas = $("v");
    const video = $("vmse");
    canvas.classList.add("hidden");
    video.classList.remove("hidden");
    $("codec").textContent = `${codecHint} (MSE / fmp4 via jmuxer)`;

    const session = createReadyJmuxer({
      JMuxer: window.JMuxer,
      node: video,
      timeoutMs: 3000,
      onError: (data) => {
        if (generation !== playerGeneration) return;
        console.warn("jmuxer error", data);
        setState(t("mseDecodeError", data?.message || data), true);
      },
    });
    mseSession = session;
    const readyMuxer = await session.ready;
    if (generation !== playerGeneration || mseSession !== session) {
      session.cancel();
      throw Object.assign(new Error("stale player generation"), { code: "jmuxer_cancelled" });
    }
    jmuxer = readyMuxer;

    // retain the existing playing and requestVideoFrameCallback handlers here

    readyMuxer.feed({ video: firstKeyNAL, duration: 40 });
    lastMseTs = firstKeyTs;
    configured = true;
    void video.play().catch(() => {
      if (generation === playerGeneration) setState(t("autoplayBlocked"));
    });
  }
  ```

  Add exact Chinese/English inline strings to `I18N`:

  ```js
  mseReadyTimeout: "MSE 初始化超时，请重试串流",
  autoplayBlocked: "画面已就绪，请点击播放",
  ```

  ```js
  mseReadyTimeout: "MSE setup timed out; retry the stream",
  autoplayBlocked: "Video is ready; press play to continue",
  ```

  In the configure catch, map `jmuxer_ready_timeout` exactly and ignore cancellation:

  ```js
  .catch((e) => {
    if (generation !== playerGeneration || e?.code === "jmuxer_cancelled") return;
    console.error("configure error", e);
    if (e?.code === "jmuxer_ready_timeout") {
      setState(t("mseReadyTimeout"), true);
    } else {
      setState(t("decoderConfigFailed", e.message), true);
    }
  })
  ```

  This timeout must not be reported as invalid camera credentials.

- [ ] **Step 8: Add watch-page ordering regression assertions**

  Extend `web/tests/watch-page.test.ts`:

  ```ts
  it("waits for the public JMuxer readiness helper before the first access unit", () => {
    expect(page).toContain('import { createReadyJmuxer } from "/watch-mse.js"');
    const readyAt = page.indexOf("await session.ready");
    const firstFeedAt = page.indexOf("readyMuxer.feed({ video: firstKeyNAL");
    expect(readyAt).toBeGreaterThan(0);
    expect(firstFeedAt).toBeGreaterThan(readyAt);
    expect(page).toContain("generation !== playerGeneration");
    expect(page).toContain("mseSession.cancel()");
    expect(page).toContain("video.play().catch(");
  });
  ```

- [ ] **Step 9: Run Task 2 verification and commit**

  Run:

  ```bash
  cd web && npm test -- tests/watch-mse.test.js tests/watch-page.test.ts
  cd web && npm run typecheck
  cd web && npm run build
  git diff --check
  ```

  Commit:

  ```bash
  git add web/public/watch-mse.js web/public/watch.html \
    web/tests/watch-mse.test.js web/tests/watch-page.test.ts
  git commit -m "fix(stream): wait for JMuxer readiness"
  ```

---

### Task 3: Make every Omni preflight visual and classify visual rejection accurately

**Files:**
- Modify: `backend/miloco/src/miloco/perception/engine/omni/probe.py`
- Modify: `backend/miloco/src/miloco/perception/engine/omni/provider.py`
- Modify: `backend/miloco/src/miloco/perception/engine/omni/error_classifier.py`
- Modify: `backend/miloco/src/miloco/perception/engine/omni/omni_client.py`
- Modify: `backend/miloco/src/miloco/perception/engine/omni/omni.py`
- Modify: `backend/miloco/src/miloco/perception/processor.py`
- Modify: `backend/miloco/src/miloco/admin/router.py`
- Modify: `backend/miloco/tests/perception/engine/omni/test_probe.py`
- Modify: `backend/miloco/tests/perception/engine/omni/test_error_classifier.py`
- Modify: `backend/miloco/tests/perception/engine/omni/test_omni_client_circuit.py`
- Modify: `backend/miloco/tests/perception/engine/omni/test_responses_provider.py`
- Modify: `backend/miloco/tests/perception/test_omni_probe_tick_drive.py`
- Modify: `backend/miloco/tests/admin/test_omni_config_preflight.py`

**Interfaces:**
- Consumes: existing `visual_probe_red.jpg`, OpenAI-shaped message IR, provider adapters, and HTTP responses.
- Produces: `_visual_probe_messages()` shared by all protocols; `messages_have_visual_input(messages): bool`; `classify_response(response, *, visual_request=False)`; new machine code `visual_payload_rejected` in both probe and runtime health.

- [ ] **Step 1: Write RED tests proving Chat and Gemini send visual content**

  Add tests to `test_probe.py` that record outgoing bodies and require image-bearing requests:

  ```python
  @pytest.mark.parametrize(
      ("protocol", "model", "expected_path"),
      [
          ("openai_chat_completions", "xiaomi/mimo-v2.5", "/v1/chat/completions"),
          ("gemini_native", "gemini-vision", "/models/gemini-vision:generateContent"),
      ],
  )
  async def test_non_responses_probe_uses_the_synthetic_red_image(
      monkeypatch, caplog, protocol, model, expected_path
  ):
      calls: list[tuple[str, str, dict]] = []
      payload = (
          {"choices": [{"message": {"content": "red"}}]}
          if protocol == "openai_chat_completions"
          else {"candidates": [{"content": {"parts": [{"text": "red"}]}}]}
      )
      monkeypatch.setattr(
          probe.httpx,
          "AsyncClient",
          _fake_async_client(
              get_resp=_FakeResp(200, {"data": [{"id": model}]}),
              post_resp=_FakeResp(200, payload),
              calls=calls,
          ),
      )
      result = await probe.probe_omni(model, "https://vlm.example/v1", "sk-secret", protocol)
      assert result["ok"] is True
      post = next(call for call in calls if call[0] == "POST")
      assert post[1].endswith(expected_path)
      assert "image" in json.dumps(post[2]["json"])
      assert "sk-secret" not in result["message"]
      assert "sk-secret" not in caplog.text
      assert "data:image" not in caplog.text
  ```

  Add a negative case where the endpoint returns a text-only acknowledgement instead of `red`; require `bad_response`, not success.

- [ ] **Step 2: Write RED tests for visual-request classification**

  Extend `test_error_classifier.py`:

  ```python
  import pytest

  def test_400_visual_request_is_visual_payload_rejected_config():
      result = classify_response(_resp(400), visual_request=True)
      assert result.code == "visual_payload_rejected"
      assert result.category == ErrorCategory.CONFIG
      assert "API Key" not in result.message

  def test_422_non_visual_request_keeps_generic_rejection():
      result = classify_response(_resp(422), visual_request=False)
      assert result.code == "rejected_authed"

  @pytest.mark.parametrize(
      ("provider_code", "expected"),
      [
          ("invalid_api_key", "bad_key"),
          ("authentication_error", "bad_key"),
          ("model_not_found", "not_found"),
          ("invalid_model", "not_found"),
      ],
  )
  def test_safe_structured_error_code_overrides_visual_fallback(provider_code, expected):
      result = classify_response(
          _resp(400, json_body={"error": {"code": provider_code}}),
          visual_request=True,
      )
      assert result.code == expected
  ```

  Extend the local `_resp` test helper with an optional keyword-only `json_body` that serialises through `httpx.Response(json=...)`. Add a negative case with an arbitrary provider message but no allow-listed structured code; it must remain `visual_payload_rejected`. This proves the classifier does not guess from free-form response text.

  Add a pure provider test:

  ```python
  def test_messages_have_visual_input_only_for_image_or_video_blocks():
      assert messages_have_visual_input([
          {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,eA=="}}]}
      ])
      assert messages_have_visual_input([
          {"role": "user", "content": [{"type": "video_url", "video_url": {"url": "data:video/mp4;base64,eA=="}}]}
      ])
      assert not messages_have_visual_input([
          {"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": "data:audio/m4a;base64,eA=="}}]}
      ])
  ```

- [ ] **Step 3: Run focused tests and confirm RED**

  Run:

  ```bash
  cd backend && uv run pytest -q \
    miloco/tests/perception/engine/omni/test_probe.py \
    miloco/tests/perception/engine/omni/test_error_classifier.py \
    -k 'synthetic_red_image or visual_request or visual_input'
  ```

  Expected: fail because Chat still sends `ping`, the classifier has no visual context, and the new code does not exist.

- [ ] **Step 4: Share the existing synthetic-image messages across protocols**

  Rename `_responses_visual_messages()` to `_visual_probe_messages()` in `probe.py` and use it in both `_probe_responses` and `probe_chat`:

  ```python
  messages = _visual_probe_messages()
  body = adapter.build_request_body(
      messages,
      model=model,
      max_tokens=16,
      temperature=0.0,
      top_p=1.0,
      stream=False,
  )
  ```

  Add one exact-answer validator used by both stream and non-stream probes:

  ```python
  def _visual_answer_is_red(value: Any) -> bool:
      return isinstance(value, str) and value.casefold().strip() in {"red", "red."}
  ```

  For non-stream responses, normalise with the selected adapter and extract the existing internal shape. Treat JSON decode, adapter parsing, missing choices, or non-string content as `bad_response`; none of these are reachability failures:

  ```python
  try:
      normalized = adapter.parse_response(response.json())
      output_text = normalized["choices"][0]["message"]["content"]
  except (json.JSONDecodeError, IndexError, KeyError, TypeError, ValueError):
      output_text = None
  if not _visual_answer_is_red(output_text):
      return {
          "ok": False,
          "code": "bad_response",
          "status": 200,
          "latency_ms": latency_ms,
          "message": "视觉预检未识别合成图片",
      }
  ```

  A successful result uses `message: "视觉预检通过"` for all protocols.

  Update the existing success fixtures so they still prove the new contract:

  ```python
  # recording_http_server Chat response
  {"choices": [{"message": {"content": "red"}}]}

  # non-stream Chat success response
  _FakeResp(200, {"choices": [{"message": {"content": "red"}}]})

  # Gemini success response
  {"candidates": [{"content": {"parts": [{"text": "red"}]}}]}
  ```

- [ ] **Step 5: Validate the forced-stream visual answer instead of accepting the first SSE line**

  Change `_probe_stream_chat` to accept the adapter and return accumulated output text. Parse only JSON `data:` lines, stop at `[DONE]`, and use the adapter's existing stream parser. Invalid SSE JSON or a provider-specific chunk the adapter cannot parse is a provider response failure, not a network reachability failure, so return `None` as the output and let `probe_chat` map it to `bad_response`:

  Add `import json` beside the existing standard-library imports in `probe.py`, then implement:

  ```python
  output_parts: list[str] = []
  async for line in resp.aiter_lines():
      line = line.strip()
      if not line.startswith("data: "):
          continue
      data = line[6:]
      if data == "[DONE]":
          break
      try:
          chunk = json.loads(data)
          delta, _usage = adapter.parse_stream_chunk(chunk)
      except (json.JSONDecodeError, KeyError, TypeError, ValueError):
          latency_ms = round((time.monotonic() - t0) * 1000)
          return 200, latency_ms, None, {}
      if delta:
          output_parts.append(delta)
  latency_ms = round((time.monotonic() - t0) * 1000)
  return 200, latency_ms, "".join(output_parts), {}
  ```

  Type the returned output as `str | None`. In `probe_chat`, require `_visual_answer_is_red(output_text)` before returning success. Malformed JSON, an unparseable provider chunk, or a wrong answer returns `bad_response`. Preserve non-200 status and `Retry-After` behaviour.

  Change existing Qwen success SSE fixtures from `pong` to `red`; tests that intentionally exercise wrong or empty content keep their non-red values.

- [ ] **Step 6: Add visual context to the runtime classifier**

  Add this helper to `provider.py`:

  ```python
  def messages_have_visual_input(messages: list[dict[str, Any]]) -> bool:
      for message in messages:
          content = message.get("content") if isinstance(message, dict) else None
          if not isinstance(content, list):
              continue
          for block in content:
              if isinstance(block, dict) and block.get("type") in {"image_url", "video_url"}:
                  return True
      return False
  ```

  Add `visual_payload_rejected` to `CODES` and `_MESSAGES` in `error_classifier.py`. Add a narrow structured-error allow-list that reads only `error.code` or `error.type`; never include `error.message`, the raw body, or the extracted provider value in returned messages or logs:

  ```python
  _SAFE_PROVIDER_CONFIG_CODES = {
      "invalid_api_key": "bad_key",
      "authentication_error": "bad_key",
      "unauthorized": "bad_key",
      "permission_denied": "bad_key",
      "model_not_found": "not_found",
      "invalid_model": "not_found",
  }

  def safe_provider_config_code(resp: httpx.Response) -> str | None:
      try:
          payload = resp.json()
      except (json.JSONDecodeError, httpx.ResponseNotRead, RuntimeError, ValueError):
          return None
      error = payload.get("error") if isinstance(payload, dict) else None
      if not isinstance(error, dict):
          return None
      for field in ("code", "type"):
          value = error.get(field)
          if isinstance(value, str):
              mapped = _SAFE_PROVIDER_CONFIG_CODES.get(value.casefold().strip())
              if mapped:
                  return mapped
      return None
  ```

  Then change the signature and 400/422 branch:

  ```python
  def classify_response(
      resp: httpx.Response,
      *,
      visual_request: bool = False,
  ) -> ClassifiedError | None:
      # existing status handling remains unchanged
      if resp.status_code in (400, 422):
          code = safe_provider_config_code(resp)
          if code is None:
              code = "visual_payload_rejected" if visual_request else "rejected_authed"
          return ClassifiedError(code, _MESSAGES[code], ErrorCategory.CONFIG)
  ```

  Use this exact message:

  ```python
  "visual_payload_rejected": "端点可连接，但当前协议或视觉请求不受支持",
  ```

  Centralise probe-to-breaker category mapping in the same module so retry and automatic tick cannot drift:

  ```python
  CONFIG_CODES = frozenset(
      {"bad_key", "no_key", "not_found", "rejected_authed", "visual_payload_rejected"}
  )

  def category_for_code(code: str) -> ErrorCategory:
      return ErrorCategory.CONFIG if code in CONFIG_CODES else ErrorCategory.RECOVERABLE
  ```

- [ ] **Step 7: Pass visual context through every runtime HTTP path**

  In `omni_client.call_omni`, `omni_client.call_omni_stream`, and `omni._call_omni_messages`, calculate once after messages are built:

  ```python
  visual_request = messages_have_visual_input(messages)
  ```

  Pass it to every direct and `HTTPStatusError` classification site:

  ```python
  classified = classify_response(resp, visual_request=visual_request)
  ```

  ```python
  classified = classify_response(
      exc.response,
      visual_request=visual_request,
  )
  ```

  Do not inspect or log message contents in the classifier. The structured-error helper may inspect only the allow-listed `error.code`/`error.type` fields and must never return or log provider response content. Keep the existing safe size-only payload summary unchanged. In the live streaming path, preserve the existing read order; if a streaming response body is not yet readable, the helper returns `None` and the visual-context fallback remains authoritative.

- [ ] **Step 8: Make only visual probe HTTP 400/422 use the visual-specific code**

  Extend `_probe_http_failure` with explicit context so a Responses `GET /models` rejection cannot be misreported as proof that the server rejected image input:

  ```python
  def _probe_http_failure(
      response: httpx.Response,
      latency_ms: int,
      *,
      visual_request: bool = False,
  ) -> dict[str, Any]:
      status = response.status_code
      if status in (400, 422):
          code = safe_provider_config_code(response)
          if code is None:
              code = "visual_payload_rejected" if visual_request else "rejected_authed"
          return {
              "ok": False,
              "code": code,
              "status": status,
              "latency_ms": latency_ms,
              "message": {
                  "bad_key": "API Key 无效或无权限",
                  "not_found": "模型或地址不存在",
                  "rejected_authed": "已连接，但请求被拒绝（模型名或 API Key 可能有误）",
                  "visual_payload_rejected": "端点可连接，但当前协议或视觉请求不受支持",
              }[code],
          }
      # preserve the existing status mapping below
  ```

  Import `safe_provider_config_code` from `error_classifier.py`. Call this helper with `visual_request=False` for the Responses `GET /models` request and `visual_request=True` only for the Responses visual `POST /responses`. In the corresponding `probe_chat` branch, call `safe_provider_config_code(r)` first and use its exact `bad_key`/`not_found` copy when present; otherwise the POST is the visual probe, so return:

  ```python
  {
      "ok": False,
      "code": "visual_payload_rejected",
      "status": status,
      "latency_ms": latency_ms,
      "message": "端点可连接，但当前协议或视觉请求不受支持",
  }
  ```

  Keep explicit 401/403 as `bad_key`, 404 as `not_found`, 429 as `rate_limited`, and server errors as `http_error`.

  Add a regression where Responses `GET /models` itself returns 400. Assert `rejected_authed`, assert the visual POST was never sent, and assert the result does not claim `visual_payload_rejected`. Add probe cases where a visual POST returns allow-listed `invalid_api_key` or `model_not_found`; assert `bad_key` or `not_found` respectively without exposing the provider body.

  Replace the duplicated inline code tuples in `perception/processor.py::_run_omni_probe` and `admin/router.py::retry_omni_probe` with:

  ```python
  cat = category_for_code(code)
  ```

  Import `category_for_code` from `error_classifier.py`. This is required so `visual_payload_rejected` opens `OPEN_CONFIG` instead of entering recoverable retry backoff.

- [ ] **Step 9: Add runtime regression tests**

  In `test_omni_client_circuit.py`, add one mocked Chat visual payload returning 400 and a text-only 400:

  ```python
  visual_payload = {
      **_payload(),
      "crops": [{"media_type": "image/jpeg", "data": "eA=="}],
  }
  monkeypatch.setattr(
      omni_client.httpx,
      "AsyncClient",
      _fake_async_client(resp=_FakeResp(400)),
  )
  for _ in range(3):
      with pytest.raises(omni_client.OmniError):
          await omni_client.call_omni(visual_payload, _cfg())
  assert get_omni_circuit_breaker().snapshot().code == "visual_payload_rejected"
  ```

  Reset the breaker, repeat three failures with `_payload()` and assert `rejected_authed`. In `test_responses_provider.py`, add the same assertion for `_call_omni_messages` using this internal IR block:

  ```python
  {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,eA=="}}
  ```

  The core assertions are:

  ```python
  snapshot = get_omni_circuit_breaker().snapshot()
  assert snapshot.code == "visual_payload_rejected"
  assert "API Key" not in snapshot.message
  ```

  Record three consecutive failures to reach the existing test/production threshold; do not change production thresholds.

  Add one automatic-tick test in `test_omni_probe_tick_drive.py` and one manual-retry test in `test_omni_config_preflight.py`. In each, make the fake probe return:

  ```python
  {
      "ok": False,
      "code": "visual_payload_rejected",
      "message": "端点可连接，但当前协议或视觉请求不受支持",
  }
  ```

  Assert `CircuitState.OPEN_CONFIG`, `health.state == "error"`, and `health.code == "visual_payload_rejected"`.

- [ ] **Step 10: Run Task 3 verification and commit**

  Run:

  ```bash
  cd backend && uv run pytest -q \
    miloco/tests/perception/engine/omni/test_probe.py \
    miloco/tests/perception/engine/omni/test_error_classifier.py \
    miloco/tests/perception/engine/omni/test_omni_client_circuit.py \
    miloco/tests/perception/engine/omni/test_responses_provider.py \
    miloco/tests/perception/test_omni_probe_tick_drive.py \
    miloco/tests/admin/test_omni_config_preflight.py
  cd backend && uv run ruff check \
    miloco/src/miloco/perception/engine/omni/probe.py \
    miloco/src/miloco/perception/engine/omni/provider.py \
    miloco/src/miloco/perception/engine/omni/error_classifier.py \
    miloco/src/miloco/perception/engine/omni/omni_client.py \
    miloco/src/miloco/perception/engine/omni/omni.py \
    miloco/src/miloco/perception/processor.py \
    miloco/src/miloco/admin/router.py \
    miloco/tests/perception/engine/omni/test_probe.py \
    miloco/tests/perception/engine/omni/test_error_classifier.py \
    miloco/tests/perception/engine/omni/test_omni_client_circuit.py \
    miloco/tests/perception/engine/omni/test_responses_provider.py \
    miloco/tests/perception/test_omni_probe_tick_drive.py \
    miloco/tests/admin/test_omni_config_preflight.py
  git diff --check
  ```

  Commit:

  ```bash
  git add backend/miloco/src/miloco/perception/engine/omni/probe.py \
    backend/miloco/src/miloco/perception/engine/omni/provider.py \
    backend/miloco/src/miloco/perception/engine/omni/error_classifier.py \
    backend/miloco/src/miloco/perception/engine/omni/omni_client.py \
    backend/miloco/src/miloco/perception/engine/omni/omni.py \
    backend/miloco/src/miloco/perception/processor.py \
    backend/miloco/src/miloco/admin/router.py \
    backend/miloco/tests/perception/engine/omni/test_probe.py \
    backend/miloco/tests/perception/engine/omni/test_error_classifier.py \
    backend/miloco/tests/perception/engine/omni/test_omni_client_circuit.py \
    backend/miloco/tests/perception/engine/omni/test_responses_provider.py \
    backend/miloco/tests/perception/test_omni_probe_tick_drive.py \
    backend/miloco/tests/admin/test_omni_config_preflight.py
  git commit -m "fix(omni): require visual-capable preflight"
  ```

---

### Task 4: Stream current Omni health into the model table and localise the new code

**Files:**
- Modify: `web/src/api/index.ts:568-585`
- Modify: `web/src/lib/types.ts:456-487, 529-539`
- Modify: `web/src/components/OmniHealthBanner.tsx:74-145`
- Modify: `web/src/components/UsageOmniConfig.tsx:39-121, 222-320, 646-682`
- Modify: `web/src/i18n/locales/zh/omniHealth.json`
- Modify: `web/src/i18n/locales/en/omniHealth.json`
- Modify: `web/src/i18n/locales/zh/usage.json`
- Modify: `web/src/i18n/locales/en/usage.json`
- Modify: `web/tests/omni-protocol-form.test.ts`
- Modify: `web/tests/i18n.test.ts`

**Interfaces:**
- Consumes: the existing single `OmniHealthBanner` SSE subscription and `OmniConfigState.active.health`.
- Produces: browser event `miloco:omni-health-updated`; `applyOmniHealth(state, health)`; live replacement of the model table's health snapshot; localised `visual_payload_rejected` copy.

- [ ] **Step 1: Write RED policy and health-reducer tests**

  Extend `web/tests/omni-protocol-form.test.ts`:

  ```ts
  import { readFileSync } from "node:fs";
  import {
    applyOmniHealth,
    omniProtocolFormPolicy,
  } from "@/components/UsageOmniConfig";
  import type { OmniConfigState, OmniHealth } from "@/lib/types";

  function fixtureConfigState(): OmniConfigState {
    const health: OmniHealth = {
      state: "ok", code: null, message: "", since_ms: 0,
      consecutive_failures: 0, next_probe_at_ms: null,
      next_probe_in_seconds: null, last_probe_at_ms: null,
      last_probe_result: null, retry_cooldown_sec: 5,
      retry_available_in_seconds: null,
    };
    return {
      active: {
        model: "vision", base_url: "http://local/v1",
        api_protocol: "openai_responses", protocol_inferred: false,
        api_key_masked: "", has_key: true, health,
      },
      profiles: [{
        label: "active", model: "vision", base_url: "http://local/v1",
        api_protocol: "openai_responses", protocol_inferred: false,
        api_key_masked: "", has_key: true, active: true,
      }],
    };
  }

  it.each(["openai_chat_completions", "openai_responses", "gemini_native"] as const)(
    "%s labels its connection test as a visual preflight",
    (protocol) => {
      expect(omniProtocolFormPolicy(protocol).testLabelKey).toBe("usage.testVisualPreflight");
    },
  );

  it("replaces the active health snapshot with the latest SSE health", () => {
    const state = fixtureConfigState();
    const health: OmniHealth = {
      ...state.active.health,
      state: "error",
      code: "visual_payload_rejected",
      message: "端点可连接，但当前协议或视觉请求不受支持",
      consecutive_failures: 4,
    };
    const updated = applyOmniHealth(state, health);
    expect(updated?.active.health).toEqual(health);
    expect(updated?.profiles).toBe(state.profiles);
  });

  it("wires the single SSE owner to the model-page health reducer", () => {
    const banner = readFileSync(
      new URL("../src/components/OmniHealthBanner.tsx", import.meta.url),
      "utf8",
    );
    const usage = readFileSync(
      new URL("../src/components/UsageOmniConfig.tsx", import.meta.url),
      "utf8",
    );
    expect(banner).toContain("OMNI_HEALTH_UPDATED_EVENT");
    expect(banner).toContain("new CustomEvent<OmniHealth>");
    expect(usage).toContain("applyOmniHealth(current, health)");
  });
  ```

  Add `visual_payload_rejected` to the explicit i18n key expectations in `web/tests/i18n.test.ts`.

- [ ] **Step 2: Run the focused web tests and confirm RED**

  Run:

  ```bash
  cd web && npm test -- tests/omni-protocol-form.test.ts tests/i18n.test.ts
  ```

  Expected: fail because the health code, policy, reducer, and translations do not exist.

- [ ] **Step 3: Extend the frontend health contract**

  Add `"visual_payload_rejected"` to `OmniHealth.code` and the documented `OmniTestResult` code list in `web/src/lib/types.ts`.

  Export one event name from `web/src/api/index.ts`:

  ```ts
  export const OMNI_HEALTH_UPDATED_EVENT = "miloco:omni-health-updated";
  ```

- [ ] **Step 4: Publish every SSE health update to the model page**

  In `OmniHealthBanner`, import the new constant and wrap the existing callback:

  ```tsx
  useEffect(() => {
    return subscribeOmniHealth((next) => {
      setHealth(next);
      window.dispatchEvent(
        new CustomEvent<OmniHealth>(OMNI_HEALTH_UPDATED_EVENT, { detail: next }),
      );
    }, () => {
      window.dispatchEvent(new Event(OMNI_CONFIG_STALE_EVENT));
    });
  }, []);
  ```

  This keeps one SSE subscription and uses the browser event only as an in-process fan-out.

- [ ] **Step 5: Apply live health to `UsageOmniConfig`**

  Export this pure reducer:

  ```ts
  export function applyOmniHealth(
    state: OmniConfigState | null,
    health: OmniHealth,
  ): OmniConfigState | null {
    if (!state) return null;
    return {
      ...state,
      active: { ...state.active, health },
    };
  }
  ```

  Add a separate effect beside the existing stale-config listener:

  ```tsx
  useEffect(() => {
    const onHealth = (event: Event) => {
      const health = (event as CustomEvent<OmniHealth>).detail;
      if (!health) return;
      setState((current) => applyOmniHealth(current, health));
    };
    window.addEventListener(OMNI_HEALTH_UPDATED_EVENT, onHealth);
    return () => window.removeEventListener(OMNI_HEALTH_UPDATED_EVENT, onHealth);
  }, []);
  ```

  Retain the existing table branch that renders non-OK active health before `rowTestResults`; it becomes correct once the snapshot is live.

- [ ] **Step 6: Make all protocol test buttons describe what they prove**

  Change `omniProtocolFormPolicy` so `testLabelKey` is always visual:

  ```ts
  return {
    keyRequired: !responses,
    mediaCopyKey: responses ? "usage.responsesImageSequence" : "usage.videoAudioPerception",
    audioCopyKey: responses ? "usage.responsesNoCameraAudio" : null,
    testLabelKey: "usage.testVisualPreflight",
    samplingControlsVisible: !responses,
  } as const;
  ```

  Add the new test-result map entry but do not add it to `TEST_WARN_CODES`; a visual-capability failure must render red and block activation:

  ```ts
  visual_payload_rejected: "usage.testVisualPayloadRejected",
  ```

- [ ] **Step 7: Add exact Chinese and English copy**

  Add to both `omniHealth.codes` objects:

  ```json
  "visual_payload_rejected": "端点可连接，但当前协议或视觉请求不受支持"
  ```

  ```json
  "visual_payload_rejected": "The endpoint is reachable, but the selected protocol or visual request is unsupported"
  ```

  Add to both `usage` objects:

  ```json
  "testVisualPayloadRejected": "端点可连接，但当前协议或视觉请求不受支持"
  ```

  ```json
  "testVisualPayloadRejected": "The endpoint is reachable, but the selected protocol or visual request is unsupported"
  ```

  Keep the existing `rejected_authed` strings for non-visual legacy failures.

- [ ] **Step 8: Run Task 4 verification and commit**

  Run:

  ```bash
  cd web && npm test -- tests/omni-protocol-form.test.ts tests/omniHealth.test.ts tests/i18n.test.ts
  cd web && npm run typecheck
  cd web && npm run build
  git diff --check
  ```

  Commit:

  ```bash
  git add web/src/api/index.ts web/src/lib/types.ts \
    web/src/components/OmniHealthBanner.tsx web/src/components/UsageOmniConfig.tsx \
    web/src/i18n/locales/zh/omniHealth.json web/src/i18n/locales/en/omniHealth.json \
    web/src/i18n/locales/zh/usage.json web/src/i18n/locales/en/usage.json \
    web/tests/omni-protocol-form.test.ts web/tests/i18n.test.ts
  git commit -m "fix(web): show current visual model health"
  ```

---

### Task 5: Run complete local verification and freeze one release candidate SHA

**Files:**
- Modify: `docs/2026-08-29-production-runtime-diagnosis_PROGRESS.md`
- Read: all files changed in Tasks 1-4

**Interfaces:**
- Consumes: the four independently committed repair units.
- Produces: one clean immutable candidate SHA and one archive/receipt that will be reused unchanged by both AI-lab hosts and production.

- [ ] **Step 1: Run all focused regression suites together**

  Run:

  ```bash
  cd web && npm test -- \
    tests/perception-camera-view.test.ts \
    tests/rtsp-polling.test.ts \
    tests/watch-mse.test.js \
    tests/watch-page.test.ts \
    tests/omni-protocol-form.test.ts \
    tests/omniHealth.test.ts \
    tests/i18n.test.ts
  cd backend && uv run pytest -q \
    miloco/tests/perception/engine/omni/test_probe.py \
    miloco/tests/perception/engine/omni/test_error_classifier.py \
    miloco/tests/perception/engine/omni/test_omni_client_circuit.py \
    miloco/tests/perception/engine/omni/test_responses_provider.py \
    miloco/tests/perception/test_omni_probe_tick_drive.py \
    miloco/tests/admin/test_omni_config_preflight.py \
    miloco/tests/integration/test_responses_perception.py \
    miloco/tests/integration/test_rtsp_live_view.py
  ```

  Expected: pass. A failure blocks release freezing.

- [ ] **Step 2: Run full web and repository checks**

  Run:

  ```bash
  cd web && npm test
  cd web && npm run typecheck
  cd web && npm run build
  ./scripts/local-ci.sh --tests
  git diff --check
  ```

  Record macOS platform exclusions separately. Do not convert an unexecuted or excluded check into a pass.

- [ ] **Step 3: Review privacy and scope mechanically**

  Run:

  ```bash
  git diff 6fae37a...HEAD --check
  git diff --name-only 6fae37a...HEAD
  rg -n -i 'rtsp://|authorization:|bearer [a-z0-9]|api[_ -]?key.{0,20}(sk-|secret)' \
    web/src web/public backend/miloco/src backend/miloco/tests web/tests \
    --glob '!**/vendor/**' --glob '!jmuxer.min.js'
  ```

  Inspect every match. Test-only synthetic values may remain; real endpoints, credentials, frames, and auth headers must be absent.

- [ ] **Step 4: Update the implementation milestone before freezing**

  Append one timestamped milestone to `docs/2026-08-29-production-runtime-diagnosis_PROGRESS.md` recording:

  ```text
  Current work: Scheme A implementation and local verification complete.
  Expected result: all three repairs pass focused and full local checks without production access.
  Result: Achieved or Partial, with exact command outcomes and platform exclusions.
  Next step: build one immutable candidate and deploy it sequentially to ai-lab01 and ai-lab02.
  ```

  Do not include endpoints, camera IDs, credentials, image data, or provider bodies.

- [ ] **Step 5: Commit the release milestone and verify a clean tree**

  Run:

  ```bash
  git add docs/2026-08-29-production-runtime-diagnosis_PROGRESS.md
  git commit -m "docs(progress): record runtime repair implementation"
  test -z "$(git status --porcelain)"
  candidate_sha="$(git rev-parse HEAD)"
  test "${#candidate_sha}" -eq 40
  printf '%s\n' "$candidate_sha"
  ```

  The printed value becomes the single candidate SHA for Tasks 6 and 7. Do not commit again until production acceptance is complete or explicitly abandoned.

- [ ] **Step 6: Build the immutable archive once**

  Run from the clean candidate tree:

  ```bash
  ./deploy.sh build
  ```

  Expected: `dist/lab/<candidate_sha>/miloco-lab-<candidate_sha>.tar.gz` and its receipt exist. Do not rebuild after either lab deployment.

---

### Task 6: Deploy the exact candidate sequentially to both AI-lab hosts

**Files:**
- Read only: immutable candidate archive and receipt from Task 5
- Write outside Git: deployment output and acceptance evidence in a safe temporary directory

**Interfaces:**
- Consumes: one exact clean candidate SHA, one immutable archive/receipt, and `/Users/nicholasliao/.ssh/id_co_openclaw`.
- Produces: lab01 then lab02 deploy/verify/status evidence; explicit `measured` or `not_measured` labels for browser, real camera, and real provider proof.

- [ ] **Step 1: Create a non-secret evidence directory and capture the candidate identity**

  Run:

  ```bash
  evidence_dir="$(mktemp -d)"
  candidate_sha="$(git rev-parse HEAD)"
  test -f "dist/lab/$candidate_sha/miloco-lab-$candidate_sha.tar.gz"
  ```

  Keep the directory path private to the execution log; do not store secrets in it.

- [ ] **Step 2: Preflight, deploy, verify, and inspect `ai-lab01.esxi`**

  Run in this order:

  ```bash
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw ./deploy.sh preflight ai-lab01.esxi
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw ./deploy.sh deploy ai-lab01.esxi
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw ./deploy.sh verify ai-lab01.esxi
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw ./deploy.sh status ai-lab01.esxi
  curl --silent --show-error --fail --max-time 5 http://ai-lab01.esxi:1810/health
  curl --silent --show-error --fail --max-time 5 http://ai-lab01.esxi:1810/watch-mse.js | \
    rg 'createReadyJmuxer|jmuxer_ready_timeout|jmuxer_cancelled'
  ```

  Expected: exact candidate SHA, healthy container, fixture acceptance pass, and the readiness helper served from the built wheel.

- [ ] **Step 3: Deploy the same archive to `ai-lab02.esxi`**

  Only after lab01 passes, run:

  ```bash
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw ./deploy.sh preflight ai-lab02.esxi
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw ./deploy.sh deploy ai-lab02.esxi
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw ./deploy.sh verify ai-lab02.esxi
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw ./deploy.sh status ai-lab02.esxi
  curl --silent --show-error --fail --max-time 5 http://ai-lab02.esxi:1810/health
  curl --silent --show-error --fail --max-time 5 http://ai-lab02.esxi:1810/watch-mse.js | \
    rg 'createReadyJmuxer|jmuxer_ready_timeout|jmuxer_cancelled'
  ```

  Expected: the same candidate SHA and archive identity as lab01. Do not rebuild or change commits between hosts.

- [ ] **Step 4: Classify lab evidence honestly**

  Record:

  ```text
  fixture_rtsp_pipeline: measured by immutable acceptance image
  fixture_responses_pipeline: measured by immutable acceptance image
  packaged_watch_mse_helper: measured by HTTP asset readback
  real_browser_mse_playback: not_measured unless a non-secret lab RTSP source is actually available
  real_rtsp_camera: not_measured unless explicitly configured in lab
  real_local_vlm: not_measured unless explicitly configured in lab
  ```

  Do not copy production RTSP credentials or the production model key into lab to convert a `not_measured` item into a pass.

- [ ] **Step 5: Stop on a lab failure without touching production**

  Allow at most two recovery attempts for one failure class. If the same class fails twice, record the first failing boundary and stop. Production Task 7 is blocked until both hosts report the same healthy candidate SHA or the user explicitly narrows acceptance.

---

### Task 7: Open a new production CO and deploy the lab-proven candidate to `docker.esxi`

**Files:**
- Read only: candidate archive/receipt, current production deployment status, approved spec and plan
- Write outside Git: ITSM CO payload/receipt in a safe temporary directory

**Interfaces:**
- Consumes: the unchanged candidate SHA from Tasks 5-6, current production rollback SHA, the `itsm-co` skill, and user-performed API-key submission.
- Produces: a separately governed exact-SHA production deployment, Responses profile activation, live camera/model evidence, rollback if required, and an accurately closed CO.

- [ ] **Step 1: Read the current production state before requesting mutation**

  Use the existing deployment status operation read-only:

  ```bash
  candidate_sha="$(git rev-parse HEAD)"
  status_output="$(
    MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw \
      ./deploy.sh status docker.esxi
  )"
  printf '%s\n' "$status_output"
  rollback_sha="$(
    sed -n 's/.* current=\([0-9a-f]\{40\}\) .*/\1/p' <<<"$status_output"
  )"
  [[ "$rollback_sha" =~ ^[0-9a-f]{40}$ ]]
  test -f "dist/lab/$candidate_sha/miloco-lab-$candidate_sha.tar.gz"
  ```

  Record the parsed full SHA as `rollback_sha`. Confirm the candidate archive still exists and its receipt matches `candidate_sha`.

- [ ] **Step 2: Use `itsm-co` to create the exact production change**

  Create a production CO for service `Miloco`, target `docker.esxi/root`, port `1811`, immutable `candidate_sha`, and these explicit change steps:

  ```text
  1. Preflight and deploy the existing immutable candidate archive.
  2. Verify container health and exact SHA.
  3. User saves a new or edited active Omni profile using openai_responses and re-enters the existing key through the authenticated UI.
  4. Verify unified perception count, direct-HTTP RTSP playback, visual preflight, and one real runtime inference.
  5. Roll back to rollback_sha and the former active model profile if acceptance fails.
  ```

  The CO must state that the deployment script will not read, copy, or print the saved API key.

- [ ] **Step 3: Wait for the CO/PAM gate**

  Poll with the `itsm-co` skill. Continue only when `state=Implement` and PAM is active. If the CO is denied, close `Not Executed` and stop. If user approval is requested, stop for the user; do not bypass the gate.

- [ ] **Step 4: Verify the exact deploy gate**

  Use the skill's receipt/payload verification with:

  ```text
  expected SHA = candidate_sha
  host = docker.esxi
  user = root
  service = Miloco
  port = 1811
  ```

  A mismatch stops the change before SSH mutation.

- [ ] **Step 5: Deploy and verify the exact candidate**

  Run:

  ```bash
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw ./deploy.sh preflight docker.esxi
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw ./deploy.sh deploy docker.esxi
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw ./deploy.sh verify docker.esxi
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw ./deploy.sh status docker.esxi
  curl --silent --show-error --fail --max-time 5 http://docker.esxi:1811/health
  ```

  Expected: healthy container on `candidate_sha`. Do not rebuild the SHA and do not remove historical images.

- [ ] **Step 6: Ask the user to perform the protocol/key submission**

  In the authenticated Miloco UI, the user selects `OpenAI Responses`, keeps the current model identifier and Base URL, and re-enters the existing API key. The agent does not retrieve the current key from production state.

  If the user asks the agent to type a Vault-backed password or key into the browser, request action-time confirmation naming `http://docker.esxi:1811` and the exact purpose before transmission. Never echo the value.

- [ ] **Step 7: Run production acceptance through the actual UI and runtime**

  Using the in-app browser and safe read-only API/log checks, prove all of these:

  ```text
  perception_backend_count == dashboard live-perception count
  both configured RTSP cards are present in the live-perception area
  MIoT-only controls are absent from RTSP cards
  each RTSP video has readyState >= 2, non-zero videoWidth/videoHeight, and an advancing currentTime
  visual preflight returns ok through openai_responses
  the active profile runtime health remains ok after one real perception request
  no current green test is displayed over a later runtime error
  ```

  Do not report WebSocket receipt alone as video proof. Do not expose camera IDs, endpoints, credentials, frames, or response bodies in the user-facing record.

- [ ] **Step 8: Roll back if any mandatory acceptance fails**

  Under the same active CO/PAM window:

  ```bash
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw \
    ./deploy.sh rollback docker.esxi "$rollback_sha"
  MILOCO_SSH_IDENTITY=/Users/nicholasliao/.ssh/id_co_openclaw ./deploy.sh verify docker.esxi
  ```

  Reactivate the former saved model profile through the UI. Do not delete RTSP sources, Xiaomi account data, or stored profiles.

- [ ] **Step 9: Close the CO accurately**

  Close `Successfully Closed` only if all mandatory production acceptance checks pass. Close `Failed` if mutation occurred and rollback did not fully restore service. Close `Not Executed` if the gate was never reached or no mutation occurred. Include exact deployed/rollback SHA and structural results, never secrets.

---

### Task 8: Record deployment evidence and preserve the deployed-SHA distinction

**Files:**
- Modify: `docs/2026-08-29-production-runtime-diagnosis_PROGRESS.md`
- Modify: `docs/2026-08-29-docker-esxi-deployment_PROGRESS.md`

**Interfaces:**
- Consumes: lab evidence, production CO result, deployed candidate SHA, and any rollback result.
- Produces: a docs-only closeout commit that explicitly distinguishes production SHA from later repository HEAD.

- [ ] **Step 1: Append the lab and production milestones**

  Before editing documentation, preserve the deployed identity:

  ```bash
  candidate_sha="$(git rev-parse HEAD)"
  [[ "$candidate_sha" =~ ^[0-9a-f]{40}$ ]]
  ```

  Append separate timestamped lab and production entries to `docs/2026-08-29-production-runtime-diagnosis_PROGRESS.md`, and append the production release or rollback milestone to `docs/2026-08-29-docker-esxi-deployment_PROGRESS.md`. Record this exact evidence structure:

  ```text
  Candidate SHA:
  Lab01 result:
  Lab02 result:
  Fixture RTSP/Responses evidence:
  Real browser/RTSP/local-VLM evidence with measured or not_measured labels:
  Production CO number and final state:
  Production deployed SHA or restored rollback SHA:
  Perception count result:
  Browser playback result:
  Visual preflight result:
  Runtime inference/health result:
  Security/log review result:
  Remaining limitations:
  ```

  Use concrete values from execution. Omit all secrets and frame content.

- [ ] **Step 2: Verify documentation and commit without redeploying**

  Run:

  ```bash
  git diff --check
  git status --short
  git add docs/2026-08-29-production-runtime-diagnosis_PROGRESS.md \
    docs/2026-08-29-docker-esxi-deployment_PROGRESS.md
  git commit -m "docs(deploy): record runtime repair rollout"
  deployed_sha="$candidate_sha"
  docs_head="$(git rev-parse HEAD)"
  test "$deployed_sha" != "$docs_head"
  git status --short
  ```

  Report both SHAs. The docs-only HEAD is not deployed and must not be described as the runtime SHA.

- [ ] **Step 3: Final completion criteria**

  The work is complete only when:

  ```text
  unified perception UI: passed in production
  direct-HTTP RTSP playback: passed with advancing decoded video
  visual preflight: passed through openai_responses
  runtime model inference: passed and breaker health is current/healthy
  production CO: accurately closed
  worktree: clean
  upstream push: not performed because no authorised writable remote exists
  ```

  If production did not execute or any mandatory proof remains missing, report the work as partially complete with the exact first failing boundary.
