import { useCallback, useEffect, useMemo, useState } from "react";

import { PendingMutationKeyStore, type MutationPayload } from "../creator/mutationKeyStore";
import { fetchInbox, InboxApiError, putAssetReview } from "./api";
import { InboxReadCoordinator } from "./readCoordinator";
import {
  isAssetVisible,
  mergeInboxAssets,
  nextAssetId,
  resolveLoadedActiveAssetId,
  transitionInboxSummary,
} from "./model";
import type {
  InboxAsset,
  InboxMediaKind,
  InboxPage,
  InboxState,
  ReviewUpdate,
} from "./types";

interface UndoEntry {
  asset: InboxAsset;
  committed: InboxAsset;
}

export interface MediaInboxStore {
  items: InboxAsset[];
  state: InboxState;
  setState(state: InboxState): void;
  kinds: InboxMediaKind[];
  toggleKind(kind: InboxMediaKind): void;
  phase: "idle" | "loading" | "ready" | "saving" | "error";
  error: string | null;
  announcement: string;
  activeAssetId: string | null;
  setActiveAssetId(id: string | null): void;
  selectedIds: Set<string>;
  toggleSelected(id: string): void;
  clearSelected(): void;
  hasMore: boolean;
  summary: Record<"all" | "inbox" | "kept" | "archived", number>;
  canWrite: boolean;
  canUndo: boolean;
  refresh(): void;
  loadMore(): void;
  update(asset: InboxAsset, patch: ReviewUpdate, label: string): Promise<boolean>;
  updateSelected(patch: ReviewUpdate, label: string): Promise<void>;
  later(): void;
  undo(): Promise<void>;
}

