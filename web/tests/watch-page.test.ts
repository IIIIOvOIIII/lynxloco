import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

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

  it("uses the same-origin dashboard session for its browser websocket", () => {
    expect(page).toContain('const CAMERA_PROTOCOL = "miloco.camera.v1"');
    expect(page).toContain("new WebSocket(url, [CAMERA_PROTOCOL])");
    expect(page).not.toContain("miloco.auth.");
  });

  it("does not receive or store a service token", () => {
    expect(page).not.toContain("__MILOCO_TOKEN__");
    expect(page).not.toContain("requestParentToken");
    expect(page).not.toContain("postMessage(");
    expect(page).not.toContain("location.hash");
    expect(page).not.toContain('params.get("token")');
    expect(page).not.toContain('localStorage.getItem("miloco_token")');
    expect(page).not.toContain('sessionStorage.getItem("miloco_token")');
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

  it("keeps H264 as the default binary path after the JPEG fallback split", () => {
    expect(page).toContain('codecHint = "h264"');
    expect(page).toContain("if (useJpegCanvas) {");
    expect(page).toContain("handleJpegPayload(payload)");
    expect(page).toContain("const nalu = payload");
    expect(page).not.toContain("buf.subarray(16)");
  });
});
