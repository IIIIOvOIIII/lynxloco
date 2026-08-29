import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import "./watch-mse.test.js";

const page = readFileSync(
  fileURLToPath(new URL("../public/watch.html", import.meta.url)),
  "utf8",
);

describe("bundled camera viewer", () => {
  it("builds the generic stream URL from an encoded camera id", () => {
    expect(page).toContain("/api/cameras/${encodeURIComponent(camId)}/stream");
    expect(page).not.toContain("/api/cameras/${camId}/stream");
    expect(page).not.toContain("?token=${encodeURIComponent(token)}");
  });

  it("carries browser websocket auth in a non-secret echoed subprotocol", () => {
    expect(page).toContain('const CAMERA_PROTOCOL = "miloco.camera.v1"');
    expect(page).toContain('`miloco.auth.${base64UrlToken(token)}`');
    expect(page).toContain("new WebSocket(url, [CAMERA_PROTOCOL, authProtocol(token)])");
  });

  it("requests an in-memory token from the exact same-origin parent before boot", () => {
    expect(page).toContain('const AUTH_REQUEST = "miloco.camera.auth.request"');
    expect(page).toContain('const AUTH_RESPONSE = "miloco.camera.auth.response"');
    expect(page).toContain("event.origin !== location.origin");
    expect(page).toContain("event.source !== window.parent");
    expect(page).toContain("window.parent === window");
    expect(page).toContain("await requestParentToken()");
    expect(page).toContain("window.parent.postMessage(");
    expect(page).toContain("location.origin,");
    expect(page).not.toContain("location.hash");
    expect(page).not.toContain('params.get("token")');
    expect(page).not.toContain('localStorage.getItem("miloco_token")');
    expect(page).not.toContain('sessionStorage.getItem("miloco_token")');
  });

  it("fails closed when neither legacy injection nor parent auth is available", () => {
    expect(page).toContain("if (!token) {");
    expect(page).toContain('setState(t("missingToken"), true)');
    expect(page).toContain("return;");
  });

  it("contains no legacy MIoT stream path", () => {
    expect(page).not.toContain("/api/miot/ws/video_stream");
  });

  it("gets the camera id from the URL without injecting it into HTML", () => {
    expect(page).toContain('params.get("camera_id")');
    expect(page).toContain("decodeURIComponent");
    expect(page).not.toContain("__MILOCO_CAMERA_ID__");
  });

  it("keeps legacy channel links working through generic camera ids", () => {
    expect(page).toContain("`${wanted}:ch${legacyChannel}`");
    expect(page).toContain("cameraIdFromPath()");
  });

  it("treats every binary message as an Annex B H264 chunk", () => {
    expect(page).toContain('codecHint = "h264"');
    expect(page).toContain("const nalu = new Uint8Array(ev.data)");
    expect(page).not.toContain("buf.subarray(16)");
  });
});
