import { beforeEach, describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { PerfKpiCards } from "@/components/PerfKpiCards";
import i18n from "@/i18n";
import type { PerfSummary } from "@/lib/types";

function summary(overrides: Partial<PerfSummary> = {}): PerfSummary {
  return {
    window: { since: 0, until: 1000 },
    cycle_count: 3, dropped_count: 3, skip_rate: 0.1, drop_rate: 0.2,
    omni_error_rate: 0.25, p95_rtf_e2e: 0.8, p95_rtf_omni: 0.6,
    agent_call_count: 2, camera_window_count: 12, expected_window_count: 15,
    legacy_cycle_count: 0, partial_windows_count: 1, omni_request_count: 4,
    omni_request_error_count: 1, omni_success_cycle_count: 2,
    ...overrides,
  };
}
function rendered(data: PerfSummary): string {
  return renderToStaticMarkup(
    <PerfKpiCards state={{ data, loading: false, error: undefined, reload: async () => {} }} />,
  ).replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
}
beforeEach(async () => { await i18n.changeLanguage("en"); });

describe("performance metric definitions", () => {
  it("uses processed camera windows and expected windows independently of batches", () => {
    const text = rendered(summary());
    expect(text).toContain("Camera windows processed 12 Expected 15 Batches 3");
    expect(text).toContain("20.0%");
    expect(text).toContain("Completed agent runs 2");
    expect(text).toContain("actual Omni requests");
    expect(text).toContain("slowest camera");
  });

  it("shows unavailable corrected metrics for history containing only legacy batches", () => {
    const text = rendered(summary({
      camera_window_count: 0, expected_window_count: 0, legacy_cycle_count: 3,
      drop_rate: null, omni_success_cycle_count: 0, omni_request_count: 0,
    }));
    expect(text).toContain("Camera windows processed — Expected — Batches 3");
    expect(text).toContain("Window drop rate —");
    expect(text).toContain("Legacy metrics");
    expect(text).toContain("no corrected data");
    expect(text).not.toContain("Window drop rate 0.0%");
  });

  it("keeps corrected data and displays a warning for mixed history", () => {
    const text = rendered(summary({ legacy_cycle_count: 2 }));
    expect(text).toContain("Camera windows processed 12 Expected 15");
    expect(text).toContain("Legacy metrics");
  });

  it("retains legacy API values only when the corrected fields are absent", () => {
    const data = summary();
    delete data.camera_window_count;
    delete data.expected_window_count;
    delete data.legacy_cycle_count;
    const text = rendered(data);
    expect(text).toContain("Cycles 3 Expected 6");
    expect(text).toContain("Legacy metrics");
  });

  it("does not present successful latency when no actual Omni call succeeded", () => {
    const text = rendered(summary({ omni_success_cycle_count: 0 }));
    expect(text).toContain("Successful RTF P95 —");
    expect(text).toContain("Successful Omni RTF P95 —");
  });
});
