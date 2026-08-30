import { describe, expect, it } from "vitest";

import { TABS } from "@/components/Sidebar";
import enNav from "@/i18n/locales/en/nav.json";
import zhNav from "@/i18n/locales/zh/nav.json";

describe("Home Assistant navigation", () => {
  it("adds a dedicated sidebar tab with localized labels", () => {
    expect(TABS.map((tab) => tab.key)).toContain("homeAssistant");
    expect(zhNav.nav.homeAssistant).toBe("Home Assistant");
    expect(enNav.nav.homeAssistantHint).toContain("Manage HA");
  });
});
