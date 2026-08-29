import { describe, expect, it } from "vitest";
import { buildPerceptionCameraViews } from "../src/lib/perceptionCameraView";
import type { CameraSummary, PerceptionCamera, ScopeCamera } from "../src/lib/types";

const perception: PerceptionCamera[] = [
  { did: "miot-1", name: "MIoT live", roomName: "Study" },
  { did: "rtsp-1", name: "RTSP live", roomName: "Garage" },
  { did: "unknown-1", name: "Metadata delayed", roomName: "Hall" },
  { did: "rtsp-1", name: "duplicate must not replace first" },
];

const summaries: CameraSummary[] = [
  {
    id: "miot-1", sourceType: "miot", name: "MIoT summary", roomName: "Study",
    enabled: true, connected: true, videoCodec: "h264", audioCodec: null,
    lastFrameUnixMs: 1, hasPassword: false, errorCode: null, errorMessage: null,
  },
  {
    id: "rtsp-1", sourceType: "rtsp", name: "RTSP summary", roomName: "Garage",
    enabled: true, connected: true, videoCodec: "h264", audioCodec: null,
    lastFrameUnixMs: 1, hasPassword: true, errorCode: null, errorMessage: null,
  },
];

const scope = [{
  did: "miot-1", name: "MIoT scope", channel: 0, channelCount: 1,
  roomName: "Study", cloudOnline: true, lanReachable: true, awake: true,
  inUse: true, connected: true, voiceInUse: false,
  perceptionPrompt: "",
}] as ScopeCamera[];

describe("buildPerceptionCameraViews", () => {
  it("uses perception membership and exact IDs for source enrichment", () => {
    const views = buildPerceptionCameraViews(perception, summaries, scope);

    expect(views.map((view) => view.id)).toEqual(["miot-1", "rtsp-1", "unknown-1"]);
    expect(views[0]).toMatchObject({ sourceType: "miot", miotScope: scope[0] });
    expect(views[1]).toMatchObject({ sourceType: "rtsp", miotScope: null });
    expect(views[2]).toMatchObject({ sourceType: null, summary: null, miotScope: null });
  });

  it("does not classify by a misleading name or room", () => {
    const views = buildPerceptionCameraViews(
      [{ did: "plain-id", name: "rtsp camera", roomName: "miot room" }],
      [],
      [],
    );

    expect(views[0].sourceType).toBeNull();
  });
});
