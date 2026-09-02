import type {
  HomeAssistantBulkAction,
  HomeAssistantEntity,
  HomeAssistantEntityPolicyBulkSkipped,
} from "./types";

export const HOME_ASSISTANT_PAGE_SIZES = [10, 25, 50, 100] as const;
export type HomeAssistantPageSize = (typeof HOME_ASSISTANT_PAGE_SIZES)[number];

export interface HomeAssistantPageSlice {
  items: HomeAssistantEntity[];
  page: number;
  pageSize: number;
  pages: number;
  total: number;
  startIndex: number;
  endIndex: number;
}

export interface HomeAssistantBulkTargetSummary {
  import: number;
  "remove-import": number;
  "allow-control": number;
  "disable-control": number;
}

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

export function filterHomeAssistantEntities(
  entities: HomeAssistantEntity[],
  query: string,
): HomeAssistantEntity[] {
  const terms = query
    .trim()
    .toLocaleLowerCase()
    .split(/\s+/)
    .filter(Boolean);
  if (terms.length === 0) return entities;

  return entities.filter((entity) => {
    const fields = [
      entity.name,
      entity.entityId,
      entity.domain,
      entity.room ?? "",
      entity.state,
    ].map((value) => String(value).toLocaleLowerCase());
    return terms.every((term) => fields.some((field) => field.includes(term)));
  });
}

function safeHomeAssistantPageSize(pageSize: number): HomeAssistantPageSize {
  return HOME_ASSISTANT_PAGE_SIZES.includes(pageSize as HomeAssistantPageSize)
    ? (pageSize as HomeAssistantPageSize)
    : 25;
}

export function clampHomeAssistantPage(
  page: number,
  totalCount: number,
  pageSize: number,
): number {
  const safePageSize = safeHomeAssistantPageSize(pageSize);
  const pages = Math.max(1, Math.ceil(totalCount / safePageSize));
  if (!Number.isFinite(page)) return 1;
  return Math.min(Math.max(1, Math.trunc(page)), pages);
}

export function paginateHomeAssistantEntities(
  entities: HomeAssistantEntity[],
  page: number,
  pageSize: number,
): HomeAssistantPageSlice {
  const safePageSize = safeHomeAssistantPageSize(pageSize);
  const safePage = clampHomeAssistantPage(page, entities.length, safePageSize);
  const startOffset = (safePage - 1) * safePageSize;
  const items = entities.slice(startOffset, startOffset + safePageSize);
  return {
    items,
    page: safePage,
    pageSize: safePageSize,
    pages: Math.max(1, Math.ceil(entities.length / safePageSize)),
    total: entities.length,
    startIndex: entities.length === 0 ? 0 : startOffset + 1,
    endIndex: startOffset + items.length,
  };
}

export function getHomeAssistantBulkTargets(
  entities: HomeAssistantEntity[],
  action: HomeAssistantBulkAction,
): HomeAssistantEntity[] {
  switch (action) {
    case "import":
      return entities.filter((entity) => !entity.included);
    case "remove-import":
      return entities.filter((entity) => entity.included);
    case "allow-control":
      return entities.filter(
        (entity) =>
          entity.included &&
          !entity.controlEnabled &&
          entity.controlSupported &&
          !entity.controlBlockedReason,
      );
    case "disable-control":
      return entities.filter((entity) => entity.included && entity.controlEnabled);
  }
}

export function summarizeHomeAssistantBulkTargets(
  entities: HomeAssistantEntity[],
): HomeAssistantBulkTargetSummary {
  return {
    import: getHomeAssistantBulkTargets(entities, "import").length,
    "remove-import": getHomeAssistantBulkTargets(entities, "remove-import").length,
    "allow-control": getHomeAssistantBulkTargets(entities, "allow-control").length,
    "disable-control": getHomeAssistantBulkTargets(entities, "disable-control").length,
  };
}

export function summarizeHomeAssistantSkippedReasons(
  skipped: HomeAssistantEntityPolicyBulkSkipped[],
): { reason: HomeAssistantEntityPolicyBulkSkipped["reason"]; count: number } | null {
  let winner: {
    reason: HomeAssistantEntityPolicyBulkSkipped["reason"];
    count: number;
  } | null = null;
  const counts = new Map<HomeAssistantEntityPolicyBulkSkipped["reason"], number>();
  for (const item of skipped) {
    const count = (counts.get(item.reason) ?? 0) + 1;
    counts.set(item.reason, count);
    if (!winner || count > winner.count) {
      winner = { reason: item.reason, count };
    }
  }
  return winner;
}
