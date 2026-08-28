import type { CameraSummary, RtspSourceInput } from "./types";

export type RtspValidationKey =
  | "rtspCamera.errors.name"
  | "rtspCamera.errors.url"
  | "rtspCamera.errors.scheme"
  | "rtspCamera.errors.host"
  | "rtspCamera.errors.userinfo"
  | "rtspCamera.errors.fragment"
  | "rtspCamera.errors.port";

export function createRtspDraft(): RtspSourceInput {
  return {
    name: "",
    room_name: "",
    uri: "",
    username: "",
    password: "",
    transport: "tcp",
    audio_enabled: true,
  };
}

/**
 * The public camera record intentionally has no transport endpoint or credentials.
 * Editing therefore starts those fields empty. In particular, an empty password is
 * sent unchanged so the backend preserves the stored password.
 */
export function editRtspDraft(camera: CameraSummary): RtspSourceInput {
  return {
    ...createRtspDraft(),
    name: camera.name,
    room_name: camera.roomName,
  };
}

export function validateRtspCamera(input: RtspSourceInput): RtspValidationKey[] {
  const errors: RtspValidationKey[] = [];
  if (!input.name.trim()) errors.push("rtspCamera.errors.name");
  if (!input.uri.trim()) {
    errors.push("rtspCamera.errors.url");
    return errors;
  }

  let parsed: URL;
  try {
    parsed = new URL(input.uri.trim());
  } catch {
    errors.push("rtspCamera.errors.url");
    return errors;
  }
  if (parsed.protocol !== "rtsp:" && parsed.protocol !== "rtsps:") {
    errors.push("rtspCamera.errors.scheme");
  }
  if (!parsed.hostname) errors.push("rtspCamera.errors.host");
  if (parsed.username || parsed.password) errors.push("rtspCamera.errors.userinfo");
  if (parsed.hash) errors.push("rtspCamera.errors.fragment");
  if (parsed.port) {
    const port = Number(parsed.port);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      errors.push("rtspCamera.errors.port");
    }
  }
  return errors;
}

/** Stable in-memory test marker; never persisted or logged. */
export function rtspDraftFingerprint(input: RtspSourceInput): string {
  return JSON.stringify([
    input.name.trim(),
    input.room_name.trim(),
    input.uri.trim(),
    input.username,
    input.password,
    input.transport,
    input.audio_enabled,
  ]);
}

export function isTestCurrent(
  input: RtspSourceInput,
  successfulFingerprint: string | null,
): boolean {
  return successfulFingerprint !== null && successfulFingerprint === rtspDraftFingerprint(input);
}

export function normalizedRtspInput(input: RtspSourceInput): RtspSourceInput {
  return {
    ...input,
    name: input.name.trim(),
    room_name: input.room_name.trim(),
    uri: input.uri.trim(),
    username: input.username.trim(),
  };
}
