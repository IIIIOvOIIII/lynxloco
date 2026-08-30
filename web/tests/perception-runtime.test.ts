import { describe, expect, it } from "vitest";

import {
  derivePerceptionRuntimeView,
  selectRuntimeDisplayWindow,
} from "@/lib/perceptionRuntime";
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

  it("uses the 15 minute window for model-call display", () => {
    const s = summary("silent");
    s.windows = [
      {
        minutes: 5,
        cycleCount: 5,
        skippedCount: 0,
        videoPassCount: 1,
        audioPassCount: 0,
        holdPassCount: 1,
        omniCallCount: 4,
        omniErrorCount: 0,
        cycleErrorCount: 0,
        droppedWindowsCount: 0,
        overflowCount: 0,
      },
      {
        minutes: 15,
        cycleCount: 20,
        skippedCount: 0,
        videoPassCount: 8,
        audioPassCount: 0,
        holdPassCount: 5,
        omniCallCount: 15,
        omniErrorCount: 1,
        cycleErrorCount: 0,
        droppedWindowsCount: 0,
        overflowCount: 0,
      },
    ];

    expect(selectRuntimeDisplayWindow(s)?.minutes).toBe(15);
  });
});
