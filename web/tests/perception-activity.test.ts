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
    expect(hasActivePerceptionCamera([scopeCamera({ inUse: true })], [])).toBe(
      true,
    );
  });
});

describe("hasActiveRtspPerceptionCamera", () => {
  it("ignores MIoT summaries and inactive RTSP summaries", () => {
    expect(
      hasActiveRtspPerceptionCamera([
        summary({
          id: "miot-1",
          sourceType: "miot",
          hasPassword: false,
        }),
        summary({ id: "rtsp:off", enabled: false, connected: true }),
      ]),
    ).toBe(false);
  });
});
