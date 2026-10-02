import assert from "node:assert/strict";
import test from "node:test";

import {
  fetchAtlasDraftFromBackend,
  fetchAtlasWorkbench,
  fetchDraftFromBackend,
} from "../src/query/api.ts";


function observation(assetId = "asset_test") {
  const stages = Object.fromEntries(
    ["metadata", "geocode", "vision", "embedding", "quality"].map((name) => [
      name,
      {
        status: "disabled",
        provenance: {
          producer_id: "memolens.image-worker",
          producer_version: "1",
          model_id: null,
          model_version: null,
          rule_id: "test-rule",
          rule_version: "1",
        },
        output: null,
        reason_code: "test_disabled",
      },
    ]),
  );
  return {
    object: "memolens.canonical_image_observation",
    schema_version: "1",
    status: "current",
    authority: "canonical_image_analysis",
    provenance_status: "verified_current",
    asset_id: assetId,
    analysis_binding: {
      analysis_run_id: `arun_${"1".repeat(32)}`,
      revision: 7,
      content_sha256: "2".repeat(64),
    },
    source_binding_sha256: "5".repeat(64),
    projection: {
      status: "current",
      generation_id: "image_projection_generation_active",
      processing_generation_id: "image_projection_generation_processing",
      receipt_sha256: "3".repeat(64),
      row_sha256: "4".repeat(64),
      reason_code: null,
    },
    stages,
    reason_code: null,
  };
}

function atlasAsset(assetId = "asset_test") {
  const current = observation(assetId);
  return {
    object: "atlas.asset",
    id: assetId,
    asset_id: assetId,
    analysis_status: "current",
    analysis_binding: current.analysis_binding,
    projection: current.projection,
    canonical_image_observation: current,
    filename: "guard.jpg",
    relative_path: "guard.jpg",
    title: "Guard",
    taken_at: null,
    place_name: null,
    country: null,
    description: "guard",
    tags: ["guard"],
    combined_text: "guard",
    embedding_backend: "semantic_hash",
    x: 0,
    y: 0,
    base_x: 0,
    base_y: 0,
    cluster_id: "cluster_guard",
    cluster_label: "Guard",
    mode_cluster_id: "cluster_guard",
    mode_cluster_label: "Guard",
    event_id: "event_guard",
    duplicate_group_id: null,
    neighbor_ids: [],
    quality_score: 0.9,
    technical_quality_score: 0.8,
    people_risk: 0,
    lat: null,
    lon: null,
    layout_version: "atlas-layout-v2",
  };
}

function retrievedImage(assetId = "asset_test") {
  const asset = atlasAsset(assetId);
  return {
    object: "retrieved_image",
    id: assetId,
    asset_id: assetId,
    analysis_status: "current",
    analysis_binding: asset.analysis_binding,
    projection: asset.projection,
    canonical_image_observation: asset.canonical_image_observation,
    filename: asset.filename,
    relative_path: asset.relative_path,
    taken_at: asset.taken_at,
    place_name: asset.place_name,
    country: asset.country,
    description: asset.description,
    tags: asset.tags,
    score: 0.9,
    matched_terms: ["guard"],
  };
}

function installJsonFetch(t, payload) {
  const original = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
  t.after(() => {
    globalThis.fetch = original;
  });
}

test("Atlas HTTP normalization preserves exact memory representative proof", async (t) => {
  const asset = atlasAsset();
  installJsonFetch(t, {
    object: "atlas.workbench",
    memories: [{ representative_assets: [asset], best_assets: [] }],
  });

  const workbench = await fetchAtlasWorkbench();

  assert.deepEqual(
    workbench.memories[0].representative_assets[0].canonical_image_observation,
    observation(),
  );
});

