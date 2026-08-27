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

  it("carries browser websocket auth in a non-secret echoed subprotocol", () => {
    expect(page).toContain('const CAMERA_PROTOCOL = "miloco.camera.v1"');
    expect(page).toContain('`miloco.auth.${base64UrlToken(token)}`');
    expect(page).toContain("new WebSocket(url, [CAMERA_PROTOCOL, authProtocol(token)])");
  });

  it("uses an ephemeral URL fragment and clears it before network access", () => {
    expect(page).toContain('new URLSearchParams(location.hash.slice(1))');
    expect(page).toContain("history.replaceState(null, \"\", location.pathname + location.search)");
    expect(page).not.toContain('params.get("token")');
    expect(page).not.toContain('localStorage.getItem("miloco_token")');
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
