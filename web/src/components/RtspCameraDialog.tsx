import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { realCreateRtspCamera, realEditRtspCamera, realTestRtspCamera } from "@/api/real";
import {
  createRtspDraft,
  editRtspDraft,
  isTestCurrent,
  normalizedRtspInput,
  rtspDraftFingerprint,
  validateRtspCamera,
} from "@/lib/rtspCamera";
import type { CameraSummary, RtspProbeResult, RtspSourceInput } from "@/lib/types";
import { toast } from "./Toast";

interface Props {
  open: boolean;
  camera?: CameraSummary | null;
  onClose: () => void;
  onSaved: () => void | Promise<void>;
}

const fieldClass =
  "w-full rounded-lg border border-border bg-bg-primary px-3 py-2 text-body text-text-primary outline-none focus:border-brand-primary";

export function RtspCameraDialog({ open, camera, onClose, onSaved }: Props) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState<RtspSourceInput>(createRtspDraft);
  const [testedFingerprint, setTestedFingerprint] = useState<string | null>(null);
  const [probe, setProbe] = useState<RtspProbeResult | null>(null);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);

  useEffect(() => {
    if (!open) return;
    setDraft(camera ? editRtspDraft(camera) : createRtspDraft());
    setTestedFingerprint(null);
    setProbe(null);
    setErrors([]);
    setTesting(false);
    setSaving(false);
  }, [camera, open]);

  if (!open) return null;

  const update = <K extends keyof RtspSourceInput>(key: K, value: RtspSourceInput[K]) => {
    setDraft((current) => ({ ...current, [key]: value }));
    setErrors([]);
  };

  const validate = (): RtspSourceInput | null => {
    const normalized = normalizedRtspInput(draft);
    const nextErrors = validateRtspCamera(normalized);
    setErrors(nextErrors);
    return nextErrors.length ? null : normalized;
  };

  const runTest = async () => {
    const input = validate();
    if (!input || testing || saving) return;
    setTesting(true);
    setProbe(null);
    try {
      const result = await realTestRtspCamera(input);
      setProbe(result);
      setTestedFingerprint(rtspDraftFingerprint(input));
      setDraft(input);
    } catch (error) {
      setTestedFingerprint(null);
      toast(error instanceof Error ? error.message : t("rtspCamera.operationFailed"), "warn");
    } finally {
      setTesting(false);
    }
  };

  const save = async () => {
    const input = validate();
    if (!input || saving || testing) return;
    setSaving(true);
    try {
      if (camera) {
        await realEditRtspCamera(camera.id, input);
        toast(t("rtspCamera.updated"), "ok");
      } else {
        await realCreateRtspCamera(input);
        toast(t("rtspCamera.created"), "ok");
      }
      await onSaved();
      onClose();
    } catch (error) {
      toast(error instanceof Error ? error.message : t("rtspCamera.operationFailed"), "warn");
    } finally {
      setSaving(false);
    }
  };

  const busy = testing || saving;
  return (
    <div
      className="fixed inset-0 z-[70] flex items-center justify-center bg-black/45 p-4"
      onClick={busy ? undefined : onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="rtsp-camera-dialog-title"
        className="w-full max-w-xl max-h-[90vh] overflow-y-auto rounded-xl border border-border bg-bg-secondary p-5 shadow-sm"
        onClick={(event) => event.stopPropagation()}
      >
        <h2 id="rtsp-camera-dialog-title" className="text-title text-text-primary mb-1">
          {t(camera ? "rtspCamera.edit" : "rtspCamera.add")}
        </h2>
        {camera && (
          <p className="text-caption text-text-secondary mb-4">
            {t("rtspCamera.redactedEditHint")}
          </p>
        )}

        <div className="grid gap-3 sm:grid-cols-2">
          <label className="text-caption text-text-secondary">
            {t("rtspCamera.name")}
            <input
              className={`${fieldClass} mt-1`}
              value={draft.name}
              autoComplete="off"
              onChange={(event) => update("name", event.target.value)}
            />
          </label>
          <label className="text-caption text-text-secondary">
            {t("rtspCamera.room")}
            <input
              className={`${fieldClass} mt-1`}
              value={draft.room_name}
              autoComplete="off"
              onChange={(event) => update("room_name", event.target.value)}
            />
          </label>
          <label className="text-caption text-text-secondary sm:col-span-2">
            {t("rtspCamera.url")}
            <input
              className={`${fieldClass} mt-1 font-mono`}
              value={draft.uri}
              placeholder="rtsp://camera.local/stream"
              spellCheck={false}
              autoCapitalize="none"
              autoComplete="off"
              onChange={(event) => update("uri", event.target.value)}
            />
          </label>
          <label className="text-caption text-text-secondary">
            {t("rtspCamera.username")}
            <input
              className={`${fieldClass} mt-1`}
              value={draft.username}
              autoCapitalize="none"
              autoComplete="off"
              onChange={(event) => update("username", event.target.value)}
            />
          </label>
          <label className="text-caption text-text-secondary">
            {t("rtspCamera.password")}
            <input
              type="password"
              className={`${fieldClass} mt-1`}
              value={draft.password}
              autoComplete="new-password"
              onChange={(event) => update("password", event.target.value)}
            />
            {camera?.hasPassword && (
              <span className="mt-1 block text-caption text-text-tertiary">
                {t("rtspCamera.passwordPreserve")}
              </span>
            )}
          </label>
          <label className="text-caption text-text-secondary">
            {t("rtspCamera.transport")}
            <select
              className={`${fieldClass} mt-1`}
              value={draft.transport}
              onChange={(event) => update("transport", event.target.value as "tcp" | "udp")}
            >
              <option value="tcp">TCP</option>
              <option value="udp">UDP</option>
            </select>
          </label>
          <label className="flex items-center gap-2 self-end rounded-lg border border-border px-3 py-2 text-body text-text-secondary">
            <input
              type="checkbox"
              checked={draft.audio_enabled}
              onChange={(event) => update("audio_enabled", event.target.checked)}
            />
            {t("rtspCamera.audioGate")}
          </label>
        </div>

        {errors.length > 0 && (
          <ul className="mt-3 space-y-1 text-caption text-error" role="alert">
            {errors.map((key) => <li key={key}>{t(key)}</li>)}
          </ul>
        )}
        {probe && isTestCurrent(normalizedRtspInput(draft), testedFingerprint) && (
          <p className="mt-3 text-caption text-success" role="status">
            {t("rtspCamera.testPassed", {
              codec: probe.videoCodec.toUpperCase(),
              width: probe.width,
              height: probe.height,
              fps: probe.fps,
            })}
          </p>
        )}

        <div className="mt-5 flex flex-wrap justify-end gap-2">
          <button
            type="button"
            className="rounded-lg border border-border px-3 py-2 text-body text-text-secondary hover:text-text-primary disabled:opacity-50"
            disabled={busy}
            onClick={onClose}
          >
            {t("rtspCamera.cancel")}
          </button>
          <button
            type="button"
            className="rounded-lg border border-border px-3 py-2 text-body text-text-primary hover:border-border-strong disabled:opacity-50"
            disabled={busy}
            onClick={() => void runTest()}
          >
            {t(testing ? "rtspCamera.testing" : "rtspCamera.test")}
          </button>
          <button
            type="button"
            className="rounded-lg bg-brand-primary px-3 py-2 text-body text-white disabled:opacity-50"
            disabled={busy}
            onClick={() => void save()}
          >
            {t(saving ? "rtspCamera.saving" : camera ? "rtspCamera.editAction" : "rtspCamera.save")}
          </button>
        </div>
      </div>
    </div>
  );
}
