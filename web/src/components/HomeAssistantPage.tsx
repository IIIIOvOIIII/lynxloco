import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  getHomeAssistantStatus,
  listHomeAssistantEntities,
  refreshHomeAssistantEntities,
  saveHomeAssistantConfig,
  testHomeAssistantConfig,
  updateHomeAssistantEntityPolicy,
} from "@/api";
import { controlDisabledReason, maskHomeAssistantToken } from "@/lib/homeAssistant";
import type { HomeAssistantEntity, HomeAssistantStatus } from "@/lib/types";
import { Switch } from "./Switch";
import { toast } from "./Toast";

interface Props {
  onDevicesChanged: () => void | Promise<void>;
}

interface FormState {
  enabled: boolean;
  baseUrl: string;
  token: string;
  verifyTls: boolean;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function reasonKey(reason: string | null): string {
  if (!reason) return "homeAssistant.reasonUnknown";
  return `homeAssistant.reason.${reason}`;
}

export function HomeAssistantPage({ onDevicesChanged }: Props) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<HomeAssistantStatus | null>(null);
  const [entities, setEntities] = useState<HomeAssistantEntity[]>([]);
  const [form, setForm] = useState<FormState>({
    enabled: false,
    baseUrl: "",
    token: "",
    verifyTls: true,
  });
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [busyEntity, setBusyEntity] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [testMessage, setTestMessage] = useState<string | null>(null);

  const importedCount = useMemo(
    () => entities.filter((entity) => entity.included).length,
    [entities],
  );
  const controlCount = useMemo(
    () =>
      entities.filter(
        (entity) => entity.included && entity.controlEnabled && entity.controlSupported,
      ).length,
    [entities],
  );

