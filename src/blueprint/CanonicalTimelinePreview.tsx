import {
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
} from "react";

import {
  buildPendingTimelinePreviewMediaItems,
  buildTimelinePreviewMediaItems,
  canonicalTimelinePreviewIdentity,
  clampTimelinePreviewPlayhead,
  isPendingTimelinePreviewForWorkspace,
  pendingTimelinePreviewIdentity,
  timelinePreviewClipAtPlayhead,
  timelinePreviewVideoSourceTimeMs,
} from "./timelinePreviewModel";
import type {
  CanonicalTimelinePendingPreview,
  CanonicalTimelineWorkspace,
} from "./timelineTypes";

export interface CanonicalTimelinePreviewProps {
  apiBase: string;
  workspace: CanonicalTimelineWorkspace;
  pendingPreview?: CanonicalTimelinePendingPreview | null;
  previewKey?: string;
}

function formatPreviewTime(valueMs: number): string {
  const bounded = Math.max(0, Math.round(valueMs));
  const minutes = Math.floor(bounded / 60_000);
  const seconds = Math.floor((bounded % 60_000) / 1_000);
  const tenths = Math.floor((bounded % 1_000) / 100);
  return `${minutes}:${String(seconds).padStart(2, "0")}.${tenths}`;
}

export function CanonicalTimelinePreview({
  apiBase,
  workspace,
  pendingPreview = null,
  previewKey = "",
}: CanonicalTimelinePreviewProps) {
  const pendingPreviewIsAdmissible = pendingPreview === null
    || isPendingTimelinePreviewForWorkspace(pendingPreview, workspace);
  const mode = pendingPreview === null ? "canonical" : "pending";
  const timeline = pendingPreview === null
    ? workspace.timeline
    : pendingPreviewIsAdmissible
      ? pendingPreview.timeline
      : null;
  const sourceBindings = pendingPreview === null
    ? workspace.source_bindings
    : pendingPreviewIsAdmissible
      ? pendingPreview.source_bindings
      : [];
  const identity = `${pendingPreview === null
    ? canonicalTimelinePreviewIdentity(workspace)
    : pendingTimelinePreviewIdentity(pendingPreview)}:${previewKey}`;
  const items = useMemo(
    () => pendingPreview === null
      ? buildTimelinePreviewMediaItems(apiBase, workspace)
      : pendingPreviewIsAdmissible
        ? buildPendingTimelinePreviewMediaItems(apiBase, pendingPreview)
        : [],
    [apiBase, identity, pendingPreview, pendingPreviewIsAdmissible, workspace],
  );
  const durationMs = timeline?.output.duration_ms ?? 0;
  const clips = useMemo(() => items.map((item) => item.clip), [items]);
  const [playheadMs, setPlayheadMs] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [mediaError, setMediaError] = useState<string | null>(null);
  const [failedThumbnails, setFailedThumbnails] = useState<Set<string>>(() => new Set());
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const playheadRef = useRef(0);
  const animationFrameRef = useRef<number | null>(null);
  const headingId = useId();
  const playheadOutputId = useId();
  const location = timelinePreviewClipAtPlayhead(clips, playheadMs);
  const activeItem = location === null ? null : items[location.index] ?? null;

  playheadRef.current = playheadMs;

  useLayoutEffect(() => {
    if (animationFrameRef.current !== null) {
      window.cancelAnimationFrame(animationFrameRef.current);
      animationFrameRef.current = null;
    }
    videoRef.current?.pause();
    playheadRef.current = 0;
    setPlayheadMs(0);
    setPlaying(false);
    setMediaError(null);
    setFailedThumbnails(new Set());
  }, [identity]);

  useEffect(() => () => {
    if (animationFrameRef.current !== null) {
      window.cancelAnimationFrame(animationFrameRef.current);
      animationFrameRef.current = null;
    }
    videoRef.current?.pause();
  }, []);

  useEffect(() => {
    const item = activeItem;
    const element = videoRef.current;
    if (item?.clip.media_kind !== "video" || element === null) return undefined;
    const clip = item.clip;
    const syncVideo = () => {
      element.muted = true;
      const targetSeconds = timelinePreviewVideoSourceTimeMs(
        clip,
        playheadRef.current,
      ) / 1_000;
      if (Math.abs(element.currentTime - targetSeconds) > 0.04) {
        element.currentTime = targetSeconds;
      }
      if (playing) {
        void element.play().catch(() => {
          setPlaying(false);
          setMediaError(
            `Inspection playback could not start for clip ${clip.clip_id}. No render or export was attempted.`,
          );
        });
      } else {
        element.pause();
      }
    };
    if (element.readyState >= HTMLMediaElement.HAVE_METADATA) {
      syncVideo();
      return undefined;
    }
    element.addEventListener("loadedmetadata", syncVideo, { once: true });
    return () => element.removeEventListener("loadedmetadata", syncVideo);
  }, [activeItem?.clip.clip_id, identity, playing]);

  useEffect(() => {
    if (!playing || durationMs <= 0 || activeItem === null) return undefined;
    let previousTimestamp = performance.now();
    const step = (timestamp: number) => {
      const elapsedMs = Math.max(0, Math.min(250, timestamp - previousTimestamp));
      previousTimestamp = timestamp;
      setPlayheadMs((current) => {
        const currentLocation = timelinePreviewClipAtPlayhead(clips, current);
        if (currentLocation === null) {
          setPlaying(false);
          return 0;
        }
        let next = current;
        if (currentLocation.clip.media_kind === "image") {
          next = current + elapsedMs;
        } else {
          const element = videoRef.current;
          if (
            element !== null
            && element.dataset.previewClipId === currentLocation.clip.clip_id
            && element.readyState >= HTMLMediaElement.HAVE_METADATA
          ) {
            const sourceTimeMs = element.currentTime * 1_000;
            next = sourceTimeMs >= currentLocation.clip.source_out_ms - 1
              ? currentLocation.clip.end_ms
              : currentLocation.clip.start_ms
                + Math.max(0, sourceTimeMs - currentLocation.clip.source_in_ms);
          }
        }
        const bounded = clampTimelinePreviewPlayhead(next, durationMs);
        playheadRef.current = bounded;
        if (bounded >= durationMs) setPlaying(false);
        return bounded;
      });
      animationFrameRef.current = window.requestAnimationFrame(step);
    };
    animationFrameRef.current = window.requestAnimationFrame(step);
    return () => {
      if (animationFrameRef.current !== null) {
        window.cancelAnimationFrame(animationFrameRef.current);
        animationFrameRef.current = null;
      }
    };
  }, [activeItem?.clip.clip_id, clips, durationMs, identity, playing]);

  function seek(nextPlayheadMs: number): void {
    const bounded = clampTimelinePreviewPlayhead(nextPlayheadMs, durationMs);
    playheadRef.current = bounded;
    setPlayheadMs(bounded);
    setMediaError(null);
    const nextLocation = timelinePreviewClipAtPlayhead(clips, bounded);
    const element = videoRef.current;
    if (
      nextLocation?.clip.media_kind === "video"
      && element !== null
      && element.dataset.previewClipId === nextLocation.clip.clip_id
      && element.readyState >= HTMLMediaElement.HAVE_METADATA
    ) {
      element.currentTime = timelinePreviewVideoSourceTimeMs(
        nextLocation.clip,
        bounded,
      ) / 1_000;
    }
  }

  function handleScrub(event: ChangeEvent<HTMLInputElement>): void {
    seek(Number(event.currentTarget.value));
  }

  function failActiveMedia(message: string): void {
    videoRef.current?.pause();
    setPlaying(false);
    setMediaError(message);
  }

  if (timeline === null || items.length !== sourceBindings.length) {
    return (
      <section
        className={`timeline-inspection-preview${mode === "pending" ? " pending" : ""}`}
        aria-label={mode === "pending" ? "Pending Timeline edit preview" : "Canonical Timeline inspection preview"}
      >
        <div className="timeline-preview-unavailable" role="status">
          Inspection preview is unavailable because the Timeline content bindings could not be adopted.
          No alternate content identity is inferred.
        </div>
      </section>
    );
  }

  return (
    <section
      className={`timeline-inspection-preview${mode === "pending" ? " pending" : ""}`}
      aria-labelledby={headingId}
    >
      <header className="timeline-preview-heading">
        <div>
          <p className="eyebrow">
            {mode === "pending" ? "Unsaved local preview" : "Read-only local inspection"}
          </p>
          <h5 id={headingId}>
            {mode === "pending" ? "Pending edit preview" : "Canonical Timeline inspection preview"}
          </h5>
        </div>
        <span>
          {mode === "pending" ? "Not saved · no export" : "Hard cuts · muted · no writes"}
        </span>
      </header>

      <div
        className="timeline-preview-stage"
        style={{ aspectRatio: timeline.output.aspect_ratio.replace(":", " / ") }}
      >
        {activeItem?.clip.media_kind === "image" && activeItem.thumbnailUrl !== null ? (
          <img
            alt={`Inspection still for Beat ${activeItem.clip.beat_id}`}
            src={activeItem.thumbnailUrl}
            onError={() => failActiveMedia(
              `The content-addressed image thumbnail for clip ${activeItem.clip.clip_id} is unavailable. No alternate content identity is inferred.`,
            )}
          />
        ) : activeItem?.clip.media_kind === "video" && activeItem.mediaUrl !== null ? (
          <video
            key={activeItem.clip.clip_id}
            ref={videoRef}
            data-preview-clip-id={activeItem.clip.clip_id}
            src={activeItem.mediaUrl}
            poster={activeItem.thumbnailUrl ?? undefined}
            muted
            playsInline
            preload="metadata"
            onError={() => failActiveMedia(
              `The content-addressed video media for clip ${activeItem.clip.clip_id} could not be loaded. No alternate content identity is inferred.`,
            )}
            onEnded={() => seek(activeItem.clip.end_ms)}
          />
        ) : (
          <div className="timeline-preview-unavailable" role="status">
            The current clip has no same-origin inspection media URL. No local path or fallback is exposed.
          </div>
        )}
        {activeItem !== null ? (
          <div className="timeline-preview-overlay">
            <strong>Beat {activeItem.clip.beat_id}</strong>
            <span>Clip {activeItem.clip.ordinal + 1} · {activeItem.clip.media_kind}</span>
          </div>
        ) : null}
      </div>

      {mediaError !== null ? (
        <p className="video-inline-error" role="alert">{mediaError}</p>
      ) : null}

      <div className="timeline-preview-controls">
        <button
          className="secondary-button compact-button"
          type="button"
          disabled={activeItem === null || mediaError !== null || durationMs <= 0}
          onClick={() => {
            if (playheadRef.current >= durationMs) seek(0);
            setPlaying((current) => !current);
          }}
        >
          {playing ? "Pause inspection" : "Play inspection"}
        </button>
        <label>
          <span className="sr-only">Inspection playhead</span>
          <input
            type="range"
            min={0}
            max={durationMs}
            step={10}
            value={Math.round(playheadMs)}
            onChange={handleScrub}
            aria-valuetext={`${formatPreviewTime(playheadMs)} of ${formatPreviewTime(durationMs)}`}
            aria-describedby={playheadOutputId}
          />
        </label>
        <output id={playheadOutputId}>{formatPreviewTime(playheadMs)} / {formatPreviewTime(durationMs)}</output>
      </div>

      <div className="timeline-preview-strip" aria-label="Exact hard-cut clip order">
        {items.map((item) => {
          const failedThumbnail = failedThumbnails.has(item.clip.clip_id);
          return (
            <button
              type="button"
              className={activeItem?.clip.clip_id === item.clip.clip_id ? "active" : ""}
              key={item.clip.clip_id}
              onClick={() => seek(item.clip.start_ms)}
              aria-label={`Inspect Beat ${item.clip.beat_id}, clip ${item.clip.ordinal + 1}`}
            >
              {item.thumbnailUrl !== null && !failedThumbnail ? (
                <img
                  alt=""
                  src={item.thumbnailUrl}
                  onError={() => setFailedThumbnails((current) => {
                    const next = new Set(current);
                    next.add(item.clip.clip_id);
                    return next;
                  })}
                />
              ) : (
                <span className="timeline-preview-thumb-missing">Preview unavailable</span>
              )}
              <small>Beat {item.clip.beat_id}</small>
              <strong>{formatPreviewTime(item.clip.start_ms)}–{formatPreviewTime(item.clip.end_ms)}</strong>
            </button>
          );
        })}
      </div>

      <p className="blueprint-honesty-note">
        {mode === "pending"
          ? "This preview applies one pending edit to a local copy. It is not a canonical revision and cannot drive render, export, publish, or Usage. Save and canonical reread must succeed before downstream use."
          : "This inspection preview reads the normalized Timeline and content-addressed media locally. It does not prove playback came from the fixed execution source binding, is not final render fidelity, does not establish approval, and cannot render, export, publish, or finalize Usage."}
      </p>
    </section>
  );
}
