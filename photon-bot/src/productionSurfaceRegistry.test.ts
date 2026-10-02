import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  PHOTON_PRODUCTION_ACTION_IDS,
  requirePhotonProductionAction,
  type PhotonProductionActionId,
} from "./productionSurfaceRegistry.js";

const sourceRoot = path.dirname(fileURLToPath(import.meta.url));
const repositoryRoot = path.resolve(sourceRoot, "..", "..");

function inventoryPhotonActions(): string[] {
  const inventory = JSON.parse(
    fs.readFileSync(
      path.join(repositoryRoot, "core", "production_surface_inventory.v1.json"),
      "utf8",
    ),
  ) as {
    actions: Array<{ id: string; surface: string }>;
  };
  return inventory.actions
    .filter((row) => row.surface === "photon_bot")
    .map((row) => row.id)
    .sort();
}

function realProductionCallsiteActions(): string[] {
  const productionPaths = fs
    .readdirSync(sourceRoot, { withFileTypes: true })
    .filter(
      (entry) =>
        entry.isFile() &&
        entry.name.endsWith(".ts") &&
        !entry.name.endsWith(".test.ts") &&
        entry.name !== "productionSurfaceRegistry.ts",
    )
    .map((entry) => path.join(sourceRoot, entry.name));
  const productionSources = productionPaths
    .map((sourcePath) => fs.readFileSync(sourcePath, "utf8"))
    .join("\n");
  const invocationCount =
    productionSources.match(/requirePhotonProductionAction\s*\(/g)?.length ?? 0;
  const literalCallsites = [
    ...productionSources.matchAll(
      /requirePhotonProductionAction\(\s*"([^"]+)"\s*\)/g,
    ),
  ].map((match) => match[1]!);

  assert.equal(
    literalCallsites.length,
    invocationCount,
    "Every production registry callsite must use a statically auditable literal action ID.",
  );
  return [...new Set(literalCallsites)].sort();
}

test("supported Photon runtime actions equal the shared production inventory", () => {
  const inventory = JSON.parse(
    fs.readFileSync(
      path.join(repositoryRoot, "core", "production_surface_inventory.v1.json"),
      "utf8",
    ),
  ) as {
    actions: Array<{ id: string; surface: string }>;
    surface_verification: Array<{ mode: string; status: string; surface: string }>;
  };
  const declared = inventory.actions
    .filter((row) => row.surface === "photon_bot")
    .map((row) => row.id)
    .sort();

  assert.deepEqual([...PHOTON_PRODUCTION_ACTION_IDS].sort(), declared);
  const verification = inventory.surface_verification.find(
    (row) => row.surface === "photon_bot",
  );
  assert.deepEqual(verification, {
    mode: "automatic",
    owner: "photon_bot",
    status: "enforced",
    surface: "photon_bot",
  });
});

test("production call sites cover every registered Photon action", () => {
  const productionSources = ["agent.ts", "discord.ts"]
    .map((name) => fs.readFileSync(path.join(sourceRoot, name), "utf8"))
    .join("\n");
  const discovered = new Set(
    [...productionSources.matchAll(/requirePhotonProductionAction\(\s*"([^"]+)"/g)]
      .map((match) => match[1]),
  );
  assert.deepEqual(
    [...discovered].sort(),
    [...PHOTON_PRODUCTION_ACTION_IDS].sort(),
  );
});

test("Photon inventory registry and real production callsites are exactly equal", () => {
  const registered = [...PHOTON_PRODUCTION_ACTION_IDS].sort();
  assert.deepEqual(inventoryPhotonActions(), registered);
  assert.deepEqual(realProductionCallsiteActions(), registered);
});

test("unknown Photon action fails closed", () => {
  assert.throws(
    () => requirePhotonProductionAction(
      "photon_bot.discord.future_magic" as PhotonProductionActionId,
    ),
    /Unknown Photon production action rejected/,
  );
});