  async function load(refresh = false) {
    setLoading(true);
    setError(null);
    try {
      const nextStatus = await getHomeAssistantStatus();
      setStatus(nextStatus);
      setForm({
        enabled: nextStatus.config.enabled,
        baseUrl: nextStatus.config.baseUrl,
        token: "",
        verifyTls: nextStatus.config.verifyTls,
      });
      if (nextStatus.configured || nextStatus.enabled) {
        const rows = refresh
          ? await refreshHomeAssistantEntities()
          : await listHomeAssistantEntities();
        setEntities(rows);
      } else {
        setEntities([]);
      }
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleTest() {
    const token = form.token.trim();
    if (!token) {
      toast(t("homeAssistant.enterTokenToTest"), "warn");
      return;
    }
    setTesting(true);
    setTestMessage(null);
    try {
      const result = await testHomeAssistantConfig({
        enabled: true,
        baseUrl: form.baseUrl.trim(),
        token,
        verifyTls: form.verifyTls,
      });
      setTestMessage(result.message || (result.ok ? t("homeAssistant.testOk") : t("homeAssistant.testFailed")));
      toast(result.ok ? t("homeAssistant.testOk") : t("homeAssistant.testFailed"), result.ok ? "ok" : "warn");
    } catch (e) {
      const message = errorMessage(e);
      setTestMessage(message);
      toast(message, "warn");
    } finally {
      setTesting(false);
    }
  }

  async function handleSave() {
    setSaving(true);
    setError(null);
    const token = form.token.trim();
    try {
      await saveHomeAssistantConfig({
        enabled: form.enabled,
        baseUrl: form.baseUrl.trim(),
        token: token || null,
        preserveToken: !token && status?.config.tokenConfigured === true,
        verifyTls: form.verifyTls,
      });
      toast(t("homeAssistant.saved"), "ok");
      await load(false);
      await onDevicesChanged();
    } catch (e) {
      const message = errorMessage(e);
      setError(message);
      toast(message, "warn");
    } finally {
      setSaving(false);
    }
  }

  async function handleRefresh() {
    setRefreshing(true);
    setError(null);
    try {
      const rows = await refreshHomeAssistantEntities();
      setEntities(rows);
      toast(t("homeAssistant.refreshed"), "ok");
    } catch (e) {
      const message = errorMessage(e);
      setError(message);
      toast(message, "warn");
    } finally {
      setRefreshing(false);
    }
  }

  async function updateEntity(entity: HomeAssistantEntity, patch: {
    included?: boolean;
    controlEnabled?: boolean;
  }) {
    setBusyEntity(entity.entityId);
    try {
      const updated = await updateHomeAssistantEntityPolicy(entity.entityId, patch);
      setEntities((rows) =>
        rows.map((row) => (row.entityId === updated.entityId ? updated : row)),
      );
      await onDevicesChanged();
      toast(t("homeAssistant.policySaved"), "ok");
    } catch (e) {
      toast(errorMessage(e), "warn");
    } finally {
      setBusyEntity(null);
    }
  }

  const configuredToken = status?.config.tokenConfigured === true;
  const statusTone = status?.connected
    ? "bg-success-bg text-success border-success"
    : status?.configured
      ? "bg-warning-bg text-warning border-warning"
      : "bg-bg-tertiary text-text-tertiary border-border";

  return (
    <section className="space-y-5 anim-in" aria-labelledby="ha-title">
      <div className="flex flex-col gap-1">
        <h2 id="ha-title" className="text-heading text-text-primary">
          {t("homeAssistant.title")}
        </h2>
        <p className="text-body text-text-secondary">
          {t("homeAssistant.subtitle")}
        </p>
      </div>

      <div className="rounded-xl bg-bg-secondary border border-border shadow-sm p-5 space-y-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-title text-text-primary">
              {t("homeAssistant.connection")}
            </div>
            <div className="text-caption text-text-tertiary mt-1">
              {t("homeAssistant.connectionHint")}
            </div>
          </div>
          <span className={`text-caption px-2 py-1 rounded-full border ${statusTone}`}>
            {loading
              ? t("homeAssistant.loading")
              : status?.connected
                ? t("homeAssistant.connected")
                : status?.configured
                  ? t("homeAssistant.configuredButOffline")
                  : t("homeAssistant.notConfigured")}
          </span>
        </div>

        {error && (
          <div className="text-body text-error bg-error-bg border border-error rounded-lg px-3 py-2">
            {error}
          </div>
        )}

        <div className="grid gap-4 md:grid-cols-[1fr_220px]">
          <label className="space-y-1">
            <span className="text-caption text-text-tertiary">
              {t("homeAssistant.baseUrl")}
            </span>
            <input
              value={form.baseUrl}
              onChange={(e) => setForm((cur) => ({ ...cur, baseUrl: e.target.value }))}
              placeholder="http://homeassistant.local:8123"
              className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-body text-text-primary outline-none focus:border-brand-primary"
            />
          </label>
          <label className="space-y-1">
            <span className="text-caption text-text-tertiary">
              {t("homeAssistant.token")}
            </span>
            <input
              value={form.token}
              onChange={(e) => setForm((cur) => ({ ...cur, token: e.target.value }))}
              type="password"
              placeholder={
                configuredToken
                  ? maskHomeAssistantToken(true)
                  : t("homeAssistant.tokenPlaceholder")
              }
              className="w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-body text-text-primary outline-none focus:border-brand-primary"
            />
          </label>
        </div>

        <div className="flex flex-wrap items-center gap-4">
          <label className="inline-flex items-center gap-2 text-body text-text-primary">
            <input
              type="checkbox"
              checked={form.enabled}
              onChange={(e) => setForm((cur) => ({ ...cur, enabled: e.target.checked }))}
            />
            {t("homeAssistant.enableIntegration")}
          </label>
          <label className="inline-flex items-center gap-2 text-body text-text-primary">
            <input
              type="checkbox"
              checked={form.verifyTls}
              onChange={(e) => setForm((cur) => ({ ...cur, verifyTls: e.target.checked }))}
            />
            {t("homeAssistant.verifyTls")}
          </label>
          <span className="text-caption text-text-tertiary">
            {configuredToken
              ? t("homeAssistant.tokenConfigured")
              : t("homeAssistant.tokenMissing")}
          </span>
        </div>

        {testMessage && (
          <div className="text-caption text-text-secondary">{testMessage}</div>
        )}

        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            onClick={handleTest}
            disabled={testing || !form.baseUrl.trim()}
            className="px-3 py-2 rounded-lg border border-border text-body text-text-primary hover:bg-bg-tertiary disabled:opacity-50"
          >
            {testing ? t("homeAssistant.testing") : t("homeAssistant.test")}
          </button>
          <button
            type="button"
            onClick={handleSave}
            disabled={saving || !form.baseUrl.trim()}
            className="px-3 py-2 rounded-lg bg-brand-primary text-white text-body hover:opacity-90 disabled:opacity-50"
          >
            {saving ? t("homeAssistant.saving") : t("homeAssistant.save")}
          </button>
          <button
            type="button"
            onClick={() => load(false)}
            disabled={loading}
            className="px-3 py-2 rounded-lg border border-border text-body text-text-primary hover:bg-bg-tertiary disabled:opacity-50"
          >
            {t("homeAssistant.reload")}
          </button>
        </div>
      </div>

      <div className="rounded-xl bg-bg-secondary border border-border shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-3 px-5 pt-4 pb-3">
          <div>
            <h3 className="text-title text-text-primary">
              {t("homeAssistant.entities")}
            </h3>
            <p className="text-caption text-text-tertiary">
              {t("homeAssistant.entitiesMeta", {
                total: entities.length,
                imported: importedCount,
                control: controlCount,
              })}
            </p>
          </div>
          <button
            type="button"
            onClick={handleRefresh}
            disabled={refreshing || !status?.configured}
            className="px-3 py-2 rounded-lg border border-border text-body text-text-primary hover:bg-bg-tertiary disabled:opacity-50"
          >
            {refreshing ? t("homeAssistant.refreshing") : t("homeAssistant.refresh")}
          </button>
        </div>

        {loading ? (
          <div className="px-5 py-10 text-body text-text-secondary text-center">
            {t("homeAssistant.loading")}
          </div>
        ) : entities.length === 0 ? (
          <div className="px-5 py-10 text-body text-text-secondary text-center">
            {t("homeAssistant.empty")}
          </div>
        ) : (
          <div className="divide-y divide-border">
            {entities.map((entity) => {
              const disabledReason = controlDisabledReason(entity);
              const rowBusy = busyEntity === entity.entityId;
              return (
                <div
                  key={entity.entityId}
                  className="grid gap-3 px-5 py-3 md:grid-cols-[minmax(0,1fr)_150px_120px_120px] md:items-center"
                >
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-body text-text-primary truncate">
                        {entity.name}
                      </span>
                      <span className="text-caption px-1.5 py-0.5 rounded border border-border text-text-tertiary">
                        {entity.domain}
                      </span>
                      {entity.room && (
                        <span className="text-caption text-text-tertiary">
                          {entity.room}
                        </span>
                      )}
                    </div>
                    <div className="text-caption-mono text-text-tertiary truncate mt-0.5">
                      {entity.entityId}
                    </div>
                    {entity.lastError && (
                      <div className="text-caption text-warning mt-1">
                        {entity.lastError}
                      </div>
                    )}
                  </div>
                  <div className="text-caption text-text-secondary">
                    {t("homeAssistant.state", { state: entity.state })}
                  </div>
                  <label className="flex items-center gap-2 text-body text-text-primary">
                    <Switch
                      checked={entity.included}
                      disabled={rowBusy}
                      onChange={() =>
                        updateEntity(entity, {
                          included: !entity.included,
                          controlEnabled: !entity.included ? entity.controlEnabled : false,
                        })
                      }
                      label={t("homeAssistant.importToggle", { name: entity.name })}
                    />
                    {t("homeAssistant.import")}
                  </label>
                  <div className="flex items-center gap-2">
                    <Switch
                      checked={entity.controlEnabled && entity.included}
                      disabled={rowBusy || disabledReason !== null}
                      onChange={() =>
                        updateEntity(entity, {
                          controlEnabled: !entity.controlEnabled,
                        })
                      }
                      label={t("homeAssistant.controlToggle", { name: entity.name })}
                    />
                    <span
                      className={`text-body ${
                        disabledReason ? "text-text-tertiary" : "text-text-primary"
                      }`}
                      title={disabledReason ? t(reasonKey(disabledReason)) : undefined}
                    >
                      {t("homeAssistant.control")}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </section>
  );
}
