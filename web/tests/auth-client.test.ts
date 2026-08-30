import { beforeEach, describe, expect, it, vi } from "vitest";
import { apiFetch, setCsrfToken } from "@/api/client";
import { authHeaders } from "@/api/register";

describe("auth-aware apiFetch", () => {
  beforeEach(() => {
    setCsrfToken(null);
    vi.restoreAllMocks();
  });

  it("uses same-origin credentials and does not send bearer from window token", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ code: 0, data: {} }), { status: 200 })),
    );
    (window as unknown as { __MILOCO_TOKEN__?: string }).__MILOCO_TOKEN__ = "service-token-secret";

    await apiFetch("/api/auth/status");

    const [, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(init.credentials).toBe("same-origin");
    expect(new Headers(init.headers).has("Authorization")).toBe(false);
  });

  it("adds csrf header to unsafe methods after login state is set", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ code: 0, data: {} }), { status: 200 })),
    );
    setCsrfToken("csrf-token");

    await apiFetch("/api/rules", { method: "POST", body: "{}" });

    const [, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    expect(new Headers(init.headers).get("X-Miloco-CSRF")).toBe("csrf-token");
  });

  it("keeps legacy safe reads in cookie mode without csrf or bearer", () => {
    setCsrfToken("csrf-token");

    const headers = new Headers(authHeaders());

    expect(headers.has("X-Miloco-CSRF")).toBe(false);
    expect(headers.has("Authorization")).toBe(false);
  });

  it("keeps legacy multipart writes in cookie mode with csrf but no bearer", () => {
    setCsrfToken("csrf-token");

    const headers = new Headers(authHeaders({ "Content-Type": "application/json" }, "POST"));

    expect(headers.get("X-Miloco-CSRF")).toBe("csrf-token");
    expect(headers.has("Authorization")).toBe(false);
  });
});
