import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  apiFetch,
  getCsrfToken,
  setCsrfToken,
  subscribeSessionExpired,
} from "@/api/client";
import { authHeaders, extractCandidates } from "@/api/register";

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

  it("sends csrf but neither bearer nor JSON content type for multipart writes", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({
        code: 0,
        data: { is_video: false, n_frames: 1, candidates: [], auto_selected: { body: [], face: [] } },
      }), { status: 200 })),
    );
    setCsrfToken("csrf-token");

    await extractCandidates("person-1", new Blob(["sample"], { type: "image/jpeg" }), "sample.jpg");

    const [, init] = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0];
    const headers = new Headers(init.headers);

    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
    expect(headers.get("X-Miloco-CSRF")).toBe("csrf-token");
    expect(headers.has("Authorization")).toBe(false);
    expect(headers.has("Content-Type")).toBe(false);
  });

  it("clears auth state and notifies the gate when any API request returns 401", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(
        JSON.stringify({ code: 1003, message: "Authentication required" }),
        { status: 401 },
      )),
    );
    setCsrfToken("csrf-token");
    let expiredCount = 0;
    const unsubscribe = subscribeSessionExpired(() => {
      expiredCount += 1;
    });

    await expect(apiFetch("/api/users")).rejects.toMatchObject({ status: 401 });

    expect(getCsrfToken()).toBe("");
    expect(expiredCount).toBe(1);
    unsubscribe();
  });
});
