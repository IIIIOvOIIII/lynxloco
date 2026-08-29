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
    options.onReady();
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
});
