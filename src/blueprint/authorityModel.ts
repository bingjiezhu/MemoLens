import {
  BLUEPRINT_DECISION_UNITS,
  type BlueprintDecisionUnitName,
  type DesktopAgentAuthorityOverview,
  type DesktopProjectDecisionAuthority,
} from "./authorityTypes";

export function canonicalDecisionUnitSelection(
  values: Iterable<BlueprintDecisionUnitName>,
): BlueprintDecisionUnitName[] {
  const selected = new Set(values);
  return BLUEPRINT_DECISION_UNITS.filter((name) => selected.has(name));
}

export function authorityProjectIds(
  overview: DesktopAgentAuthorityOverview | null,
): string[] {
  if (!overview) return [];
  const ids = new Set<string>();
  overview.pairings.forEach((pairing) => ids.add(pairing.projectId));
  overview.capabilities.forEach((capability) => ids.add(capability.projectId));
  if (overview.projectAuthority) ids.add(overview.projectAuthority.projectId);
  return [...ids].sort((left, right) => left.localeCompare(right));
}

export function decisionUnitsForOperation(
  authority: DesktopProjectDecisionAuthority,
  selected: Iterable<BlueprintDecisionUnitName>,
  operation: "confirm" | "revoke",
): BlueprintDecisionUnitName[] {
  const requested = new Set(selected);
  return authority.decisionUnits
    .filter((unit) => requested.has(unit.name))
    .filter((unit) => (
      operation === "confirm" ? unit.state === "unverified" : unit.state === "confirmed"
    ))
    .map((unit) => unit.name);
}
