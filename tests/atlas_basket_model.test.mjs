import assert from "node:assert/strict";
import test from "node:test";

import {
  MAX_BASKET_ITEMS,
  basketItemFromAtlasAsset,
  basketItemFromPhotoAsset,
  basketItemFromSource,
  basketSignature,
  mergeBasketItems,
} from "../src/basket/model.ts";

function item(id, overrides = {}) {
  return {
    id,
    title: `title-${id}`,
    subtitle: `subtitle-${id}`,
    imageUrl: `/preview/${id}.jpg`,
    ...overrides,
  };
}

function photo(id, overrides = {}) {
  return {
    id,
    title: `Photo ${id}`,
    summary: "summary",
    location: "Paris",
    takenAt: "2025-01-02",
    slot: "cover",
    concepts: ["city"],
    surfaceTint: "#fff",
    imageUrl: `/photos/${id}.jpg`,
    ...overrides,
  };
}

function atlasAsset(id, overrides = {}) {
  return {
    object: "atlas.asset",
    id,
    filename: `${id}.jpg`,
    relative_path: `nested/${id}.jpg`,
    title: `Atlas ${id}`,
    taken_at: "2024-03-04T12:30:00Z",
    place_name: "Tokyo",
    country: "Japan",
    description: "city walk",
    tags: ["city", "walk"],
    combined_text: "city walk",
    embedding_backend: "local",
    x: 0,
    y: 0,
    base_x: 0,
    base_y: 0,
    cluster_id: "cluster-1",
    cluster_label: "City",
    mode_cluster_id: "mode-1",
    mode_cluster_label: "Places",
    event_id: "event-1",
    duplicate_group_id: null,
    neighbor_ids: [],
    quality_score: 0.9,
    technical_quality_score: 0.8,
    people_risk: 0,
    lat: null,
    lon: null,
    layout_version: "1",
    ...overrides,
  };
}

test("basket signatures preserve item order and use the existing unit separator", () => {
  assert.equal(basketSignature([item("a"), item("b"), item("a")]), "a\u001fb\u001fa");
  assert.equal(basketSignature([]), "");
});

test("hydrate merge keeps persisted order, overlays local edits, and appends local-only items", () => {
  const persisted = [item("a"), item("b"), item("c")];
  const localDuringHydration = [
    item("b", { title: "locally updated" }),
    item("d"),
  ];

  const merged = mergeBasketItems(persisted, localDuringHydration);

  assert.deepEqual(merged.map((entry) => entry.id), ["a", "b", "c", "d"]);
  assert.equal(merged[1].title, "locally updated");
});

test("merge deduplicates by id and enforces the existing 240 item ceiling", () => {
  const current = Array.from({ length: MAX_BASKET_ITEMS }, (_, index) => item(`item-${index}`));
  const merged = mergeBasketItems(current, [
    item("item-10", { title: "replacement" }),
    item("overflow"),
  ]);

  assert.equal(merged.length, MAX_BASKET_ITEMS);
  assert.equal(merged[10].title, "replacement");
  assert.equal(merged.some((entry) => entry.id === "overflow"), false);
});

test("photo conversion preserves title, preview, and subtitle formatting", () => {
  assert.deepEqual(basketItemFromPhotoAsset(photo("photo-1")), {
    id: "photo-1",
    title: "Photo photo-1",
    subtitle: "Paris · 2025-01-02",
    imageUrl: "/photos/photo-1.jpg",
  });
  assert.equal(
    basketItemFromPhotoAsset(photo("photo-2", { location: "", takenAt: "unknown" })).subtitle,
    "unknown",
  );
});

test("Atlas conversion retains the existing query mapper and encoded preview contract", () => {
  const asset = atlasAsset("atlas-1");
  const converted = basketItemFromAtlasAsset(
    asset,
    2,
    "http://127.0.0.1:5519/",
    "/Users/example/My Photos",
  );

  assert.equal(converted.id, "atlas-1");
  assert.equal(converted.title, "atlas 1");
  assert.equal(converted.subtitle, "Tokyo · Japan · 2024-03-04");
  assert.equal(
    converted.imageUrl,
    "http://127.0.0.1:5519/v1/library/previews/nested/atlas-1.jpg?width=1100&root_path=%2FUsers%2Fexample%2FMy+Photos",
  );
  assert.deepEqual(
    basketItemFromSource(asset, 2, "http://127.0.0.1:5519/", "/Users/example/My Photos"),
    converted,
  );
  assert.deepEqual(
    basketItemFromSource(photo("photo-3"), 0, "ignored", null),
    basketItemFromPhotoAsset(photo("photo-3")),
  );
});
