import assert from "node:assert/strict";
import test from "node:test";

import {
  authorityProjectIds,
  canonicalDecisionUnitSelection,
  decisionUnitsForOperation,
} from "../src/blueprint/authorityModel.ts";

test("decision selection is always returned in frozen contract order", () => {
  assert.deepEqual(
    canonicalDecisionUnitSelection(["techniques", "script", "intent_goal"]),
    ["intent_goal", "script", "techniques"],
  );
});

test("confirm and revoke controls select only authority-compatible units", () => {
  const authority = {
    projectId: "project-1",
    revision: 2,
    state: "partially_confirmed",
    confirmedDecisionUnitCount: 1,
    decisionUnitCount: 8,
    asOfRevision: 2,
    decisionUnits: [
      { name: "intent_goal", label: "Goal", state: "unverified", confirmedAt: null },
      { name: "intent_stance", label: "Stance", state: "unverified", confirmedAt: null },
      { name: "script", label: "Script", state: "confirmed", confirmedAt: "2026-08-22T12:00:00Z" },
      { name: "creative_direction", label: "Direction", state: "unverified", confirmedAt: null },
      { name: "output", label: "Output", state: "unverified", confirmedAt: null },
      { name: "material_constraints", label: "Material", state: "unverified", confirmedAt: null },
      { name: "references", label: "References", state: "unverified", confirmedAt: null },
      { name: "techniques", label: "Techniques", state: "unverified", confirmedAt: null },
    ],
  };
  const selected = ["intent_goal", "script"];
  assert.deepEqual(decisionUnitsForOperation(authority, selected, "confirm"), ["intent_goal"]);
  assert.deepEqual(decisionUnitsForOperation(authority, selected, "revoke"), ["script"]);
});

test("project suggestions are unique and stable across pairing and capability lists", () => {
  const shared = {
    pairings: [{ projectId: "project-b" }, { projectId: "project-a" }],
    capabilities: [{ projectId: "project-a" }],
    projectAuthority: { projectId: "project-c" },
  };
  assert.deepEqual(authorityProjectIds(shared), ["project-a", "project-b", "project-c"]);
});
