import type {
  CreativeTimeline,
  TimelineClip,
  TimelineOperation,
  TimelineTrack,
} from "./types";

export interface ParsedTimelineCommand {
  operations: TimelineOperation[];
  summaries: string[];
  unrecognized: string[];
}

export function formatMilliseconds(value: number | null | undefined): string {
  const total = Math.max(0, Math.round(Number(value) || 0));
  const minutes = Math.floor(total / 60_000);
  const seconds = Math.floor((total % 60_000) / 1000);
  const milliseconds = total % 1000;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}.${String(milliseconds).padStart(3, "0")}`;
}

export function editableTimelineTrack(timeline: CreativeTimeline): TimelineTrack | null {
  return timeline.tracks.find((track) => track.type === "video")
    ?? timeline.tracks.find((track) => track.type === "image")
    ?? timeline.tracks.find((track) => track.clips.length > 0)
    ?? null;
}

export function editableTimelineClips(timeline: CreativeTimeline): TimelineClip[] {
  return editableTimelineTrack(timeline)?.clips ?? [];
}

function chineseNumber(value: string): number | null {
  if (/^\d+$/.test(value)) return Number(value);
  const digits: Record<string, number> = {
    零: 0,
    一: 1,
    二: 2,
    两: 2,
    三: 3,
    四: 4,
    五: 5,
    六: 6,
    七: 7,
    八: 8,
    九: 9,
  };
  if (value === "十") return 10;
  if (value.startsWith("十")) return 10 + (digits[value.slice(1)] ?? 0);
  if (value.endsWith("十")) return (digits[value.slice(0, -1)] ?? 0) * 10;
  if (value.includes("十")) {
    const [tens, ones] = value.split("十");
    return (digits[tens] ?? 0) * 10 + (digits[ones] ?? 0);
  }
  return digits[value] ?? null;
}

function referencedClipIndex(clause: string, clipCount: number): number | null {
  if (/(最后|last)\s*(一个|一条|一段|一张)?\s*(镜头|片段|素材|图片?|clip|image)?/i.test(clause)) {
    return clipCount > 0 ? clipCount - 1 : null;
  }
  if (/(第一|first)\s*(个|一条|一段|一张)?\s*(镜头|片段|素材|图片?|clip|image)?/i.test(clause)) {
    return clipCount > 0 ? 0 : null;
  }
  const match = clause.match(/第\s*([\d零一二两三四五六七八九十]+)\s*(?:个|条|段)?\s*(?:镜头|片段|素材|clip)?/i)
    ?? clause.match(/(?:clip|shot)\s*#?\s*(\d+)/i);
  if (!match) return null;
  const ordinal = chineseNumber(match[1]);
  if (ordinal === null || ordinal < 1 || ordinal > clipCount) return null;
  return ordinal - 1;
}

function parseDurationDeltaMs(clause: string): number | null {
  const match = clause.match(/(\d+(?:\.\d+)?)\s*(毫秒|ms|秒|seconds?|s)/i);
  if (!match) return null;
  const amount = Number(match[1]);
  if (!Number.isFinite(amount) || amount <= 0) return null;
  return /(毫秒|ms)/i.test(match[2]) ? Math.round(amount) : Math.round(amount * 1000);
}

function trimOperation(clip: TimelineClip, nextDuration: number): TimelineOperation {
  const duration = Math.max(100, Math.round(nextDuration));
  if (clip.kind === "image") {
    return {
      op: "set_duration",
      clip_id: clip.id,
      timeline_duration_ms: duration,
    };
  }
  if (typeof clip.source_in_ms === "number" && typeof clip.source_out_ms === "number") {
    return {
      op: "trim_clip",
      clip_id: clip.id,
      source_in_ms: clip.source_in_ms,
      source_out_ms: clip.source_in_ms + duration,
    };
  }
  return {
    op: "set_duration",
    clip_id: clip.id,
    timeline_duration_ms: duration,
  };
}

export function parseTimelineInstruction(
  instruction: string,
  timeline: CreativeTimeline,
): ParsedTimelineCommand {
  const track = editableTimelineTrack(timeline);
  const clips = track?.clips ?? [];
  const clauses = instruction
    .split(/[,;，；]|\r?\n|\s+(?:and then|then|and)\s+|然后|并且|再把/gi)
    .map((part) => part.trim())
    .filter(Boolean);
  const operations: TimelineOperation[] = [];
  const summaries: string[] = [];
  const unrecognized: string[] = [];
  const virtualDurations = new Map(clips.map((clip) => [clip.id, clip.timeline_duration_ms]));

  for (const clause of clauses) {
    const clipIndex = referencedClipIndex(clause, clips.length);
    const clip = clipIndex === null ? null : clips[clipIndex] ?? null;

    if (/(竖屏|portrait|9\s*:\s*16)/i.test(clause)) {
      operations.push({ op: "set_format", width: 1080, height: 1920, fps: timeline.format.fps });
      summaries.push("将画幅设为 9:16 竖屏");
      continue;
    }
    if (/(横屏|landscape|16\s*:\s*9)/i.test(clause)) {
      operations.push({ op: "set_format", width: 1920, height: 1080, fps: timeline.format.fps });
      summaries.push("将画幅设为 16:9 横屏");
      continue;
    }
    if (/(方形|square|1\s*:\s*1)/i.test(clause)) {
      operations.push({ op: "set_format", width: 1080, height: 1080, fps: timeline.format.fps });
      summaries.push("将画幅设为 1:1 方形");
      continue;
    }
    if (clip && /(删除|移除|去掉|delete|remove)/i.test(clause)) {
      operations.push({ op: "delete_clip", clip_id: clip.id });
      summaries.push(`删除第 ${clipIndex! + 1} 个镜头`);
      continue;
    }
    if (clip && /(缩短|减少|shorten|trim)/i.test(clause)) {
      const delta = parseDurationDeltaMs(clause);
      if (delta !== null) {
        const nextDuration = Math.max(100, (virtualDurations.get(clip.id) ?? clip.timeline_duration_ms) - delta);
        operations.push(trimOperation(clip, nextDuration));
        virtualDurations.set(clip.id, nextDuration);
        summaries.push(`将第 ${clipIndex! + 1} 个镜头缩短 ${formatMilliseconds(delta)}`);
        continue;
      }
    }
    if (clip && /(延长|加长|extend|longer)/i.test(clause)) {
      const delta = parseDurationDeltaMs(clause);
      if (delta !== null) {
        const nextDuration = (virtualDurations.get(clip.id) ?? clip.timeline_duration_ms) + delta;
        operations.push(trimOperation(clip, nextDuration));
        virtualDurations.set(clip.id, nextDuration);
        summaries.push(`将第 ${clipIndex! + 1} 个镜头延长到 ${formatMilliseconds(nextDuration)}`);
        continue;
      }
    }
    if (clip && /(移到|移动到|move\s+to)/i.test(clause)) {
      const ordinals = [...clause.matchAll(/第\s*([\d零一二两三四五六七八九十]+)/g)]
        .map((match) => chineseNumber(match[1]))
        .filter((value): value is number => value !== null);
      const targetOrdinal = ordinals.length >= 2 ? ordinals[ordinals.length - 1] : null;
      if (targetOrdinal && targetOrdinal <= clips.length) {
        operations.push({ op: "move_clip", clip_id: clip.id, to_index: targetOrdinal - 1 });
        summaries.push(`将第 ${clipIndex! + 1} 个镜头移到第 ${targetOrdinal} 位`);
        continue;
      }
    }
    if (clip && /(淡出|淡入|crossfade|fade)/i.test(clause)) {
      unrecognized.push(`${clause} (preview 0.3 supports deterministic cuts only)`);
      continue;
    }
    unrecognized.push(clause);
  }

  return { operations, summaries, unrecognized };
}
