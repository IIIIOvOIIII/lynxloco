import { describe, expect, it } from "vitest";

import {
  clampHomeAssistantPage,
  controlDisabledReason,
  filterHomeAssistantEntities,
  getHomeAssistantBulkTargets,
  maskHomeAssistantToken,
  mergeHomeAssistantBulkUpdates,
  paginateHomeAssistantEntities,
  summarizeHomeAssistantBulkTargets,
  summarizeHomeAssistantSkippedReasons,
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

  it("filters entities by all query terms across searchable fields", () => {
    const rows = [
      entity({
        entityId: "light.kitchen_main",
        name: "厨房主灯",
        domain: "light",
        room: "厨房",
        state: "off",
      }),
      entity({
        entityId: "switch.living_room_plug",
        name: "客厅插座",
        domain: "switch",
        room: "客厅",
        state: "on",
      }),
      entity({
        entityId: "sensor.kitchen_temperature",
        name: "温度",
        domain: "sensor",
        room: "厨房",
        state: "23",
      }),
    ];

    expect(
      filterHomeAssistantEntities(rows, " LIGHT kitchen ").map((row) => row.entityId),
    ).toEqual(["light.kitchen_main"]);
    expect(
      filterHomeAssistantEntities(rows, "厨房 off").map((row) => row.entityId),
    ).toEqual(["light.kitchen_main"]);
    expect(filterHomeAssistantEntities(rows, "garage")).toEqual([]);
  });

  it("paginates and clamps pages with one-based display ranges", () => {
    const rows = Array.from({ length: 26 }, (_, index) =>
      entity({ entityId: `light.test_${index + 1}`, name: `Light ${index + 1}` }),
    );

    expect(clampHomeAssistantPage(0, rows.length, 25)).toBe(1);
    expect(clampHomeAssistantPage(9, rows.length, 25)).toBe(2);

    const page = paginateHomeAssistantEntities(rows, 2, 25);
    expect(page.page).toBe(2);
    expect(page.pages).toBe(2);
    expect(page.startIndex).toBe(26);
    expect(page.endIndex).toBe(26);
    expect(page.items.map((row) => row.entityId)).toEqual(["light.test_26"]);
  });

  it("selects eligible entities for each bulk action", () => {
    const rows = [
      entity({
        entityId: "light.importable",
        included: false,
        controlEnabled: false,
        controlSupported: true,
      }),
      entity({
        entityId: "light.read_only",
        included: true,
        controlEnabled: false,
        controlSupported: true,
      }),
      entity({
        entityId: "lock.blocked",
        included: true,
        controlEnabled: false,
        controlSupported: false,
        controlBlockedReason: "blocked-risk",
      }),
      entity({
        entityId: "switch.enabled",
        included: true,
        controlEnabled: true,
        controlSupported: true,
      }),
    ];

    expect(getHomeAssistantBulkTargets(rows, "import").map((row) => row.entityId)).toEqual([
      "light.importable",
    ]);
    expect(getHomeAssistantBulkTargets(rows, "remove-import").map((row) => row.entityId)).toEqual([
      "light.read_only",
      "lock.blocked",
      "switch.enabled",
    ]);
    expect(getHomeAssistantBulkTargets(rows, "allow-control").map((row) => row.entityId)).toEqual([
      "light.read_only",
    ]);
    expect(getHomeAssistantBulkTargets(rows, "disable-control").map((row) => row.entityId)).toEqual([
      "switch.enabled",
    ]);

    expect(summarizeHomeAssistantBulkTargets(rows)).toEqual({
      import: 1,
      "remove-import": 3,
      "allow-control": 1,
      "disable-control": 1,
    });
  });

  it("summarizes skipped reasons by the most common reason", () => {
    expect(
      summarizeHomeAssistantSkippedReasons([
        { entityId: "lock.front_door", reason: "blocked-risk" },
        { entityId: "lock.back_door", reason: "blocked-risk" },
        { entityId: "sensor.temp", reason: "unsupported-domain" },
      ]),
    ).toEqual({ reason: "blocked-risk", count: 2 });
    expect(summarizeHomeAssistantSkippedReasons([])).toBeNull();
  });

  it("merges policy-only bulk updates without discarding discovered entity details", () => {
    const existing = entity({
      entityId: "switch.living_room_plug",
      name: "客厅插座",
      domain: "switch",
      state: "on",
      room: "客厅",
      included: true,
      controlEnabled: true,
      controlSupported: true,
      lastSeenAt: 1720000000000,
      lastControlAt: 1720000001000,
      lastError: "temporary failure",
    });

    expect(
      mergeHomeAssistantBulkUpdates([existing], [
        {
          entityId: "switch.living_room_plug",
          included: false,
          controlEnabled: false,
        },
      ]),
    ).toEqual([
      {
        ...existing,
        included: false,
        controlEnabled: false,
      },
    ]);
  });
});
