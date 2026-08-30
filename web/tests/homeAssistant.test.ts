import { describe, expect, it } from "vitest";

import {
  controlDisabledReason,
  maskHomeAssistantToken,
} from "@/lib/homeAssistant";
import type { HomeAssistantEntity } from "@/lib/types";

function entity(patch: Partial<HomeAssistantEntity>): HomeAssistantEntity {
  return {
    entityId: "light.kitchen",
    name: "厨房灯",
    domain: "light",
    state: "off",
    included: true,
    controlEnabled: false,
    controlSupported: true,
    controlBlockedReason: null,
    lastSeenAt: null,
    lastControlAt: null,
    lastError: null,
    ...patch,
  };
}

describe("Home Assistant helpers", () => {
  it("masks a configured token", () => {
    expect(maskHomeAssistantToken(true)).toBe("••••••••");
    expect(maskHomeAssistantToken(false)).toBe("");
  });

  it("explains blocked control toggle", () => {
    expect(
      controlDisabledReason(
        entity({
          controlSupported: false,
          controlBlockedReason: "blocked-risk",
        }),
      ),
    ).toBe("blocked-risk");
  });

  it("disables control for non-imported entities", () => {
    expect(controlDisabledReason(entity({ included: false }))).toBe(
      "not-imported",
    );
  });
});
