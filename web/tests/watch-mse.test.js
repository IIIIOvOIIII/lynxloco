import { afterEach, describe, expect, it, vi } from "vitest";
import {
  createReadyJmuxer,
  feedFirstAccessUnitWhenReady,
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
