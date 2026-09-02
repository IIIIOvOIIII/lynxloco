import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import i18n from "@/i18n";
import {
  createRtspDraft,
  editRtspDraft,
  isTestCurrent,
  rtspDraftFingerprint,
  validateRtspCamera,
} from "@/lib/rtspCamera";

describe("RTSP camera form model", () => {
  it.each([
    ["http://camera.test/live", "rtspCamera.errors.scheme"],
    ["rtsp:///live", "rtspCamera.errors.host"],
    ["rtsp://user:pass@camera.test/live", "rtspCamera.errors.userinfo"],
    ["rtsp://camera.test/live#secret", "rtspCamera.errors.fragment"],
  ])("rejects %s", (uri, expected) => {
    expect(validateRtspCamera({ ...createRtspDraft(), name: "Cam", uri })).toContain(expected);
  });

  it("accepts rtsp and rtsps hosts", () => {
    expect(
      validateRtspCamera({ ...createRtspDraft(), name: "Cam", uri: "rtsp://cam.test/live" }),
    ).toEqual([]);
    expect(
      validateRtspCamera({ ...createRtspDraft(), name: "Cam", uri: "rtsps://cam.test/live" }),
    ).toEqual([]);
  });

  it("creates an untested, disabled-by-contract draft", () => {
    const draft = createRtspDraft();
    expect(draft).toEqual({
      name: "",
      room_name: "",
      uri: "",
      username: "",
      password: "",
      transport: "tcp",
      audio_enabled: true,
    });
    expect(draft).not.toHaveProperty("enabled");
  });

  it("never loads secret transport fields when editing; blank password means preserve", () => {
    const draft = editRtspDraft({
      id: "rtsp:1",
      sourceType: "rtsp",
      name: "Door",
      roomName: "Entry",
      enabled: true,
      connected: true,
      videoCodec: "h264",
      audioCodec: null,
      lastFrameUnixMs: null,
      hasPassword: true,
      errorCode: null,
      errorMessage: null,
      perceptionPrompt: "",
    });
    expect(draft.name).toBe("Door");
    expect(draft.room_name).toBe("Entry");
    expect(draft.uri).toBe("");
    expect(draft.username).toBe("");
    expect(draft.password).toBe("");
  });

  it("invalidates test success whenever any connection input changes", () => {
    const draft = { ...createRtspDraft(), name: "Cam", uri: "rtsp://cam.test/live" };
    const tested = rtspDraftFingerprint(draft);
    expect(isTestCurrent(draft, tested)).toBe(true);
    expect(isTestCurrent({ ...draft, transport: "udp" }, tested)).toBe(false);
    expect(isTestCurrent({ ...draft, password: "new" }, tested)).toBe(false);
    expect(isTestCurrent(draft, null)).toBe(false);
  });
});

describe("RTSP camera localization", () => {
  const localesDir = fileURLToPath(new URL("../src/i18n/locales/", import.meta.url));
  const required = [
    "url",
    "room",
    "username",
    "passwordPreserve",
    "transport",
    "audioGate",
    "test",
    "enable",
    "disable",
    "offline",
    "reconnecting",
    "statusStale",
    "liveTranscode",
  ];

  it.each(["zh", "en"] as const)("defines required %s strings", (language) => {
    const resource = JSON.parse(
      readFileSync(`${localesDir}${language}/rtspCamera.json`, "utf8"),
    ).rtspCamera as Record<string, unknown>;
    for (const key of required) expect(resource[key], key).toBeTypeOf("string");
  });

  it("loads the domain through the normal i18n glob", async () => {
    await i18n.changeLanguage("en");
    expect(i18n.t("rtspCamera.url")).not.toBe("rtspCamera.url");
    await i18n.changeLanguage("zh");
  });
});

describe("RTSP credential handling", () => {
  it("does not send transport fields to logs, URL builders, or browser storage", () => {
    const files = [
      "../src/lib/rtspCamera.ts",
      "../src/components/RtspCameraDialog.tsx",
      "../src/api/real.ts",
    ].map((path) => readFileSync(fileURLToPath(new URL(path, import.meta.url)), "utf8"));
    const source = files.join("\n");
    expect(source).not.toMatch(/console\.(?:log|info|warn|error)\s*\(/);
    expect(source).not.toMatch(/(?:localStorage|sessionStorage)\.(?:getItem|setItem)\s*\(/);
    expect(source).not.toContain("?password=");
    expect(source).not.toContain("?username=");
    expect(source).not.toContain("?uri=");
  });
});
