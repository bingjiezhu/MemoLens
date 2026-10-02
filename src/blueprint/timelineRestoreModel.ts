import { isHistoricalTimelineWorkspaceForCurrentHead } from "./timelineModel";
import type {
  CanonicalTimelineHead,
  CanonicalTimelineRestoreFrom,
  CanonicalTimelineWorkspace,
} from "./timelineTypes.js";

const MAX_VISIBLE_HISTORY_REVISIONS = 100;

export interface CanonicalTimelinePendingRestore {
  object: "canonical_timeline.pending_restore";
  schema_version: "1";
  canonical: false;
  persisted: false;
  database_uuid: string;
  project_id: string;
  based_on_head: CanonicalTimelineHead;
  restore_from: CanonicalTimelineRestoreFrom;
  summary: string;
}

function fail(detail: string): never {
  throw new Error(`Canonical Timeline restore cannot be staged: ${detail}.`);
}

export function historicalTimelineRevisionNumbers(
  head: CanonicalTimelineHead | null,
): number[] {
  if (head === null || head.revision <= 1) return [];
  const oldestVisible = Math.max(
    1,
    head.revision - MAX_VISIBLE_HISTORY_REVISIONS,
  );
  return Array.from(
    { length: head.revision - oldestVisible },
    (_value, index) => head.revision - index - 1,
  );
}

export function timelineRestoreFrom(
  workspace: CanonicalTimelineWorkspace,
): CanonicalTimelineRestoreFrom {
  const selected = workspace.selected_revision;
  if (selected === null || selected.is_head) {
    return fail("the selected Timeline revision is not historical");
  }
  const { is_head: _isHead, ...restoreFrom } = selected;
  return restoreFrom;
}

export function stageCanonicalTimelineRestore(input: {
  current: CanonicalTimelineWorkspace;
  historical: CanonicalTimelineWorkspace;
}): CanonicalTimelinePendingRestore {
  if (!isHistoricalTimelineWorkspaceForCurrentHead(input.current, input.historical)) {
    return fail("the historical selection does not belong to the exact current head");
  }
  const head = input.current.head;
  if (head === null) return fail("the current Timeline head is missing");
  const restoreFrom = timelineRestoreFrom(input.historical);
  return {
    object: "canonical_timeline.pending_restore",
    schema_version: "1",
    canonical: false,
    persisted: false,
    database_uuid: input.current.database_uuid,
    project_id: input.current.project_id,
    based_on_head: { ...head },
    restore_from: { ...restoreFrom },
    summary: `Restore historical revision ${restoreFrom.revision} as revision ${head.revision + 1}`,
  };
}