export function useMediaInbox(input: {
  apiBase: string;
  dbPath: string | null;
  canRead: boolean;
  canWrite: boolean;
}): MediaInboxStore {
  const [items, setItems] = useState<InboxAsset[]>([]);
  const [state, setState] = useState<InboxState>("inbox");
  const [kinds, setKinds] = useState<InboxMediaKind[]>(["image", "video"]);
  const [page, setPage] = useState<Pick<InboxPage, "next_cursor" | "has_more">>({ next_cursor: null, has_more: false });
  const [summary, setSummary] = useState<InboxPage["summary"]>({ all: 0, inbox: 0, kept: 0, archived: 0 });
  const [phase, setPhase] = useState<MediaInboxStore["phase"]>("idle");
  const [error, setError] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const [activeAssetId, setActiveAssetId] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [undoEntry, setUndoEntry] = useState<UndoEntry | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const kindsKey = useMemo(() => [...kinds].sort().join(","), [kinds]);
  const mutationKeys = useMemo(() => new PendingMutationKeyStore(window.localStorage), []);
  const reads = useMemo(() => new InboxReadCoordinator(), []);

  const load = useCallback(async (cursor: string | null, append: boolean, epoch: number) => {
    const ticket = reads.begin(epoch, cursor);
    if (!ticket) return;
    if (!input.canRead || !input.dbPath || kinds.length === 0) {
      if (reads.isCurrent(ticket)) {
        setItems([]);
        setPage({ next_cursor: null, has_more: false });
        setSummary({ all: 0, inbox: 0, kept: 0, archived: 0 });
        setActiveAssetId(null);
        setSelectedIds(new Set());
        setPhase("idle");
      }
      reads.settle(ticket);
      return;
    }
    setPhase("loading");
    setError(null);
    try {
      const nextPage = await fetchInbox({
        apiBase: input.apiBase,
        dbPath: input.dbPath,
        state,
        kinds,
        cursor,
        signal: ticket.controller.signal,
      });
      if (!reads.isCurrent(ticket)) return;
      setItems((current) => append ? mergeInboxAssets(current, nextPage.items) : nextPage.items);
      setPage(nextPage);
      setSummary(nextPage.summary);
      if (!append) {
        setActiveAssetId((current) => resolveLoadedActiveAssetId(current, nextPage.items, false));
      } else {
        setActiveAssetId((current) => resolveLoadedActiveAssetId(current, nextPage.items, true));
      }
      if (!append) setSelectedIds(new Set());
      setPhase("ready");
    } catch (reason) {
      if (!reads.isCurrent(ticket)) return;
      setError(reason instanceof Error ? reason.message : "Media Inbox could not be loaded.");
      setPhase("error");
    } finally {
      reads.settle(ticket);
    }
  }, [input.apiBase, input.canRead, input.dbPath, kindsKey, reads, state]);

  useEffect(() => {
    const epoch = reads.replace();
    void load(null, false, epoch);
    return () => {
      reads.replace();
    };
  }, [load, reads, reloadKey]);

  const update = useCallback(async (
    asset: InboxAsset,
    patch: ReviewUpdate,
    label: string,
  ): Promise<boolean> => {
    if (!input.canWrite || !input.dbPath) {
      setAnnouncement("Open MemoLens Desktop to change review state.");
      return false;
    }
    reads.replace();
    setPhase("saving");
    setError(null);
    const payload: MutationPayload = {
      db_path: input.dbPath,
      asset_id: asset.id,
      base_revision: asset.review.revision,
      ...patch,
    };
    const scope = `asset-review:${input.dbPath}:${asset.id}`;
    const key = await mutationKeys.acquire(scope, payload);
    try {
      const review = await putAssetReview({
        apiBase: input.apiBase,
        dbPath: input.dbPath,
        assetId: asset.id,
        baseRevision: asset.review.revision,
        update: patch,
        idempotencyKey: key,
      });
      await mutationKeys.settle(scope, key);
      const committed = { ...asset, review };
      setSummary((current) => transitionInboxSummary(
        current,
        asset.review.inbox_state,
        review.inbox_state,
      ));
      setItems((current) => current
        .map((item) => item.id === asset.id ? committed : item)
        .filter((item) => isAssetVisible(item, state)));
      setSelectedIds((current) => {
        const next = new Set(current);
        next.delete(asset.id);
        return next;
      });
      setUndoEntry({ asset, committed });
      setAnnouncement(`${label}: ${asset.filename}. Original file unchanged.`);
      setActiveAssetId((current) => nextAssetId(items.filter((item) => item.id !== asset.id), current));
      setPhase("ready");
      return true;
    } catch (reason) {
      await mutationKeys.settle(scope, key, reason);
      const conflict = reason instanceof InboxApiError && reason.code === "review_revision_conflict";
      setError(conflict
        ? "This item changed in another view. The inbox has been refreshed."
        : reason instanceof Error ? reason.message : "The review change could not be saved.");
      setPhase("error");
      if (conflict) setReloadKey((current) => current + 1);
      return false;
    }
  }, [input.apiBase, input.canWrite, input.dbPath, items, mutationKeys, reads, state]);

  const updateSelected = useCallback(async (patch: ReviewUpdate, label: string) => {
    const selected = items.filter((asset) => selectedIds.has(asset.id));
    let updated = 0;
    for (const asset of selected) {
      if (await update(asset, patch, label)) updated += 1;
    }
    if (updated > 1) setAnnouncement(`${label}: ${updated} items. Original files unchanged.`);
  }, [items, selectedIds, update]);

  const undo = useCallback(async () => {
    if (!undoEntry || !input.canWrite || !input.dbPath) return;
    reads.replace();
    setPhase("saving");
    setError(null);
    const patch: ReviewUpdate = {
      inbox_state: undoEntry.asset.review.inbox_state,
      favorite: undoEntry.asset.review.favorite,
      project_ready: undoEntry.asset.review.project_ready,
      note: undoEntry.asset.review.note,
    };
    const payload: MutationPayload = {
      db_path: input.dbPath,
      asset_id: undoEntry.asset.id,
      base_revision: undoEntry.committed.review.revision,
      ...patch,
    };
    const scope = `asset-review:${input.dbPath}:${undoEntry.asset.id}`;
    const key = await mutationKeys.acquire(scope, payload);
    try {
      const review = await putAssetReview({
        apiBase: input.apiBase,
        dbPath: input.dbPath,
        assetId: undoEntry.asset.id,
        baseRevision: undoEntry.committed.review.revision,
        update: patch,
        idempotencyKey: key,
      });
      await mutationKeys.settle(scope, key);
      const restored = { ...undoEntry.asset, review };
      setSummary((current) => transitionInboxSummary(
        current,
        undoEntry.committed.review.inbox_state,
        review.inbox_state,
      ));
      setItems((current) => isAssetVisible(restored, state)
        ? mergeInboxAssets(current, [restored])
        : current.filter((asset) => asset.id !== restored.id));
      setUndoEntry(null);
      setAnnouncement(`Undone: ${restored.filename}.`);
      setActiveAssetId(restored.id);
      setPhase("ready");
    } catch (reason) {
      await mutationKeys.settle(scope, key, reason);
      setError(reason instanceof Error ? reason.message : "Undo could not be saved.");
      setPhase("error");
    }
  }, [input.apiBase, input.canWrite, input.dbPath, mutationKeys, reads, state, undoEntry]);

  return {
    items,
    state,
    setState: (nextState) => {
      if (nextState === state) return;
      reads.replace();
      setState(nextState);
    },
    kinds,
    toggleKind: (kind) => {
      reads.replace();
      setKinds((current) => current.includes(kind)
        ? current.filter((value) => value !== kind)
        : [...current, kind]);
    },
    phase,
    error,
    announcement,
    activeAssetId,
    setActiveAssetId,
    selectedIds,
    toggleSelected: (id) => setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    }),
    clearSelected: () => setSelectedIds(new Set()),
    hasMore: page.has_more,
    summary,
    canWrite: input.canWrite,
    canUndo: Boolean(undoEntry),
    refresh: () => {
      reads.replace();
      setReloadKey((current) => current + 1);
    },
    loadMore: () => {
      if (page.next_cursor) {
        void load(page.next_cursor, true, reads.currentEpoch());
      }
    },
    update,
    updateSelected,
    later: () => {
      setActiveAssetId((current) => nextAssetId(items, current));
      setAnnouncement("Left in Inbox for later. No revision written.");
    },
    undo,
  };
}
