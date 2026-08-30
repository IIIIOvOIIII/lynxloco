import { afterEach, describe, expect, it, vi } from "vitest";

import {
  realListDevices,
  realListHomeAssistantEntities,
} from "@/api/real";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
  vi.restoreAllMocks();
});

function mockNormal(data: unknown) {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  globalThis.fetch = vi.fn(
    async (request: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ url: request.toString(), init });
      return new Response(JSON.stringify({ code: 0, message: "ok", data }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    },
  ) as unknown as typeof fetch;
  return calls;
}

describe("Home Assistant API mapping", () => {
  it("maps snake_case HA entity fields to camelCase", async () => {
    const calls = mockNormal([
      {
        entity_id: "light.kitchen",
        friendly_name: "厨房灯",
        domain: "light",
        state: "off",
        included: true,
        control_enabled: false,
        control_supported: true,
        control_blocked_reason: null,
        last_seen_at: null,
        last_control_at: null,
        last_error: null,
      },
    ]);

    const rows = await realListHomeAssistantEntities();

    expect(calls[0]?.url).toBe("/api/home-assistant/entities");
    expect(rows[0]).toMatchObject({
      entityId: "light.kitchen",
      name: "厨房灯",
      controlEnabled: false,
      controlSupported: true,
    });
  });
});

describe("unified device API mapping", () => {
  it("preserves HA source and control metadata on devices", async () => {
    mockNormal({
      homes: [],
      scenes: [],
      devices: [
        {
          did: "ha:primary:light.kitchen",
          name: "厨房灯",
          category: "light",
          room: "厨房",
          online: true,
          status_text: "关闭",
          status_kind: "off",
          dangerous: false,
          main_switch: { iid: "on", current: false },
          props: [],
          source: "home_assistant",
          source_label: "Home Assistant",
          included: true,
          control_enabled: false,
          read_only_reason: "control-disabled",
        },
      ],
    });

    const devices = await realListDevices();

    expect(devices[0]).toMatchObject({
      did: "ha:primary:light.kitchen",
      source: "home_assistant",
      sourceLabel: "Home Assistant",
      controlAvailable: false,
      controlPolicy: "read_only",
      readOnlyReason: "control-disabled",
    });
  });
});
