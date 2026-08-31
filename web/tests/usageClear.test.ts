/**
 * 清除用量数据的请求契约。
 *
 * 这里守的是一件容易出事的事：**model 与 base_url 必须成对给或都不给**。
 * 只给一半时前端直接抛、不发请求：把半个目标丢掉会让「清这一项」静默变成
 * 「清所有模型」，这是这里最坏的失败方向。后端同样对半个目标返 400。
 *
 * 另一条：base_url 空串是**合法目标**（schema v3 之前的老数据，来源未记录），
 * 不是「未指定」。任何按真值判断的写法都会把这类行的定点清除变成全模型清除。
 */

import { describe, it, expect, vi, afterEach } from "vitest";
import { realClearUsageData } from "@/api/real";
import { dailyCaveatApplies } from "@/lib/usageTokens";

const originalFetch = globalThis.fetch;

afterEach(() => {
  vi.restoreAllMocks();
  globalThis.fetch = originalFetch;
});

function captureFetch() {
  const calls: { url: string; body: Record<string, unknown> }[] = [];
  globalThis.fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({
      url: typeof input === "string" ? input : input.toString(),
      body: JSON.parse(String(init?.body ?? "{}")),
    });
    return new Response(JSON.stringify({ code: 0, message: "ok", data: {} }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as unknown as typeof fetch;
  return calls;
}

describe("realClearUsageData 请求体", () => {
  it("无参 = 全清：四个字段都显式为 null，不靠字段缺省", async () => {
    const calls = captureFetch();
    await realClearUsageData();
    expect(calls[0].url).toContain("/api/admin/token-usage/clear");
    expect(calls[0].body).toEqual({ since_ms: null, model: null, base_url: null, from_date: null });
  });

  it("只给时间范围：目标仍为 null（所有模型）", async () => {
    const calls = captureFetch();
    await realClearUsageData({ sinceMs: 1_700_000_000_000 });
    expect(calls[0].body).toEqual({
      since_ms: 1_700_000_000_000,
      model: null,
      base_url: null,
      from_date: null,
    });
  });

  it("定点清除：model 与 base_url 原样带上", async () => {
    const calls = captureFetch();
    await realClearUsageData({
      sinceMs: null,
      model: "mimo-v2.5",
      baseUrl: "https://api.xiaomimimo.com/v1-test",
    });
    expect(calls[0].body).toEqual({
      since_ms: null,
      model: "mimo-v2.5",
      base_url: "https://api.xiaomimimo.com/v1-test",
      from_date: null,
    });
  });

  it("base_url 空串是目标本身，不能退化成「不限 endpoint」", async () => {
    const calls = captureFetch();
    await realClearUsageData({ model: "mimo-v2.5", baseUrl: "" });
    expect(calls[0].body).toEqual({ since_ms: null, model: "mimo-v2.5", base_url: "", from_date: null });
  });

  it("只给一半时抛错且**不发请求**：半个目标绝不能退化成全模型清除", async () => {
    const calls = captureFetch();
    await expect(realClearUsageData({ model: "mimo-v2.5" })).rejects.toThrow(/同时给/);
    await expect(realClearUsageData({ baseUrl: "https://api.x.com/v1" })).rejects.toThrow(
      /同时给/,
    );
    expect(calls).toHaveLength(0);
  });

  it("范围与目标可叠加", async () => {
    const calls = captureFetch();
    await realClearUsageData({ sinceMs: 42, model: "m", baseUrl: "u" });
    expect(calls[0].body).toEqual({
      since_ms: 42,
      model: "m",
      base_url: "u",
      from_date: null,
    });
  });

  it("界面显示的那一天原样带上：日表按盒子时区归日，提示按浏览器时区算", async () => {
    const calls = captureFetch();
    await realClearUsageData({ sinceMs: 1_700_000_000_000, fromDate: "2026-08-24" });
    expect(calls[0].body.from_date).toBe("2026-08-24");
  });

  it("全清不带边界日：没有「从哪天起」这回事", async () => {
    const calls = captureFetch();
    await realClearUsageData({ sinceMs: null, fromDate: "2026-08-24" });
    expect(calls[0].body.from_date).toBeNull();
  });
});

describe("「连带删除某天」这句提示的成立条件", () => {
  // 日表按 `date >= from_date` 整天删，但只有那天真的已经滚进日表才谈得上「连带」。
  // 滚存截止天对齐且只搬更早的行，所以日表最新日期比今天早好几天。
  it("边界日晚于日表最新日期（近 24 小时那一档）→ 不说", () => {
    // 今天 8-28 点「近 24 小时」，边界日 8-27；日表最新只到 8-24
    expect(dailyCaveatApplies("2026-08-27", "2026-08-24")).toBe(false);
  });

  it("边界日正好是日表最新那天 → 要说（那天会被整天删掉）", () => {
    expect(dailyCaveatApplies("2026-08-24", "2026-08-24")).toBe(true);
  });

  it("边界日早于日表最新日期（近 7 天那一档）→ 要说", () => {
    expect(dailyCaveatApplies("2026-08-22", "2026-08-24")).toBe(true);
  });

  it("日表为空 / 接口没给 → 不说，宁可不说也不说错", () => {
    expect(dailyCaveatApplies("2026-08-22", null)).toBe(false);
  });

  it("全清档没有边界日 → 不说", () => {
    expect(dailyCaveatApplies(null, "2026-08-24")).toBe(false);
  });

  it("跨月跨年按字典序比较仍成立（等宽零填充）", () => {
    expect(dailyCaveatApplies("2026-09-01", "2026-08-31")).toBe(false);
    expect(dailyCaveatApplies("2025-12-31", "2026-01-01")).toBe(true);
  });
});
