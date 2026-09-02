import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it, vi } from "vitest";

import {
  RTSP_FAST_POLL_LIMIT,
  RTSP_RECOVERY_BACKOFF_MS,
  cameraSummaryAvailability,
  createRtspRefreshCoordinator,
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
    perceptionPrompt: "",
    ...overrides,
  };
}

describe("bounded RTSP camera status refresh", () => {
  it("runs a fresh GET after a mutation even when an older GET was in flight", async () => {
    let finishOld!: () => void;
    let finishFresh!: () => void;
    let snapshot = camera({ enabled: false, connected: false });
    const reload = vi
      .fn<() => Promise<void>>()
      .mockImplementationOnce(
        () =>
          new Promise<void>((resolve) => {
            finishOld = () => {
              snapshot = camera({ enabled: false, connected: false });
              resolve();
            };
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise<void>((resolve) => {
            finishFresh = () => {
              snapshot = camera({ enabled: true, connected: true });
              resolve();
            };
          }),
      );
    const refresh = createRtspRefreshCoordinator(reload);

    const oldGet = refresh.poll();
    await Promise.resolve();
    expect(reload).toHaveBeenCalledTimes(1);
    const afterMutation = refresh.afterMutation();
    finishOld();
    await oldGet;
    await Promise.resolve();
    expect(reload).toHaveBeenCalledTimes(2);
    finishFresh();
    await afterMutation;

    expect(snapshot).toMatchObject({ enabled: true, connected: true });
  });

  it("coalesces mutations waiting behind the same old GET", async () => {
    let finishOld!: () => void;
    let finishFresh!: () => void;
    const reload = vi
      .fn<() => Promise<void>>()
      .mockImplementationOnce(
        () =>
          new Promise<void>((resolve) => {
            finishOld = resolve;
          }),
      )
      .mockImplementationOnce(
        () =>
          new Promise<void>((resolve) => {
            finishFresh = resolve;
          }),
      );
    const refresh = createRtspRefreshCoordinator(reload);

    const oldGet = refresh.poll();
    await Promise.resolve();
    const firstMutation = refresh.afterMutation();
    const secondMutation = refresh.afterMutation();
    expect(secondMutation).toBe(firstMutation);
    finishOld();
    await oldGet;
    await Promise.resolve();
    expect(reload).toHaveBeenCalledTimes(2);
    finishFresh();
    await Promise.all([firstMutation, secondMutation]);
    expect(reload).toHaveBeenCalledTimes(2);
  });

  it("does not let a failed old GET swallow the trailing mutation refresh", async () => {
    let failOld!: (error: Error) => void;
    const reload = vi
      .fn<() => Promise<void>>()
      .mockImplementationOnce(
        () =>
          new Promise<void>((_resolve, reject) => {
            failOld = reject;
          }),
      )
      .mockResolvedValueOnce();
    const refresh = createRtspRefreshCoordinator(reload);

    const oldGet = refresh.poll();
    await Promise.resolve();
    const afterMutation = refresh.afterMutation();
    failOld(new Error("old request failed"));
    await expect(oldGet).rejects.toThrow("old request failed");
    await afterMutation;
    expect(reload).toHaveBeenCalledTimes(2);
  });

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

  it("uses short bounded recovery delays for a stale connected snapshot", () => {
    const connected = camera({ connected: true, videoCodec: "h264" });
    expect(
      rtspPollingPlan([connected], {}, true, {
        staleError: true,
        recoveryAttempt: 0,
      }),
    ).toMatchObject({ mode: "recovery", delayMs: RTSP_RECOVERY_BACKOFF_MS[0] });
    expect(
      rtspPollingPlan([connected], {}, true, {
        staleError: true,
        recoveryAttempt: RTSP_RECOVERY_BACKOFF_MS.length,
      }),
    ).toMatchObject({ mode: "slow" });
  });

  it("keeps a connected snapshot non-fatal while marking it stale", () => {
    const rtsp = camera({ connected: true, videoCodec: "h264" });
    const miot = camera({
      id: "miot:1",
      sourceType: "miot",
      name: "Kitchen",
      hasPassword: false,
    });
    const error = new Error("private response detail");

    expect(cameraSummaryAvailability(undefined, error)).toEqual({
      fatalError: error,
      stale: false,
    });
    expect(cameraSummaryAvailability([miot, rtsp], error)).toEqual({
      fatalError: undefined,
      stale: true,
    });
    expect(isRtspLiveReady(rtsp)).toBe(true);
    expect(rtspPollingPlan([rtsp], {}, true)).toMatchObject({ mode: "slow" });
    expect(
      rtspPollingPlan([rtsp], {}, true, {
        staleError: true,
        recoveryAttempt: 0,
      }),
    ).toMatchObject({ mode: "recovery" });
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

  it("App wires visibility cleanup, freshness modes, and manual refresh", () => {
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
    expect(app).toContain("createRtspRefreshCoordinator(cameraSummaries.reload)");
    expect(app).toContain("refreshRtsp.afterMutation()");
    expect(app).toContain("cameraSummaryState.fatalError");
    expect(app).toContain("cameraStatusStale={cameraSummaryState.stale}");
    expect(app).toContain("onRefreshRtsp=");
    expect(hero).toContain("onRefreshRtsp");
    expect(hero).toContain("cameraStatusStale");
  });
});
