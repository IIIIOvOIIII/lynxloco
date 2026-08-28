import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  cameraWatchUrl,
  isCameraAuthRequest,
} from "@/components/LivePlayerPlaceholder";

describe("unified camera watch URL", () => {
  it("encodes MIoT and RTSP camera ids without using the legacy route", () => {
    expect(cameraWatchUrl("miot/cam:ch0")).toBe(
      "/api/cameras/miot%2Fcam%3Ach0/watch?embedded=1",
    );
    expect(cameraWatchUrl("rtsp:source/1")).toBe(
      "/api/cameras/rtsp%3Asource%2F1/watch?embedded=1",
    );
    expect(cameraWatchUrl("rtsp:source/1")).not.toContain("/api/miot/");
    expect(cameraWatchUrl("rtsp:source/1")).not.toContain("token");
    expect(cameraWatchUrl("rtsp:source/1")).not.toContain("#");
  });

  it("only sends the in-memory injected token to its own same-origin iframe", () => {
    const component = readFileSync(
      fileURLToPath(new URL("../src/components/LivePlayerPlaceholder.tsx", import.meta.url)),
      "utf8",
    );
    expect(component).toContain("event.origin !== expectedOrigin");
    expect(component).toContain("event.source !== expectedSource");
    expect(component).toContain('event.data?.type === "miloco.camera.auth.request"');
    expect(component).toContain("iframeRef.current.contentWindow.postMessage(");
    expect(component).toContain("window.location.origin,");
    expect(component).not.toContain("localStorage.setItem");
    expect(component).not.toContain("sessionStorage.setItem");
  });

  it("rejects a wrong origin or source and accepts only the exact child", () => {
    const source = {} as MessageEventSource;
    const request = {
      origin: "https://miloco.test",
      source,
      data: { type: "miloco.camera.auth.request", version: 1 },
    };
    expect(isCameraAuthRequest(request, "https://evil.test", source)).toBe(false);
    expect(isCameraAuthRequest(request, request.origin, null)).toBe(false);
    expect(isCameraAuthRequest(request, request.origin, {} as MessageEventSource)).toBe(false);
    expect(isCameraAuthRequest(request, request.origin, source)).toBe(true);
    expect(
      isCameraAuthRequest({ ...request, data: { ...request.data, version: 2 } }, request.origin, source),
    ).toBe(false);
  });
});
