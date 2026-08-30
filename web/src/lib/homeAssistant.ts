import type { HomeAssistantEntity } from "./types";

export function maskHomeAssistantToken(tokenConfigured: boolean): string {
  return tokenConfigured ? "••••••••" : "";
}

export function controlDisabledReason(
  entity: HomeAssistantEntity,
): string | null {
  if (!entity.included) return "not-imported";
  if (!entity.controlSupported) {
    return entity.controlBlockedReason || "unsupported-domain";
  }
  return null;
}
