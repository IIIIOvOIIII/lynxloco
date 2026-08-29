import { describe, expect, it } from "vitest";

import { dispatchOmniHealthUpdated } from "@/api";
import {
  applyOmniHealth,
  omniDiscoveryRequest,
  omniProfileIdentity,
  omniProtocolSelection,
  omniProtocolFormPolicy,
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

function fixtureConfigState(): OmniConfigState {
  return {
    active: {
      label: "active",
      model: "vision",
      base_url: "http://local/v1",
      api_protocol: "openai_responses",
      protocol_inferred: false,
      api_key_masked: "",
      has_key: true,
      health: fixtureHealth(),
    },
    profiles: [
      {
        label: "active",
        model: "vision",
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
  it("replaces only the active health snapshot with the latest runtime health", () => {
    const state = fixtureConfigState();
    const health = fixtureHealth({
      state: "error",
      code: "visual_payload_rejected",
      message: "visual request unsupported",
      since_ms: 1_725_000_000_000,
      consecutive_failures: 4,
      last_probe_at_ms: 1_725_000_000_000,
      last_probe_result: "fail",
    });
    const updated = applyOmniHealth(state, health);
    expect(updated?.active.health).toEqual(health);
    expect(updated?.profiles).toBe(state.profiles);
  });

  it("accepts recovery updates and leaves an unloaded configuration unloaded", () => {
    const state = fixtureConfigState();
    state.active.health = fixtureHealth({
      state: "error",
      code: "visual_payload_rejected",
      message: "visual request unsupported",
    });
    const recovered = fixtureHealth({
      last_probe_at_ms: 1_725_000_000_001,
      last_probe_result: "ok",
    });
    expect(applyOmniHealth(state, recovered)?.active.health).toEqual(recovered);
    expect(applyOmniHealth(null, recovered)).toBeNull();
  });

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
