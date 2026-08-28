import { describe, expect, it } from "vitest";

import {
  omniDiscoveryRequest,
  omniProfileIdentity,
  omniProtocolSelection,
  omniProtocolFormPolicy,
} from "@/components/UsageOmniConfig";
import { hasConfiguredOmni } from "@/components/PetAutoGenFlow";

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
    "keeps API key and ordinary test behavior for %s",
    (protocol) => {
      const policy = omniProtocolFormPolicy(protocol);
      expect(policy.keyRequired).toBe(true);
      expect(policy.testLabelKey).toBe("usage.testConnection");
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
