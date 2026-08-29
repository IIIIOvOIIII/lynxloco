import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createReadyJmuxer,
  feedFirstAccessUnitWhenReady,
  handleCurrentSocketTerminalClose,
} from "../public/watch-mse.js";

let options;
let instance;

class FakeJMuxer {
  constructor(value) {
    options = value;
    instance = this;
  }

  feed = vi.fn();
  destroy = vi.fn();
}

class ThrowingDestroyJMuxer extends FakeJMuxer {
  destroy = vi.fn(() => { throw new Error("destroy failed"); });
}

afterEach(() => {
  vi.useRealTimers();
  options = undefined;
  instance = undefined;
});

describe("createReadyJmuxer", () => {
  it("does not resolve or feed before onReady", async () => {
    let resolved = false;
    const session = createReadyJmuxer({ JMuxer: FakeJMuxer, node: {} });
    session.ready.then(() => { resolved = true; });
    await Promise.resolve();
    expect(resolved).toBe(false);
    expect(instance.feed).not.toHaveBeenCalled();
    options.onReady();
    expect(await session.ready).toBe(instance);
  });

  it("times out once and destroys the partial instance", async () => {
    vi.useFakeTimers();
    const session = createReadyJmuxer({
      JMuxer: FakeJMuxer,
      node: {},
      timeoutMs: 3000,
    });
    const rejection = expect(session.ready).rejects.toMatchObject({
      code: "jmuxer_ready_timeout",
    });
    await vi.advanceTimersByTimeAsync(3000);
    await rejection;
    expect(instance.destroy).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
    options.onReady();
    expect(instance.destroy).toHaveBeenCalledOnce();
  });

  it("still rejects the timeout once when destroy throws", async () => {
    vi.useFakeTimers();
    const session = createReadyJmuxer({
      JMuxer: ThrowingDestroyJMuxer,
      node: {},
      timeoutMs: 3000,
    });
    const rejection = expect(session.ready).rejects.toMatchObject({
      code: "jmuxer_ready_timeout",
    });

    await vi.advanceTimersByTimeAsync(3000);
    await rejection;
    expect(instance.destroy).toHaveBeenCalledOnce();
  });

  it("cancels once and ignores a late ready callback", async () => {
    const session = createReadyJmuxer({ JMuxer: FakeJMuxer, node: {} });
    const rejection = expect(session.ready).rejects.toMatchObject({
      code: "jmuxer_cancelled",
    });
    session.cancel();
    await rejection;
    options.onReady();
    expect(instance.destroy).toHaveBeenCalledOnce();
  });

  it("still rejects cancellation once when destroy throws", async () => {
    vi.useFakeTimers();
    const session = createReadyJmuxer({
      JMuxer: ThrowingDestroyJMuxer,
      node: {},
    });
    const rejection = expect(session.ready).rejects.toMatchObject({
      code: "jmuxer_cancelled",
    });

    expect(() => session.cancel()).not.toThrow();
    await rejection;
    session.cancel();
    expect(instance.destroy).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("destroys a ready instance when the player is later torn down", async () => {
    const session = createReadyJmuxer({ JMuxer: FakeJMuxer, node: {} });
    options.onReady();
    await session.ready;
    session.cancel();
    session.cancel();
    expect(instance.destroy).toHaveBeenCalledOnce();
  });

  it("clears the readiness timer when JMuxer construction throws", () => {
    vi.useFakeTimers();
    class ThrowingJMuxer {
      constructor() { throw new Error("constructor failed"); }
    }
    expect(() => createReadyJmuxer({ JMuxer: ThrowingJMuxer, node: {} })).toThrow(
      "constructor failed",
    );
    expect(vi.getTimerCount()).toBe(0);
  });

  it("ignores a late error from a timed-out session after retry", async () => {
    vi.useFakeTimers();
    const errors = [];
    const generation = 7;
    const playerGeneration = generation;
    let activeSession;
    let firstSession;
    firstSession = createReadyJmuxer({
      JMuxer: FakeJMuxer,
      node: {},
      timeoutMs: 3000,
      isCurrent: () => (
        generation === playerGeneration && activeSession === firstSession
      ),
      onError: (error) => errors.push(error),
    });
    activeSession = firstSession;
    const firstOptions = options;
    const timeout = expect(firstSession.ready).rejects.toMatchObject({
      code: "jmuxer_ready_timeout",
    });
    await vi.advanceTimersByTimeAsync(3000);
    await timeout;

    let secondSession;
    secondSession = createReadyJmuxer({
      JMuxer: FakeJMuxer,
      node: {},
      isCurrent: () => (
        generation === playerGeneration && activeSession === secondSession
      ),
      onError: (error) => errors.push(error),
    });
    activeSession = secondSession;
    const secondOptions = options;
    secondOptions.onReady();
    await secondSession.ready;

    firstOptions.onError("late session error");
    secondOptions.onError("current session error");

    expect(errors).toEqual(["current session error"]);
    secondSession.cancel();
  });
});

describe("feedFirstAccessUnitWhenReady", () => {
  it("feeds the captured access unit exactly once and only after onReady", async () => {
    const firstKeyNAL = new Uint8Array([0, 0, 0, 1, 0x65]);
    const session = createReadyJmuxer({ JMuxer: FakeJMuxer, node: {} });

    const configured = feedFirstAccessUnitWhenReady({
      session,
      firstKeyNAL,
    });
    await Promise.resolve();
    expect(instance.feed).not.toHaveBeenCalled();

    options.onReady();
    expect(await configured).toBe(instance);
    expect(instance.feed).toHaveBeenCalledOnce();
    expect(instance.feed).toHaveBeenCalledWith({
      video: firstKeyNAL,
      duration: 40,
    });

    options.onReady();
    await Promise.resolve();
    expect(instance.feed).toHaveBeenCalledOnce();
  });

  it("rejects a stale player generation without feeding", async () => {
    let currentGeneration = 4;
    const session = createReadyJmuxer({ JMuxer: FakeJMuxer, node: {} });
    const configured = feedFirstAccessUnitWhenReady({
      session,
      firstKeyNAL: new Uint8Array([0, 0, 0, 1, 0x65]),
      isCurrent: () => currentGeneration === 4,
    });

    currentGeneration = 5;
    options.onReady();

    await expect(configured).rejects.toMatchObject({ code: "jmuxer_cancelled" });
    expect(instance.feed).not.toHaveBeenCalled();
    expect(instance.destroy).toHaveBeenCalledOnce();
  });

  it("destroys the session and preserves a first-feed error", async () => {
    const feedError = new Error("first feed failed");
    class ThrowingFeedJMuxer extends FakeJMuxer {
      feed = vi.fn(() => { throw feedError; });
    }
    const session = createReadyJmuxer({
      JMuxer: ThrowingFeedJMuxer,
      node: {},
    });
    const configured = feedFirstAccessUnitWhenReady({
      session,
      firstKeyNAL: new Uint8Array([0, 0, 0, 1, 0x65]),
    });

    options.onReady();

    await expect(configured).rejects.toBe(feedError);
    expect(instance.feed).toHaveBeenCalledOnce();
    expect(instance.destroy).toHaveBeenCalledOnce();
  });
});

describe("handleCurrentSocketTerminalClose", () => {
  it("cancels pending readiness before preserving the terminal reason", async () => {
    vi.useFakeTimers();
    const socket = {};
    let generation = 4;
    const closedGeneration = generation;
    let status = "connecting";
    let fpsHandle = 17;
    let videoSource = "blob:active-player";
    const session = createReadyJmuxer({ JMuxer: FakeJMuxer, node: {} });
    const configureResult = session.ready.catch((error) => {
      if (closedGeneration !== generation || error?.code === "jmuxer_cancelled") return;
      status = error.code;
    });

    const handled = handleCurrentSocketTerminalClose({
      closingSocket: socket,
      currentSocket: socket,
      cleanup: () => {
        generation += 1;
        session.cancel();
        fpsHandle = null;
        videoSource = "";
      },
      showReason: () => { status = "specific stream failure"; },
    });

    await configureResult;
    await vi.advanceTimersByTimeAsync(3000);
    options.onReady();
    await Promise.resolve();

    expect(handled).toBe(true);
    expect(generation).toBe(5);
    expect(instance.destroy).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
    expect(fpsHandle).toBeNull();
    expect(videoSource).toBe("");
    expect(status).toBe("specific stream failure");
  });

  it("destroys ready playback and still shows the reason when cleanup throws", async () => {
    const socket = {};
    let status = "playing";
    let fpsHandle = 23;
    let videoPaused = false;
    let videoSource = "blob:ready-player";
    const session = createReadyJmuxer({
      JMuxer: ThrowingDestroyJMuxer,
      node: {},
    });
    options.onReady();
    await session.ready;

    const handled = handleCurrentSocketTerminalClose({
      closingSocket: socket,
      currentSocket: socket,
      cleanup: () => {
        session.cancel();
        fpsHandle = null;
        videoPaused = true;
        videoSource = "";
        throw new Error("transient cleanup failure");
      },
      showReason: () => { status = "camera disabled"; },
    });

    expect(handled).toBe(true);
    expect(instance.destroy).toHaveBeenCalledOnce();
    expect(fpsHandle).toBeNull();
    expect(videoPaused).toBe(true);
    expect(videoSource).toBe("");
    expect(status).toBe("camera disabled");
  });

  it("ignores an old socket close after reconnect", () => {
    const oldSocket = {};
    const currentSocket = {};
    let generation = 9;
    let cleanupCalls = 0;
    let status = "new player active";

    const handled = handleCurrentSocketTerminalClose({
      closingSocket: oldSocket,
      currentSocket,
      cleanup: () => {
        cleanupCalls += 1;
        generation += 1;
      },
      showReason: () => { status = "old socket closed"; },
    });

    expect(handled).toBe(false);
    expect(generation).toBe(9);
    expect(cleanupCalls).toBe(0);
    expect(status).toBe("new player active");
  });
});
