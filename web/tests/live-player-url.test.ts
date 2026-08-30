import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  cameraWatchUrl,
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

  it("uses the same-origin dashboard session without an injected token exchange", () => {
    const component = readFileSync(
      fileURLToPath(new URL("../src/components/LivePlayerPlaceholder.tsx", import.meta.url)),
      "utf8",
    );
    expect(component).toContain("dashboard session cookie");
    expect(component).not.toContain("miloco.camera.auth.request");
    expect(component).not.toContain("postMessage(");
    expect(component).not.toContain("resolveToken");
    expect(component).not.toContain("localStorage.setItem");
    expect(component).not.toContain("sessionStorage.setItem");
  });
});
