import { afterEach, describe, expect, it, vi } from "vitest";

import {
  realCreateRtspCamera,
  realClearCameraPrompt,
  realDeleteCamera,
  realDisableCamera,
  realEditRtspCamera,
  realEnableCamera,
  realListCameraSummaries,
  realSetCameraPrompt,
  realTestRtspCamera,
} from "@/api/real";
import type { RtspSourceInput } from "@/lib/types";
import { ApiError } from "@/api/client";

const originalFetch = globalThis.fetch;

afterEach(() => {
  vi.restoreAllMocks();
  globalThis.fetch = originalFetch;
});

const input: RtspSourceInput = {
  name: "Front door",
  room_name: "Entry",
  uri: "rtsps://camera.example.test:7441/live",
  username: "viewer",
  password: "secret",
  transport: "tcp",
  audio_enabled: true,
};

function summary(overrides: Record<string, unknown> = {}) {
  return {
    id: "rtsp:source/1",
    source_type: "rtsp",
    name: "Front door",
    room_name: "Entry",
    enabled: false,
    connected: false,
    video_codec: "h264",
    audio_codec: null,
    last_frame_unix_ms: null,
    has_password: true,
    error_code: null,
    error_message: null,
    perception_prompt: "Ignore TV reflection",
    ...overrides,
  };
}

function mockNormal(data: unknown) {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  globalThis.fetch = vi.fn(async (request: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: request.toString(), init });
    return new Response(JSON.stringify({ code: 0, message: "ok", data }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  }) as unknown as typeof fetch;
  return calls;
}

