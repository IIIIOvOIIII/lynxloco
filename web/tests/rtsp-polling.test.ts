import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";

import {
  RTSP_FAST_POLL_LIMIT,
  isRtspLiveReady,
  rtspPollingPlan,
  runSingleFlight,
} from "@/lib/rtspPolling";
import type { CameraSummary } from "@/lib/types";

function camera(overrides: Partial<CameraSummary> = {}): CameraSummary {
  return {
    id: "rtsp:1",
    sourceType: "rtsp",
    name: "Door",
    roomName: "Entry",
    enabled: true,
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

describe("bounded RTSP camera status refresh", () => {
  it("fast-polls an enabled source until a later response makes live view reachable", () => {
    const pending = camera();
    expect(rtspPollingPlan([pending], {}, true)).toMatchObject({
      mode: "fast",
      cameraIds: [pending.id],
    });

    const connected = camera({ connected: true, videoCodec: "h264" });
    expect(isRtspLiveReady(connected)).toBe(true);
    expect(rtspPollingPlan([connected], { [connected.id]: 1 }, true)).toMatchObject({
      mode: "slow",
      cameraIds: [],
    });
  });

  it("stops fast polling at the bounded budget and falls back to low frequency", () => {
    const pending = camera();
    expect(
      rtspPollingPlan([pending], { [pending.id]: RTSP_FAST_POLL_LIMIT }, true),
    ).toMatchObject({ mode: "slow", cameraIds: [] });
  });

  it.each([
    "authentication_failed",
    "invalid_uri",
    "no_video_stream",
    "no_video_track",
    "unsupported_video_codec",
    "resource_not_found",
  ])("stops polling on stable terminal error %s", (errorCode) => {
    expect(rtspPollingPlan([camera({ errorCode })], {}, true)).toBeNull();
  });

  it("does not schedule while hidden or for disabled-only sources", () => {
    expect(rtspPollingPlan([camera()], {}, false)).toBeNull();
    expect(rtspPollingPlan([camera({ enabled: false })], {}, true)).toBeNull();
  });

  it("coalesces overlapping reloads and permits a later refresh", async () => {
    let finish!: () => void;
    const task = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    );
    const state: { current: Promise<void> | null } = { current: null };

    const first = runSingleFlight(state, task);
    const overlapping = runSingleFlight(state, task);
    expect(overlapping).toBe(first);
    expect(task).toHaveBeenCalledTimes(0);

    await Promise.resolve();
    expect(task).toHaveBeenCalledTimes(1);
    finish();
    await first;
    expect(state.current).toBeNull();

    void runSingleFlight(state, task);
    await Promise.resolve();
    expect(task).toHaveBeenCalledTimes(2);
    finish();
  });

  it("App wires visibility cleanup, overlap protection, reload, and manual refresh", () => {
    const app = readFileSync(
      fileURLToPath(new URL("../src/App.tsx", import.meta.url)),
      "utf8",
    );
    const hero = readFileSync(
      fileURLToPath(new URL("../src/components/HeroNow.tsx", import.meta.url)),
      "utf8",
    );
    expect(app).toContain("rtspPollingPlan(");
    expect(app).toContain('document.addEventListener("visibilitychange"');
    expect(app).toContain('document.removeEventListener("visibilitychange"');
    expect(app).toContain("window.clearTimeout(");
    expect(app).toContain(
      "runSingleFlight(rtspRefreshPromise, cameraSummaries.reload)",
    );
    expect(app).toContain("onRefreshRtsp=");
    expect(hero).toContain("onRefreshRtsp");
  });
});
