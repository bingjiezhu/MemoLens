import { useEffect } from "react";

import {
  formatAssetDuration,
  formatCapturedDate,
  groupInboxMoments,
  isEditableTarget,
} from "./model";
import type { InboxAsset, InboxState } from "./types";
import { useMediaInbox } from "./useMediaInbox";

interface MediaInboxProps {
  apiBase: string;
  dbPath: string | null;
  canRead: boolean;
  canWrite: boolean;
  onSummaryChange?(summary: { inboxCount: number; hasMore: boolean }): void;
}

const FILTERS: Array<{ value: InboxState; label: string }> = [
  { value: "all", label: "All" },
  { value: "inbox", label: "Inbox" },
  { value: "kept", label: "Kept" },
  { value: "archived", label: "Archived" },
];

function safeThumbnailUrl(apiBase: string, thumbnailUrl: string | null): string | null {
  if (!thumbnailUrl) return null;
  try {
    const backend = new URL(`${apiBase.replace(/\/+$/, "")}/`);
    const thumbnail = new URL(thumbnailUrl, backend);
    return thumbnail.origin === backend.origin ? thumbnail.toString() : null;
  } catch {
    return null;
  }
}

function AssetCard({
  asset,
  apiBase,
  active,
  selected,
  canWrite,
  busy,
  onActivate,
  onSelect,
  onUpdate,
}: {
  asset: InboxAsset;
  apiBase: string;
  active: boolean;
  selected: boolean;
  canWrite: boolean;
  busy: boolean;
  onActivate(): void;
  onSelect(): void;
  onUpdate(patch: Parameters<ReturnType<typeof useMediaInbox>["update"]>[1], label: string): void;
}) {
  const thumbnail = safeThumbnailUrl(apiBase, asset.thumbnail_url);
  const duration = formatAssetDuration(asset.duration_ms);
  return (
    <article className={`inbox-asset-card${active ? " active" : ""}`} onFocus={onActivate}>
      <button className="inbox-asset-preview" type="button" onClick={onActivate} aria-label={`Focus ${asset.filename}`}>
        {thumbnail ? <img src={thumbnail} alt="" loading="lazy" /> : <span className="inbox-placeholder" aria-hidden="true">{asset.kind === "video" ? "▶" : "◇"}</span>}
        <span className="inbox-kind">{asset.kind}</span>
        {duration ? <span className="inbox-duration">{duration}</span> : null}
      </button>
      <div className="inbox-asset-copy">
        <div><strong title={asset.filename}>{asset.filename}</strong><span>{formatCapturedDate(asset.captured_at)}</span></div>
        <label className="inbox-select"><input type="checkbox" checked={selected} onChange={onSelect} aria-label={`Select ${asset.filename}`} /><span>Select</span></label>
      </div>
      <div className="inbox-primary-actions">
        <button
          type="button"
          onClick={() => onUpdate({ inbox_state: "kept" }, "Kept")}
          disabled={!canWrite || busy}
          aria-label={`Keep ${asset.filename}`}
        >
          Keep <kbd>K</kbd>
        </button>
        <button
          type="button"
          onClick={() => onUpdate({ inbox_state: "archived" }, "Archived")}
          disabled={!canWrite || busy}
          aria-label={`Archive ${asset.filename}; original file unchanged`}
        >
          Archive <kbd>A</kbd>
        </button>
      </div>
      <div className="inbox-secondary-actions">
        <button type="button" className={asset.review.favorite ? "active" : ""} aria-pressed={asset.review.favorite} onClick={() => onUpdate({ favorite: !asset.review.favorite }, asset.review.favorite ? "Favorite removed" : "Favorited")} disabled={!canWrite || busy}>♡ Favorite</button>
        <button type="button" className={asset.review.project_ready ? "active" : ""} aria-pressed={asset.review.project_ready} onClick={() => onUpdate({ project_ready: !asset.review.project_ready }, asset.review.project_ready ? "Ready removed" : "Marked ready")} disabled={!canWrite || busy}>✓ Ready</button>
      </div>
    </article>
  );
}