function mockError(status: number, body: unknown) {
  globalThis.fetch = vi.fn(
    async () =>
      new Response(JSON.stringify(body), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
  ) as unknown as typeof fetch;
}

describe("generic camera API", () => {
  it("lists the redacted public camera shape from GET /api/cameras", async () => {
    const raw = summary({
      uri: "rtsp://should-not-map/live",
      username: "should-not-map",
      password: "should-not-map",
    });
    const calls = mockNormal([raw]);

    const cameras = await realListCameraSummaries();

    expect(calls[0]).toMatchObject({ url: "/api/cameras" });
    expect(calls[0].init?.method).toBeUndefined();
    expect(cameras).toEqual([
      {
        id: "rtsp:source/1",
        sourceType: "rtsp",
        name: "Front door",
        roomName: "Entry",
        enabled: false,
        connected: false,
        videoCodec: "h264",
        audioCodec: null,
        lastFrameUnixMs: null,
        hasPassword: true,
        errorCode: null,
        errorMessage: null,
        perceptionPrompt: "Ignore TV reflection",
      },
    ]);
    expect(cameras[0]).not.toHaveProperty("uri");
    expect(cameras[0]).not.toHaveProperty("username");
    expect(cameras[0]).not.toHaveProperty("password");
  });

  it("tests an unsaved source at the exact test path", async () => {
    const calls = mockNormal({ video_codec: "h264", width: 1280, height: 720 });

    await realTestRtspCamera(input);

    expect(calls[0].url).toBe("/api/cameras/rtsp/test");
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual(input);
  });

  it("creates a disabled source without an enabled field", async () => {
    const calls = mockNormal(summary());

    const created = await realCreateRtspCamera(input);

    expect(calls[0].url).toBe("/api/cameras/rtsp");
    expect(calls[0].init?.method).toBe("POST");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual(input);
    expect(JSON.parse(String(calls[0].init?.body))).not.toHaveProperty("enabled");
    expect(created.enabled).toBe(false);
  });

  it("edits with an encoded camera id and preserves a blank password", async () => {
    const calls = mockNormal(summary());
    const edit = { ...input, password: "" };

    await realEditRtspCamera("rtsp:source/1", edit);

    expect(calls[0].url).toBe("/api/cameras/rtsp/rtsp%3Asource%2F1");
    expect(calls[0].init?.method).toBe("PUT");
    expect(JSON.parse(String(calls[0].init?.body)).password).toBe("");
  });

  it.each([
    ["enable", realEnableCamera, "POST"],
    ["disable", realDisableCamera, "POST"],
  ] as const)("uses the exact %s path", async (action, fn, method) => {
    const calls = mockNormal(summary());
    await fn("rtsp:source/1");
    expect(calls[0].url).toBe(`/api/cameras/rtsp%3Asource%2F1/${action}`);
    expect(calls[0].init?.method).toBe(method);
  });

  it("deletes with DELETE and an encoded camera id", async () => {
    const calls = mockNormal(null);
    await realDeleteCamera("rtsp:source/1");
    expect(calls[0].url).toBe("/api/cameras/rtsp%3Asource%2F1");
    expect(calls[0].init?.method).toBe("DELETE");
  });

  it("sets a camera prompt through the generic camera endpoint", async () => {
    const calls = mockNormal(summary({ perception_prompt: "玄关左下角摆件不是宠物" }));

    const updated = await realSetCameraPrompt(
      "rtsp:source/1",
      "玄关左下角摆件不是宠物",
    );

    expect(calls[0].url).toBe("/api/cameras/rtsp%3Asource%2F1/prompt");
    expect(calls[0].init?.method).toBe("PUT");
    expect(JSON.parse(String(calls[0].init?.body))).toEqual({
      prompt: "玄关左下角摆件不是宠物",
    });
    expect(updated.perceptionPrompt).toBe("玄关左下角摆件不是宠物");
  });

  it("clears a camera prompt through the generic camera endpoint", async () => {
    const calls = mockNormal(summary({ perception_prompt: "" }));

    const updated = await realClearCameraPrompt("rtsp:source/1");

    expect(calls[0].url).toBe("/api/cameras/rtsp%3Asource%2F1/prompt");
    expect(calls[0].init?.method).toBe("DELETE");
    expect(updated.perceptionPrompt).toBe("");
  });

  it("preserves a stable probe error from FastAPI detail", async () => {
    mockError(409, {
      detail: {
        code: "authentication_failed",
        message: "RTSP authentication failed",
      },
    });
    const promise = realTestRtspCamera(input);
    await expect(promise).rejects.toMatchObject({
      status: 409,
      code: "authentication_failed",
      message: "RTSP authentication failed",
    } satisfies Partial<ApiError>);
  });

  it("shows a safe stable conflict when an RTSP edit loses its revision", async () => {
    mockError(409, {
      detail: {
        code: "camera_configuration_changed",
        message: "Camera configuration changed; retry the update",
        private: "rtsp://private.example/secret-path",
      },
    });
    await expect(realEditRtspCamera("rtsp:source/1", input)).rejects.toMatchObject({
      status: 409,
      code: "camera_configuration_changed",
      message: "Camera configuration changed; retry the update",
    } satisfies Partial<ApiError>);
  });

  it("preserves validation code/message from a 422 detail object", async () => {
    mockError(422, {
      detail: {
        code: "invalid_camera_request",
        message: "RTSP camera request is invalid",
      },
    });
    await expect(realCreateRtspCamera(input)).rejects.toMatchObject({
      status: 422,
      code: "invalid_camera_request",
      message: "RTSP camera request is invalid",
    } satisfies Partial<ApiError>);
  });

  it("preserves not-found code/message from a 404 detail object", async () => {
    mockError(404, {
      detail: { code: "camera_not_found", message: "Camera was not found" },
    });
    await expect(realDeleteCamera("rtsp:missing")).rejects.toMatchObject({
      status: 404,
      code: "camera_not_found",
      message: "Camera was not found",
    } satisfies Partial<ApiError>);
  });

  it("keeps string detail compatibility without exposing an object coercion", async () => {
    mockError(409, { detail: "Camera is disabled" });
    await expect(realEnableCamera("rtsp:source/1")).rejects.toMatchObject({
      status: 409,
      code: undefined,
      message: "Camera is disabled",
    } satisfies Partial<ApiError>);
  });

  it("falls back safely when detail fields are not strings", async () => {
    mockError(409, {
      detail: {
        code: "connection_failed",
        message: { private: "rtsp://private.example/secret" },
      },
    });
    await expect(realTestRtspCamera(input)).rejects.toMatchObject({
      status: 409,
      code: "connection_failed",
      message: "HTTP 409",
    } satisfies Partial<ApiError>);
  });
});
