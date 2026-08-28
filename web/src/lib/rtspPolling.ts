import type { CameraSummary } from "./types";

export const RTSP_FAST_POLL_LIMIT = 10;
export const RTSP_FAST_POLL_MS = 1_000;
export const RTSP_SLOW_POLL_MS = 30_000;
export const RTSP_RECOVERY_BACKOFF_MS = [2_000, 5_000, 10_000] as const;

const TERMINAL_ERROR_CODES = new Set([
  "authentication_failed",
  "invalid_uri",
  "no_video_stream",
  "no_video_track",
  "unsupported_video_codec",
  "resource_not_found",
]);

export interface RtspPollingPlan {
  mode: "fast" | "recovery" | "slow";
  delayMs: number;
  cameraIds: string[];
}

export interface RtspPollingContext {
  staleError: boolean;
  recoveryAttempt: number;
}

export interface RtspRefreshCoordinator {
  poll: () => Promise<void>;
  afterMutation: () => Promise<void>;
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

export function createRtspRefreshCoordinator(
  reload: () => Promise<void>,
): RtspRefreshCoordinator {
  const state: { current: Promise<void> | null } = { current: null };
  let requestedRevision = 0;
  let refreshedRevision = 0;
  let trailing: Promise<void> | null = null;
  const poll = () => runSingleFlight(state, reload);

  const afterMutation = (): Promise<void> => {
    requestedRevision += 1;
    if (!trailing) {
      trailing = (async () => {
        do {
          const olderRequest = state.current;
          if (olderRequest) {
            try {
              await olderRequest;
            } catch {
              // A failed pre-mutation request cannot replace the required
              // post-mutation snapshot.
            }
          }
          const coveredRevision = requestedRevision;
          await poll();
          refreshedRevision = coveredRevision;
        } while (refreshedRevision < requestedRevision);
      })().finally(() => {
        trailing = null;
      });
    }
    return trailing;
  };

  return { poll, afterMutation };
}

export function cameraSummaryAvailability(
  data: CameraSummary[] | undefined,
  error: Error | undefined,
): { fatalError: Error | undefined; stale: boolean } {
  return {
    fatalError: data === undefined ? error : undefined,
    stale: data !== undefined && error !== undefined,
  };
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
  context: RtspPollingContext = { staleError: false, recoveryAttempt: 0 },
): RtspPollingPlan | null {
  if (!visible) return null;
  const enabled = cameras.filter(
    (camera) => camera.sourceType === "rtsp" && camera.enabled,
  );
  if (
    enabled.length > 0 &&
    context.staleError &&
    context.recoveryAttempt < RTSP_RECOVERY_BACKOFF_MS.length
  ) {
    return {
      mode: "recovery",
      delayMs: RTSP_RECOVERY_BACKOFF_MS[context.recoveryAttempt],
      cameraIds: [],
    };
  }
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
