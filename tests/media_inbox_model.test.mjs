import assert from "node:assert/strict";
import test from "node:test";

import {
  applyReviewUpdate,
  groupInboxMoments,
  normalizeInboxPage,
  resolveLoadedActiveAssetId,
  transitionInboxSummary,
} from "../src/library/model.ts";
import { InboxReadCoordinator } from "../src/library/readCoordinator.ts";

function asset(id, capturedAt = null) {
  return {
    id,
    kind: "image",
    filename: `${id}.jpg`,
    captured_at: capturedAt,
    width: 1200,
    height: 900,
    duration_ms: null,
    thumbnail_url: `/thumb/${id}`,
    review: {
      revision: 0,
      inbox_state: "inbox",
      favorite: false,
      project_ready: false,
      note: null,
      created_at: null,
    },
  };
}

test("inbox envelope normalizer keeps summary and safe review defaults", () => {
  const page = normalizeInboxPage({
    data: [asset("a", "2026-08-12T10:00:00Z")],
    summary: { inbox: 18, kept: 4, archived: 2, all: 24 },
    next_cursor: "opaque",
    has_more: true,
  });
  assert.equal(page.items[0].review.revision, 0);
  assert.equal(page.summary.inbox, 18);
  assert.equal(page.next_cursor, "opaque");
  assert.equal(page.has_more, true);
});

test("moment grouping uses captured day and keeps unknown dates separate", () => {
  const moments = groupInboxMoments([
    asset("a", "2026-08-12T10:00:00Z"),
    asset("b", "2026-08-12T21:00:00-07:00"),
    asset("c", "2026-08-11T10:00:00Z"),
    asset("d"),
  ]);
  assert.deepEqual(moments.map((moment) => [moment.key, moment.items.length]), [
    ["2026-08-12", 2],
    ["2026-08-11", 1],
    ["unknown", 1],
  ]);
});

test("loading another page preserves the active asset from the prior page", () => {
  const nextPage = [asset("c"), asset("d")];
  assert.equal(resolveLoadedActiveAssetId("a", nextPage, true), "a");
  assert.equal(resolveLoadedActiveAssetId("missing", nextPage, false), "c");
  assert.equal(resolveLoadedActiveAssetId(null, nextPage, true), "c");
});

test("review update is immutable and Later can remain a no-op", () => {
  const current = asset("a").review;
  const next = applyReviewUpdate(current, { inbox_state: "archived", favorite: true });
  assert.equal(current.inbox_state, "inbox");
  assert.equal(next.inbox_state, "archived");
  assert.equal(next.favorite, true);
  assert.deepEqual(applyReviewUpdate(current, {}), current);
  assert.deepEqual(
    transitionInboxSummary({ all: 4, inbox: 3, kept: 1, archived: 0 }, "inbox", "archived"),
    { all: 4, inbox: 2, kept: 1, archived: 1 },
  );
});

test("read epochs abort stale pages and suppress duplicate cursors", () => {
  const reads = new InboxReadCoordinator();
  const inboxEpoch = reads.replace();
  const firstPage = reads.begin(inboxEpoch, null);
  const secondPage = reads.begin(inboxEpoch, "page-2");
  assert.ok(firstPage);
  assert.ok(secondPage);
  assert.equal(reads.begin(inboxEpoch, "page-2"), null);

  const archivedEpoch = reads.replace();
  assert.equal(firstPage.controller.signal.aborted, true);
  assert.equal(secondPage.controller.signal.aborted, true);
  assert.equal(reads.isCurrent(firstPage), false);
  assert.equal(reads.isCurrent(secondPage), false);
  assert.equal(reads.begin(inboxEpoch, "page-3"), null);

  const archivedFirstPage = reads.begin(archivedEpoch, null);
  assert.ok(archivedFirstPage);
  assert.equal(reads.isCurrent(archivedFirstPage), true);
  reads.settle(archivedFirstPage);
  assert.equal(reads.isCurrent(archivedFirstPage), false);
});