export function MediaInbox(props: MediaInboxProps) {
  const store = useMediaInbox({
    apiBase: props.apiBase,
    dbPath: props.dbPath,
    canRead: props.canRead,
    canWrite: props.canWrite,
  });
  const busy = store.phase === "saving";
  const activeAsset = store.items.find((asset) => asset.id === store.activeAssetId) ?? store.items[0] ?? null;
  const moments = groupInboxMoments(store.items);

  useEffect(() => {
    props.onSummaryChange?.({ inboxCount: store.summary.inbox, hasMore: false });
  }, [props.onSummaryChange, store.summary.inbox]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (isEditableTarget(event.target) || event.metaKey || event.ctrlKey || event.altKey) return;
      const key = event.key.toLowerCase();
      if (key === "z" && store.canUndo && store.canWrite) {
        event.preventDefault();
        void store.undo();
        return;
      }
      if (!activeAsset || !store.canWrite || busy) return;
      if (key === "k") {
        event.preventDefault();
        void store.update(activeAsset, { inbox_state: "kept" }, "Kept");
      } else if (key === "a") {
        event.preventDefault();
        void store.update(activeAsset, { inbox_state: "archived" }, "Archived");
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [activeAsset, busy, store]);

  return (
    <section className="section-block media-inbox" aria-labelledby="media-inbox-title">
      <div className="media-inbox-heading">
        <div>
          <p className="eyebrow">Media Inbox</p>
          <h2 id="media-inbox-title">Keep what matters. Leave originals alone.</h2>
          <p>Review photos and videos as creative material. Archive only changes MemoLens suggestions—the original file never moves or changes.</p>
        </div>
        {!store.canWrite ? <span className="status-pill">Browser · read only</span> : <span className="status-pill">Desktop · editable</span>}
      </div>

      <div className="inbox-toolbar">
        <div className="inbox-filter-tabs" role="tablist" aria-label="Media review state">
          {FILTERS.map((filter) => (
            <button
              key={filter.value}
              type="button"
              role="tab"
              aria-selected={store.state === filter.value}
              tabIndex={store.state === filter.value ? 0 : -1}
              className={store.state === filter.value ? "active" : ""}
              onClick={() => store.setState(filter.value)}
              onKeyDown={(event) => {
                const currentIndex = FILTERS.findIndex((entry) => entry.value === filter.value);
                const lastIndex = FILTERS.length - 1;
                const nextIndex = event.key === "Home"
                  ? 0
                  : event.key === "End"
                    ? lastIndex
                    : event.key === "ArrowRight"
                      ? (currentIndex + 1) % FILTERS.length
                      : event.key === "ArrowLeft"
                        ? (currentIndex - 1 + FILTERS.length) % FILTERS.length
                        : null;
                if (nextIndex === null) return;
                event.preventDefault();
                store.setState(FILTERS[nextIndex].value);
                const tabs = event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>("[role='tab']");
                tabs?.[nextIndex]?.focus();
              }}
            >
              {filter.label}
            </button>
          ))}
        </div>
        <div className="inbox-kind-filters" aria-label="Media kinds">
          {(["image", "video"] as const).map((kind) => (
            <label key={kind}>
              <input
                type="checkbox"
                checked={store.kinds.includes(kind)}
                onChange={() => store.toggleKind(kind)}
              />
              <span>{kind === "image" ? "Photos" : "Videos"}</span>
            </label>
          ))}
        </div>
        <button type="button" className="secondary-button compact-button" onClick={store.refresh} disabled={store.phase === "loading"}>Refresh</button>
      </div>

      {store.selectedIds.size > 0 ? (
        <div className="inbox-batch-bar">
          <strong>{store.selectedIds.size} selected</strong>
          <button type="button" onClick={() => void store.updateSelected({ inbox_state: "kept" }, "Kept")} disabled={!store.canWrite || busy}>Keep selected</button>
          <button type="button" onClick={() => void store.updateSelected({ inbox_state: "archived" }, "Archived")} disabled={!store.canWrite || busy}>Archive selected</button>
          <button type="button" onClick={store.clearSelected}>Clear</button>
        </div>
      ) : null}

      {store.phase === "loading" && store.items.length === 0 ? <div className="inbox-empty">Loading private media…</div> : null}
      {store.items.length > 0 ? (
        <div className="inbox-moments">
          {moments.map((moment) => (
            <section className="inbox-moment" key={moment.key} aria-labelledby={`inbox-moment-${moment.key}`}>
              <div className="inbox-moment-heading"><h3 id={`inbox-moment-${moment.key}`}>{moment.label}</h3><span>{moment.items.length} {moment.items.length === 1 ? "moment" : "moments"}</span></div>
              <div className="inbox-grid">
                {moment.items.map((asset) => (
                  <AssetCard
                    key={asset.id}
                    asset={asset}
                    apiBase={props.apiBase}
                    active={asset.id === activeAsset?.id}
                    selected={store.selectedIds.has(asset.id)}
                    canWrite={store.canWrite}
                    busy={busy}
                    onActivate={() => store.setActiveAssetId(asset.id)}
                    onSelect={() => store.toggleSelected(asset.id)}
                    onUpdate={(patch, label) => void store.update(asset, patch, label)}
                  />
                ))}
              </div>
            </section>
          ))}
        </div>
      ) : store.phase !== "loading" ? <div className="inbox-empty"><strong>Nothing in {store.state === "all" ? "this view" : store.state}.</strong><span>Newly indexed photos and videos arrive in Inbox.</span></div> : null}

      <div className="inbox-footer">
        <div className="inbox-shortcuts"><span><kbd>K</kbd> Keep</span><span><kbd>A</kbd> Archive</span><span><kbd>Z</kbd> Undo</span></div>
        <div className="inbox-footer-actions"><button type="button" className="secondary-button compact-button" onClick={store.later} disabled={!activeAsset}>Later</button>{store.canUndo ? <button type="button" className="secondary-button compact-button" onClick={() => void store.undo()} disabled={!store.canWrite || busy}>Undo</button> : null}{store.hasMore ? <button type="button" className="secondary-button compact-button" onClick={store.loadMore} disabled={store.phase === "loading"}>Load more</button> : null}</div>
      </div>

      <p className="sr-only" role="status" aria-live="polite">{store.announcement}</p>
      {store.announcement ? <p className="inbox-announcement" aria-hidden="true">{store.announcement}</p> : null}
      {store.error ? <p className="inline-error" role="alert">{store.error}</p> : null}
    </section>
  );
}
