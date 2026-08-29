function sessionError(code, message) {
  return Object.assign(new Error(message), { code });
}

export function createReadyJmuxer({
  JMuxer,
  node,
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
    instance?.destroy?.();
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
      onError,
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
      destroyOnce();
      if (!readySettled) {
        readySettled = true;
        rejectReady(sessionError("jmuxer_cancelled", "JMuxer readiness was cancelled"));
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
  readyMuxer.feed({ video: firstKeyNAL, duration: 40 });
  return readyMuxer;
}
