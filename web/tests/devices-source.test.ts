import { afterEach, describe, expect, it, vi } from "vitest";

import {
  realControlDeviceProp,
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

function mockFetchByUrl(matches: Record<string, unknown>) {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  globalThis.fetch = vi.fn(
    async (request: RequestInfo | URL, init?: RequestInit) => {
      const url = request.toString();
      calls.push({ url, init });
      for (const [key, body] of Object.entries(matches)) {
        if (url.includes(key)) {
          return new Response(JSON.stringify(body), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
      }
      return new Response(JSON.stringify({ code: 404, message: "not found" }), {
        status: 404,
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

  it("shows Home Assistant climate fan mode as a controllable device property", async () => {
    mockFetchByUrl({
      "/api/devices/home": {
        code: 0,
        message: "ok",
        data: {
          homes: [],
          scenes: [],
          devices: [
            {
              did: "ha:primary:climate.zhonghong_hvac_1_0",
              name: "客厅空调",
              category: "climate",
              room: "客厅",
              online: true,
              source: "home_assistant",
              source_label: "Home Assistant",
              included: true,
              control_enabled: true,
              spec: {
                state: {
                  iid: "state",
                  description: "当前状态",
                  format: "string",
                  readable: true,
                  writeable: false,
                },
                fan_mode: {
                  iid: "fan_mode",
                  description: "风速",
                  format: "string",
                  readable: true,
                  writeable: true,
                  value_list: [
                    { value: "auto", description: "auto" },
                    { value: "low", description: "low" },
                    { value: "medium", description: "medium" },
                    { value: "high", description: "high" },
                    { value: "silent", description: "silent" },
                  ],
                },
              },
            },
          ],
        },
      },
      "/api/devices/ha%3Aprimary%3Aclimate.zhonghong_hvac_1_0/status": {
        code: 0,
        message: "ok",
        data: { properties: [{ iid: "fan_mode", value: "high", code: 0 }] },
      },
    });

    const devices = await realListDevices();

    expect(devices[0]?.props).toContainEqual(
      expect.objectContaining({
        iid: "fan_mode",
        label: "风速",
        type: "enum",
        value: "high",
        options: [
          { label: "auto", value: "auto" },
          { label: "low", value: "low" },
          { label: "medium", value: "medium" },
          { label: "high", value: "high" },
          { label: "silent", value: "silent" },
        ],
      }),
    );
  });

  it("uses the unified control endpoint for Home Assistant device properties", async () => {
    const calls = mockFetchByUrl({
      "/api/devices/ha%3Aprimary%3Aclimate.zhonghong_hvac_1_0/control": {
        code: 0,
        message: "ok",
        data: { success: true },
      },
    });

    await realControlDeviceProp(
      "ha:primary:climate.zhonghong_hvac_1_0",
      "fan_mode",
      "high",
    );

    expect(calls[0]?.url).toBe(
      "/api/devices/ha%3Aprimary%3Aclimate.zhonghong_hvac_1_0/control",
    );
    expect(calls[0]?.init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0]?.init?.body))).toEqual({
      type: "set_property",
      iid: "fan_mode",
      value: "high",
    });
  });
});
