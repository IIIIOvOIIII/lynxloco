import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { OmniConcurrencyField } from "@/components/UsageOmniConfig";
import { realUpdateOmniConfig } from "@/api/real";
import i18n from "@/i18n";

const originalFetch = globalThis.fetch;
afterEach(() => {
  vi.restoreAllMocks();
  globalThis.fetch = originalFetch;
});

describe("model concurrency setting", () => {
  it("renders the saved value with an accessible label and integer limits", async () => {
    await i18n.changeLanguage("zh");
    const html = renderToStaticMarkup(
      <OmniConcurrencyField value="4" onChange={() => {}} />,
    );
    expect(html).toContain("模型并发数");
    expect(html).toContain('aria-label="模型并发数"');
    expect(html).toContain('type="number"');
    expect(html).toContain('value="4"');
    expect(html).toContain('min="1"');
    expect(html).toContain('max="8"');
    expect(html).toContain('step="1"');
    expect(html).toContain('aria-describedby="omni-concurrency-hint"');
  });

  it("sends the edited concurrency without activating or testing the model", async () => {
    const requests: { url: string; body: Record<string, unknown> }[] = [];
    globalThis.fetch = vi.fn(async (url, init) => {
      requests.push({ url: String(url), body: JSON.parse(String(init?.body)) });
      return new Response(JSON.stringify({ code: 0, message: "ok", data: {} }), {
        status: 200, headers: { "Content-Type": "application/json" },
      });
    });
    await realUpdateOmniConfig({
      label: "active", original_label: "active", model: "vision",
      base_url: "https://model.example/v1", api_protocol: "openai_responses",
      concurrency: 8, activate: false,
    });
    expect(requests).toEqual([{
      url: "/api/admin/omni-config",
      body: {
        label: "active", original_label: "active", model: "vision",
        base_url: "https://model.example/v1", api_protocol: "openai_responses",
        concurrency: 8, activate: false,
      },
    }]);
  });
});
