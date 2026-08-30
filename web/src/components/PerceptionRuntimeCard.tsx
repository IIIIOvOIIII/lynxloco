import { useTranslation } from "react-i18next";

import {
  derivePerceptionRuntimeView,
  selectRuntimeDisplayWindow,
} from "@/lib/perceptionRuntime";
import type { PerceptionRuntimeSummary } from "@/lib/types";

function cardToneClass(tone: "ok" | "info" | "warn" | "danger"): string {
  switch (tone) {
    case "ok":
      return "border-success/25 bg-success-bg/40";
    case "warn":
      return "border-warning/30 bg-warning-bg/45";
    case "danger":
      return "border-error/30 bg-error-bg/45";
    case "info":
      return "border-info/25 bg-info-bg/35";
  }
}

function dotClass(tone: "ok" | "info" | "warn" | "danger"): string {
  switch (tone) {
    case "ok":
      return "bg-success";
    case "warn":
      return "bg-warning";
    case "danger":
      return "bg-error";
    case "info":
      return "bg-info";
  }
}

export function PerceptionRuntimeCard(props: {
  summary: PerceptionRuntimeSummary | null | undefined;
  loading?: boolean;
  error?: Error | null;
  onReload?: () => void;
}): React.JSX.Element | null {
  const { summary, loading = false, error = null, onReload } = props;
  const { t } = useTranslation();

  if (!summary && !loading && !error) return null;

  if (error && !summary) {
    return (
      <section className="px-5 md:px-8 pt-3">
        <div className="rounded-2xl border border-warning/30 bg-warning-bg/45 px-4 py-3 text-body">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="font-medium text-text-primary">
                {t("perceptionRuntime.title")}
              </div>
              <div className="mt-1 text-text-secondary">
                {t("perceptionRuntime.loadFailed")}
              </div>
            </div>
            {onReload && (
              <button
                type="button"
                className="rounded-lg border border-border px-3 py-1.5 text-text-secondary hover:bg-bg-secondary"
                onClick={onReload}
              >
                {t("perceptionRuntime.reload")}
              </button>
            )}
          </div>
        </div>
      </section>
    );
  }

  if (loading && !summary) {
    return (
      <section className="px-5 md:px-8 pt-3">
        <div className="rounded-2xl border border-border bg-bg-secondary px-4 py-3">
          <div className="h-4 w-32 animate-pulse rounded bg-border" />
          <div className="mt-3 h-3 w-2/3 animate-pulse rounded bg-border" />
        </div>
      </section>
    );
  }

  if (!summary) return null;

  const view = derivePerceptionRuntimeView(summary);
  const window = selectRuntimeDisplayWindow(summary);
  const classification = summary.latestOmni?.response.classification;

  return (
    <section className="px-5 md:px-8 pt-3">
      <div
        className={`rounded-2xl border px-4 py-3 text-body ${cardToneClass(
          view.tone,
        )}`}
      >
        <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span
                aria-hidden
                className={`h-2 w-2 rounded-full ${dotClass(view.tone)}`}
              />
              <span className="font-medium text-text-primary">
                {t(view.titleKey)}
              </span>
            </div>
            <div className="mt-1 text-text-secondary">
              {t(view.detailKey)}
            </div>
          </div>
          {onReload && (
            <button
              type="button"
              className="self-start rounded-lg border border-border px-3 py-1.5 text-text-secondary hover:bg-bg-secondary"
              onClick={onReload}
            >
              {t("perceptionRuntime.reload")}
            </button>
          )}
        </div>
        <div className="mt-3 grid gap-1 text-caption text-text-secondary md:grid-cols-3">
          <div>
            {t("perceptionRuntime.sourcesLine", {
              count: summary.sources.activeCount,
            })}
          </div>
          <div>
            {t("perceptionRuntime.summaryLine", {
              inferences: summary.logs.todayInferenceCount,
              raw: summary.logs.rawLastHour,
              events: summary.logs.meaningfulLastHour,
            })}
          </div>
          <div>
            {window
              ? t("perceptionRuntime.modelLine", {
                  minutes: window.minutes,
                  calls: window.omniCallCount,
                  errors: window.omniErrorCount,
                })
              : t("perceptionRuntime.modelLineEmpty")}
          </div>
        </div>
        {classification && (
          <div className="mt-2 text-caption text-text-tertiary">
            {t("perceptionRuntime.classification", {
              state: t(
                `perceptionRuntime.classifications.${classification}`,
                String(classification),
              ),
            })}
          </div>
        )}
      </div>
    </section>
  );
}
