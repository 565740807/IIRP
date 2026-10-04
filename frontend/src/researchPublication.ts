/** Publication rules for immutable research responses; no fetching or finance. */
export type ResearchPublicationKind = "refresh" | "submit";

export interface ResearchPublicationState {
  epoch: number;
  view: string;
  /** Explicit navigation identity; automatic URL replacement must preserve it. */
  navigation?: string;
  mounted: boolean;
  edited: boolean;
  analysisPending: boolean;
}

export interface ResearchPublicationTicket {
  epoch: number;
  view: string;
  navigation?: string;
}

/** Conditions serialized into a canonical URL do not select another saved view. */
export function researchViewIdentity(
  pathname: string,
  search: string | URLSearchParams,
): string {
  const params = new URLSearchParams(search);
  const frozen = [...new Set((params.get("result_ids") ?? "").split(",").filter(Boolean))]
    .sort().join(",");
  return `${pathname}|${params.get("a") ?? ""}|${frozen}`;
}

export function captureResearchPublication(
  current: ResearchPublicationState,
): ResearchPublicationTicket {
  return {
    epoch: current.epoch,
    view: current.view,
    navigation: current.navigation,
  };
}

/** Cache storage is allowed separately; this gate controls visible publication. */
export function canPublishResearch(
  kind: ResearchPublicationKind,
  ticket: ResearchPublicationTicket | null | undefined,
  current: ResearchPublicationState,
): boolean {
  if (!ticket || !current.mounted || current.edited) return false;
  if (ticket.epoch !== current.epoch || ticket.view !== current.view ||
      ticket.navigation !== current.navigation) return false;
  // A submit response normally arrives while its own mutation is still pending.
  // A refresh must not replace conditions being submitted by another command.
  return kind === "submit" || !current.analysisPending;
}

/** Caller acquires busy synchronously, then releases it on every settle path. */
export function canStartResearchRefresh(
  current: ResearchPublicationState,
  availability: {
    analysisId: string | null | undefined;
    frozen: boolean;
    visible: boolean;
    busy: boolean;
  },
): boolean {
  return current.mounted && !current.edited && !current.analysisPending &&
    !!availability.analysisId && !availability.frozen && availability.visible &&
    !availability.busy;
}
