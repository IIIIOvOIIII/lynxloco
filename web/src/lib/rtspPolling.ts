import type { CameraSummary } from "./types";

export const RTSP_FAST_POLL_LIMIT = 10;
export const RTSP_FAST_POLL_MS = 1_000;
export const RTSP_SLOW_POLL_MS = 30_000;

const TERMINAL_ERROR_CODES = new Set([
  "authentication_failed",
  "invalid_uri",
  "no_video_stream",
  "no_video_track",
  "unsupported_video_codec",
  "resource_not_found",
]);

export interface RtspPollingPlan {
  mode: "fast" | "slow";
  delayMs: number;
  cameraIds: string[];
}

export function runSingleFlight<T>(
  state: { current: Promise<T> | null },
  task: () => Promise<T>,
): Promise<T> {
  if (state.current) return state.current;
  let tracked: Promise<T>;
  tracked = Promise.resolve()
    .then(task)
    .finally(() => {
      if (state.current === tracked) state.current = null;
    });
  state.current = tracked;
  return tracked;
}

export function isTerminalRtspError(code: string | null): boolean {
  return code !== null && TERMINAL_ERROR_CODES.has(code);
}

export function isRtspLiveReady(camera: CameraSummary): boolean {
  return camera.sourceType === "rtsp" && camera.enabled && camera.connected;
}

export function rtspPollingPlan(
  cameras: CameraSummary[],
  fastAttempts: Readonly<Record<string, number>>,
  visible: boolean,
): RtspPollingPlan | null {
  if (!visible) return null;
  const enabled = cameras.filter(
    (camera) => camera.sourceType === "rtsp" && camera.enabled,
  );
  const fast = enabled.filter(
    (camera) =>
      !camera.connected &&
      !isTerminalRtspError(camera.errorCode) &&
      (fastAttempts[camera.id] ?? 0) < RTSP_FAST_POLL_LIMIT,
  );
  if (fast.length) {
    return {
      mode: "fast",
      delayMs: RTSP_FAST_POLL_MS,
      cameraIds: fast.map((camera) => camera.id),
    };
  }
  const needsSlowRefresh = enabled.some(
    (camera) => camera.connected || !isTerminalRtspError(camera.errorCode),
  );
  return needsSlowRefresh
    ? { mode: "slow", delayMs: RTSP_SLOW_POLL_MS, cameraIds: [] }
    : null;
}
