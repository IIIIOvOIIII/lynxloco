import type {
  CameraSummary,
  PerceptionCamera,
  PerceptionCameraView,
  ScopeCamera,
} from "@/lib/types";
import { feedDid } from "@/lib/cameraChannel";

export function buildPerceptionCameraViews(
  perceptionCameras: PerceptionCamera[],
  cameraSummaries: CameraSummary[],
  scopeCameras: ScopeCamera[],
): PerceptionCameraView[] {
  const summaryById = new Map(cameraSummaries.map((camera) => [camera.id, camera]));
  const scopeById = new Map(
    scopeCameras.map((camera) => [
      feedDid(camera.did, camera.channel, camera.channelCount > 1),
      camera,
    ]),
  );
  const seen = new Set<string>();
  const views: PerceptionCameraView[] = [];

  for (const camera of perceptionCameras) {
    if (seen.has(camera.did)) continue;
    seen.add(camera.did);
    const summary = summaryById.get(camera.did) ?? null;
    const miotScope = scopeById.get(camera.did) ?? null;
    views.push({
      id: camera.did,
      name: camera.name || summary?.name || camera.did,
      roomName: camera.roomName || summary?.roomName || undefined,
      sourceType: summary?.sourceType ?? (miotScope ? "miot" : null),
      connected: summary?.connected ?? null,
      summary,
      miotScope,
    });
  }

  return views;
}