test("Atlas HTTP normalization rejects stripped, forged, pending, or unavailable proof", async (t) => {
  const cases = [
    (() => {
      const asset = atlasAsset("asset_missing");
      delete asset.canonical_image_observation;
      return asset;
    })(),
    (() => {
      const asset = atlasAsset("asset_forged");
      asset.canonical_image_observation = {
        ...asset.canonical_image_observation,
        asset_id: "asset_other",
      };
      return asset;
    })(),
    (() => {
      const asset = atlasAsset("asset_pending");
      asset.analysis_status = "pending";
      asset.projection = { ...asset.projection, status: "pending" };
      return asset;
    })(),
    (() => {
      const asset = atlasAsset("asset_unavailable");
      asset.analysis_status = "unknown";
      asset.canonical_image_observation = {
        ...asset.canonical_image_observation,
        status: "unavailable",
        provenance_status: "unavailable",
      };
      return asset;
    })(),
  ];
  const original = globalThis.fetch;
  t.after(() => {
    globalThis.fetch = original;
  });

  for (const asset of cases) {
    globalThis.fetch = async () => new Response(JSON.stringify({
      object: "atlas.workbench",
      memories: [{ representative_assets: [asset], best_assets: [] }],
    }), { status: 200 });
    await assert.rejects(
      fetchAtlasWorkbench(),
      /exact verified-current image observation/,
    );
  }
});

test("Atlas Create adapter keeps the same exact revision in the generated draft", async (t) => {
  const image = retrievedImage();
  let requestBody;
  const original = globalThis.fetch;
  globalThis.fetch = async (_url, init) => {
    requestBody = JSON.parse(init.body);
    return new Response(JSON.stringify({
      id: "atlas_gen_test",
      status: "completed",
      message: null,
      candidate_count: 1,
      data: [image],
    }), { status: 200, headers: { "Content-Type": "application/json" } });
  };
  t.after(() => {
    globalThis.fetch = original;
  });

  const draft = await fetchAtlasDraftFromBackend("guard", "balanced", {
    assetIds: ["asset_other", image.id],
    assetObservations: [observation("asset_other"), observation()],
  });

  assert.ok(draft);
  assert.deepEqual(requestBody.asset_ids, ["asset_other", image.id]);
  assert.deepEqual(
    requestBody.asset_observations,
    [observation("asset_other"), observation()],
  );
  assert.deepEqual(
    draft.selected[0].canonicalImageObservation,
    observation(),
  );
});

test("primary retrieval adapter requires and preserves exact canonical proof", async (t) => {
  const image = retrievedImage();
  installJsonFetch(t, {
    id: "retrieval_test",
    status: "completed",
    message: null,
    candidate_count: 1,
    data: [image],
  });

  const draft = await fetchDraftFromBackend("guard", "balanced");

  assert.ok(draft);
  assert.deepEqual(
    draft.selected[0].canonicalImageObservation,
    observation(),
  );
});

test("primary retrieval adapter fails closed when canonical proof is missing", async (t) => {
  const image = retrievedImage();
  delete image.canonical_image_observation;
  installJsonFetch(t, {
    id: "retrieval_missing_proof",
    status: "completed",
    message: null,
    candidate_count: 1,
    data: [image],
  });

  await assert.rejects(
    fetchDraftFromBackend("guard", "balanced"),
    /exact verified-current image observation/,
  );
});

test("Atlas Create adapter fails locally when basket proof is missing or out of order", async (t) => {
  const original = globalThis.fetch;
  let fetchCount = 0;
  globalThis.fetch = async () => {
    fetchCount += 1;
    throw new Error("network must not be reached");
  };
  t.after(() => {
    globalThis.fetch = original;
  });

  await assert.rejects(
    fetchAtlasDraftFromBackend("guard", "balanced", {
      assetIds: ["asset_test"],
    }),
    /exact verified-current image observation/,
  );
  await assert.rejects(
    fetchAtlasDraftFromBackend("guard", "balanced", {
      assetIds: ["asset_test"],
      assetObservations: [observation("asset_other")],
    }),
    /exact verified-current image observation/,
  );
  assert.equal(fetchCount, 0);
});

test("Atlas Create adapter rejects a current label with contradictory revision proof", async (t) => {
  const image = retrievedImage("asset_forged");
  image.analysis_binding = {
    ...image.analysis_binding,
    revision: image.analysis_binding.revision + 1,
  };
  installJsonFetch(t, {
    id: "atlas_gen_forged",
    status: "completed",
    message: null,
    candidate_count: 1,
    data: [image],
  });

  await assert.rejects(
    fetchAtlasDraftFromBackend("guard", "balanced"),
    /exact verified-current image observation/,
  );
});
