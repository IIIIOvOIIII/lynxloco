import { describe, expect, it } from "vitest";

import { dispatchOmniHealthUpdated } from "@/api";
import {
  createOmniConfigRefreshController,
  omniDiscoveryRequest,
  omniProfileIdentity,
  omniProtocolSelection,
  omniProtocolFormPolicy,
  omniSavedProfileTestLabelKey,
  omniTestReason,
} from "@/components/UsageOmniConfig";
import { hasConfiguredOmni } from "@/components/PetAutoGenFlow";
import type { OmniConfigState, OmniHealth } from "@/lib/types";

function fixtureHealth(overrides: Partial<OmniHealth> = {}): OmniHealth {
  return {
    state: "ok",
    code: null,
    message: "",
    since_ms: 0,
    consecutive_failures: 0,
    next_probe_at_ms: null,
    next_probe_in_seconds: null,
    last_probe_at_ms: null,
    last_probe_result: null,
    retry_cooldown_sec: 5,
    retry_available_in_seconds: null,
    ...overrides,
  };
}

function fixtureConfigState(label = "active"): OmniConfigState {
  return {
    active: {
      label,
      model: `${label}-vision`,
      base_url: "http://local/v1",
      api_protocol: "openai_responses",
      protocol_inferred: false,
      api_key_masked: "",
      has_key: true,
      health: fixtureHealth(),
    },
    profiles: [
      {
        label,
        model: `${label}-vision`,
        base_url: "http://local/v1",
        api_protocol: "openai_responses",
        protocol_inferred: false,
        api_key_masked: "",
        has_key: true,
        active: true,
      },
    ],
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("Omni protocol form policy", () => {
  it("makes Responses key optional and presents image-only visual preflight", () => {
    expect(omniProtocolFormPolicy("openai_responses")).toEqual({
      keyRequired: false,
      mediaCopyKey: "usage.responsesImageSequence",
      audioCopyKey: "usage.responsesNoCameraAudio",
      testLabelKey: "usage.testVisualPreflight",
      samplingControlsVisible: false,
    });
  });

  it.each(["openai_chat_completions", "gemini_native"] as const)(
    "keeps API key but labels the %s test as a visual preflight",
    (protocol) => {
      const policy = omniProtocolFormPolicy(protocol);
      expect(policy.keyRequired).toBe(true);
      expect(policy.testLabelKey).toBe("usage.testVisualPreflight");
      expect(policy.samplingControlsVisible).toBe(true);
    },
  );

  it("includes protocol in profile identity", () => {
    const common = { model: "vision", base_url: "http://local/v1" };
    expect(
      omniProfileIdentity({ ...common, api_protocol: "openai_responses" }),
    ).not.toBe(
      omniProfileIdentity({
        ...common,
        api_protocol: "openai_chat_completions",
      }),
    );
  });
});

describe("Omni runtime health fan-out", () => {
  it("dispatches the latest health as the stable browser event detail", () => {
    const target = new EventTarget();
    const health = fixtureHealth({
      state: "error",
      code: "visual_payload_rejected",
      message: "visual request unsupported",
    });
    let received: OmniHealth | null = null;
    target.addEventListener("miloco:omni-health-updated", (event) => {
      received = (event as CustomEvent<OmniHealth>).detail;
    });
    dispatchOmniHealthUpdated(health, target);
    expect(received).toEqual(health);
  });

  it("accepts only the latest authoritative config refresh", async () => {
    const older = deferred<OmniConfigState>();
    const latest = deferred<OmniConfigState>();
    const requests = [older, latest];
    const accepted: OmniConfigState[] = [];
    const failures: unknown[] = [];
    const controller = createOmniConfigRefreshController(
      () => requests.shift()!.promise,
      (state) => accepted.push(state),
      (error) => failures.push(error),
    );
    const oldRun = controller.refresh();
    const latestRun = controller.refresh();

    latest.resolve(fixtureConfigState("current"));
    await latestRun;
    older.resolve(fixtureConfigState("stale"));
    await oldRun;

    expect(accepted.map((state) => state.active.label)).toEqual(["current"]);
    expect(failures).toEqual([]);
  });

  it("reports only the latest failure and ignores invalidated or disposed results", async () => {
    const staleFailure = deferred<OmniConfigState>();
    const currentFailure = deferred<OmniConfigState>();
    const invalidated = deferred<OmniConfigState>();
    const afterDispose = deferred<OmniConfigState>();
    const requests = [staleFailure, currentFailure, invalidated, afterDispose];
    const accepted: OmniConfigState[] = [];
    const failures: unknown[] = [];
    const controller = createOmniConfigRefreshController(
      () => requests.shift()!.promise,
      (state) => accepted.push(state),
      (error) => failures.push(error),
    );
    const staleRun = controller.refresh();
    const currentRun = controller.refresh();
    const currentError = new Error("refresh unavailable");

    currentFailure.reject(currentError);
    await currentRun;
    staleFailure.reject(new Error("stale failure"));
    await staleRun;

    const invalidatedRun = controller.refresh();
    controller.invalidate();
    invalidated.resolve(fixtureConfigState("invalidated"));
    await invalidatedRun;

    const disposedRun = controller.refresh();
    controller.dispose();
    afterDispose.resolve(fixtureConfigState("after-dispose"));
    await disposedRun;

    expect(accepted).toEqual([]);
    expect(failures).toEqual([currentError]);
  });
});

describe("Saved profile visual preflight action", () => {
  it("uses visual-preflight copy unless that exact row is running", () => {
    expect(omniSavedProfileTestLabelKey("active", null)).toBe(
      "usage.testVisualPreflight",
    );
    expect(omniSavedProfileTestLabelKey("active", "other")).toBe(
      "usage.testVisualPreflight",
    );
    expect(omniSavedProfileTestLabelKey("active", "active")).toBe(
      "usage.testing",
    );
  });
});

describe("Omni test result copy", () => {
  it("keeps backend bad_response detail instead of flattening it", () => {
    expect(
      omniTestReason(
        {
          ok: false,
          code: "bad_response",
          message: "Responses 结构化预检返回空文本",
          latency_ms: 26754,
        },
        (key) => `translated:${key}`,
      ),
    ).toBe("Responses 结构化预检返回空文本");
  });

  it("still localizes stable non-diagnostic codes", () => {
    expect(
      omniTestReason(
        {
          ok: false,
          code: "bad_key",
          message: "provider-specific key failure",
        },
        (key) => `translated:${key}`,
      ),
    ).toBe("translated:usage.testBadKey");
  });
});

describe("Responses without an API key", () => {
  it("remains configured for Pet auto-generation preflight", () => {
    expect(
      hasConfiguredOmni({
        api_protocol: "openai_responses",
        has_key: false,
      }),
    ).toBe(true);
    expect(
      hasConfiguredOmni({
        api_protocol: "openai_chat_completions",
        has_key: false,
      }),
    ).toBe(false);
  });
});

describe("Omni protocol model discovery", () => {
  it("clears candidates and discovery status when protocol changes", () => {
    expect(omniProtocolSelection("gemini_native")).toEqual({
      apiProtocol: "gemini_native",
      models: [],
      modelsMsg: null,
      modelsErr: false,
      modelsErrCode: null,
      testResult: null,
    });
  });

  it("builds the next explicit discovery with the selected protocol", () => {
    expect(
      omniDiscoveryRequest(
        "gemini_native",
        " https://proxy.example/v1beta/ ",
        " gemini-key ",
        "legacy-gemini",
      ),
    ).toEqual({
      api_protocol: "gemini_native",
      base_url: "https://proxy.example/v1beta/",
      api_key: "gemini-key",
      label: "legacy-gemini",
    });
  });
});
