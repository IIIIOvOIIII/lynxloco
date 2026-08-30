import type { CameraSummary, ScopeCamera } from "@/lib/types";

export function hasActiveRtspPerceptionCamera(
  cameraSummaries: CameraSummary[],
): boolean {
  return cameraSummaries.some(
    (camera) =>
      camera.sourceType === "rtsp" && camera.enabled && camera.connected,
  );
}

export function hasActivePerceptionCamera(
  scopeCameras: ScopeCamera[],
  cameraSummaries: CameraSummary[],
): boolean {
  return (
    scopeCameras.some((camera) => camera.inUse) ||
    hasActiveRtspPerceptionCamera(cameraSummaries)
  );
}
