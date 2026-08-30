function sessionError(code, message) {
  return Object.assign(new Error(message), { code });
}

export function createReadyJmuxer({
  JMuxer,
  node,
  isCurrent = () => true,
  onError,
  timeoutMs = 3000,
  timers = globalThis,
}) {
  let instance;
  let readySettled = false;
  let destroyed = false;
  let rejectReady;
  let signalReady;
  let timeoutId;

  const readySignal = new Promise((resolve) => { signalReady = resolve; });
  const destroyOnce = () => {
    if (destroyed) return;
    destroyed = true;
    try { instance?.destroy?.(); } catch {}
  };
  const ready = new Promise((resolve, reject) => {
    rejectReady = reject;
    timeoutId = timers.setTimeout(() => {
      if (readySettled) return;
      readySettled = true;
      try { destroyOnce(); } finally {
        reject(sessionError("jmuxer_ready_timeout", "JMuxer did not become ready in time"));
      }
    }, timeoutMs);
    readySignal.then(() => {
      if (readySettled) return;
      readySettled = true;
      timers.clearTimeout(timeoutId);
      resolve(instance);
    });
  });

  try {
    instance = new JMuxer({
      node,
      mode: "video",
      flushingTime: 0,
      fps: 25,
      debug: false,
      onReady: () => signalReady(),
      onError: (data) => {
        if (isCurrent()) onError?.(data);
      },
    });
  } catch (error) {
    readySettled = true;
    timers.clearTimeout(timeoutId);
    throw error;
  }

  return {
    instance,
    ready,
    cancel() {
      timers.clearTimeout(timeoutId);
      try { destroyOnce(); } finally {
        if (!readySettled) {
          readySettled = true;
          rejectReady(sessionError("jmuxer_cancelled", "JMuxer readiness was cancelled"));
        }
      }
    },
  };
}

export async function feedFirstAccessUnitWhenReady({
  session,
  firstKeyNAL,
  isCurrent = () => true,
}) {
  const readyMuxer = await session.ready;
  if (!isCurrent()) {
    session.cancel();
    throw sessionError("jmuxer_cancelled", "stale player generation");
  }
  try {
    readyMuxer.feed({ video: firstKeyNAL, duration: 40 });
  } catch (error) {
    try { session.cancel(); } catch {}
    throw error;
  }
  return readyMuxer;
}

export function handleCurrentSocketTerminalClose({
  closingSocket,
  currentSocket,
  cleanup,
  showReason,
}) {
  if (closingSocket !== currentSocket) return false;
  try { cleanup(); } catch {}
  showReason();
  return true;
}

export function shouldUseJpegCanvasFallback({
  cameraId,
  isSecureContext,
  hasVideoDecoder,
}) {
  return (
    typeof cameraId === "string"
    && cameraId.startsWith("rtsp:")
    && (!isSecureContext || !hasVideoDecoder)
  );
}

export function shouldReconnectLiveSocket({
  nowMs,
  socketOpen,
  configured,
  lastWsTs,
  lastFrameLocalMs,
  wsQuietTimeoutMs = 45_000,
  decodedStallTimeoutMs = 30_000,
}) {
  if (!socketOpen) return { reconnect: false, reason: null };
  const wsAge = lastWsTs > 0 ? nowMs - lastWsTs : 0;
  if (wsAge > wsQuietTimeoutMs) {
    return { reconnect: true, reason: "ws_quiet" };
  }
  const frameAge = lastFrameLocalMs > 0 ? nowMs - lastFrameLocalMs : 0;
  if (configured && frameAge > decodedStallTimeoutMs) {
    return { reconnect: true, reason: "decode_stalled" };
  }
  return { reconnect: false, reason: null };
}
