import type {
  PerceptionRuntimeSummary,
  PerceptionRuntimeWindow,
} from "@/lib/types";

export interface PerceptionRuntimeView {
  tone: "ok" | "info" | "warn" | "danger";
  titleKey: string;
  detailKey: string;
}

const STATE_VIEW: Record<
  PerceptionRuntimeSummary["semanticState"],
  PerceptionRuntimeView
> = {
  inactive: {
    tone: "info",
    titleKey: "perceptionRuntime.inactiveTitle",
    detailKey: "perceptionRuntime.inactiveDetail",
  },
  not_ready: {
    tone: "warn",
    titleKey: "perceptionRuntime.notReadyTitle",
    detailKey: "perceptionRuntime.notReadyDetail",
  },
  no_sources: {
    tone: "info",
    titleKey: "perceptionRuntime.noSourcesTitle",
    detailKey: "perceptionRuntime.noSourcesDetail",
  },
  collecting: {
    tone: "ok",
    titleKey: "perceptionRuntime.collectingTitle",
    detailKey: "perceptionRuntime.collectingDetail",
  },
  eventing: {
    tone: "ok",
    titleKey: "perceptionRuntime.eventingTitle",
    detailKey: "perceptionRuntime.eventingDetail",
  },
  describing: {
    tone: "ok",
    titleKey: "perceptionRuntime.describingTitle",
    detailKey: "perceptionRuntime.describingDetail",
  },
  silent: {
    tone: "warn",
    titleKey: "perceptionRuntime.silentTitle",
    detailKey: "perceptionRuntime.silentDetail",
  },
  degraded: {
    tone: "danger",
    titleKey: "perceptionRuntime.degradedTitle",
    detailKey: "perceptionRuntime.degradedDetail",
  },
};

export function derivePerceptionRuntimeView(
  summary: PerceptionRuntimeSummary | null | undefined,
): PerceptionRuntimeView {
  if (!summary) {
    return {
      tone: "info",
      titleKey: "perceptionRuntime.unknownTitle",
      detailKey: "perceptionRuntime.unknownDetail",
    };
  }
  return STATE_VIEW[summary.semanticState] ?? STATE_VIEW.collecting;
}

export function selectRuntimeDisplayWindow(
  summary: PerceptionRuntimeSummary | null | undefined,
): PerceptionRuntimeWindow | null {
  if (!summary?.windows?.length) return null;
  return (
    summary.windows.find((window) => window.minutes === 15) ??
    summary.windows.at(-1) ??
    null
  );
}
