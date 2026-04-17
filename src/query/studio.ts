import { INITIAL_PROMPT, PHOTO_LIBRARY } from "./mockLibrary";
import type {
  DraftAnalysis,
  DraftResult,
  PhotoAsset,
  PipelineStep,
  ToneVariant,
} from "./types";

const PIPELINE_BLUEPRINT = [
  {
    id: "understand",
    title: "Interpret the request",
    detail: "Read mood, subject, timeframe, and overall tone from the prompt",
    metric: "tone + intent",
  },
  {
    id: "retrieve",
    title: "Search the library",
    detail: "Pull candidates from semantic cues, tags, and capture context",
    metric: "semantic + metadata",
  },
  {
    id: "curate",
    title: "Curate the nine",
    detail: "Remove near-duplicates and keep the set visually balanced",
    metric: "score + diversity",
  },
  {
    id: "compose",
    title: "Write the draft",
    detail: "Prepare the title, order, and a caption that is ready to use",
    metric: "title + caption",
  },
];

const KEYWORD_GROUPS: Record<string, string[]> = {
  soft: ["温柔", "柔和", "soft", "gentle", "softer"],
  daily: ["日常", "daily", "生活", "vlog", "普通日子"],
  memory: ["回忆", "memory", "纪念", "recent", "最近", "近半年"],
  quiet: ["安静", "quiet", "别太热闹", "不要太热闹", "calm"],
  city: ["城市", "street", "city", "downtown"],
  portrait: ["某个人", "人物", "portrait", "一个人", "主角"],
  friends: ["朋友", "friends", "聚会", "一起"],
  coast: ["海边", "coast", "ocean", "beach", "海"],
  coffee: ["咖啡", "coffee", "cafe"],
  walk: ["散步", "walk", "走路", "路上"],
  light: ["光", "光线", "window", "sunlight", "亮一点"],
  sunset: ["日落", "sunset", "傍晚", "golden hour"],
  travel: ["旅行", "travel", "roadtrip", "假期", "度假"],
  losangeles: ["洛杉矶", "los angeles", "la"],
  social: ["朋友圈", "发图", "post", "publish", "社交"],
};

function hasKeyword(prompt: string, key: string): boolean {
  const keywords = KEYWORD_GROUPS[key];
  if (!keywords) {
    return false;
  }
  return keywords.some((keyword) => prompt.includes(keyword));
}

function hashString(value: string): number {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash << 5) - hash + value.charCodeAt(index);
    hash |= 0;
  }
  return Math.abs(hash);
}

function dedupe(values: string[]): string[] {
  return [...new Set(values)];
}

function titleCase(value: string): string {
  return value
    .split(" ")
    .map((word) =>
      word.length > 0 ? word.charAt(0).toUpperCase() + word.slice(1) : word,
    )
    .join(" ");
}

export function createPipelineSteps(
  activeIndex: number | null,
  completedCount = PIPELINE_BLUEPRINT.length,
): PipelineStep[] {
  return PIPELINE_BLUEPRINT.map((step, index) => ({
    ...step,
    index: index + 1,
    status:
      index < completedCount
        ? "done"
        : activeIndex === index
          ? "active"
          : "pending",
  }));
}

export function analyzePrompt(rawPrompt: string): DraftAnalysis {
  const prompt = rawPrompt.trim().toLowerCase();
  const focusTokens = Object.keys(KEYWORD_GROUPS).filter((key) =>
    hasKeyword(prompt, key),
  );

  const locationLabel = hasKeyword(prompt, "losangeles")
    ? "Around Los Angeles"
    : hasKeyword(prompt, "coast")
      ? "Coastline or roadside"
      : "Local library";

  const toneLabel = hasKeyword(prompt, "soft")
    ? "Soft narrative"
    : hasKeyword(prompt, "quiet")
      ? "Quiet restraint"
      : hasKeyword(prompt, "memory")
        ? "Memory-driven"
        : "Balanced natural";

  const focus = hasKeyword(prompt, "portrait")
    ? "Portrait-led"
    : hasKeyword(prompt, "friends")
      ? "Relationship-led"
      : hasKeyword(prompt, "coast")
        ? "Coastal story"
        : hasKeyword(prompt, "city")
          ? "City everyday"
          : "Life fragments";

  const useCase = hasKeyword(prompt, "social")
    ? "Social post draft"
    : hasKeyword(prompt, "memory")
      ? "Memory set"
      : "Curated selection";

  const timeHint =
    prompt.includes("最近半年") || prompt.includes("半年")
      ? "Last six months"
      : prompt.includes("最近")
        ? "Recent"
        : prompt.includes("去年")
          ? "Last year"
          : "Any time";

  return {
    focus,
    toneLabel,
    timeHint,
    useCase,
    locationLabel,
    tokens: dedupe(
      focusTokens.map((token) =>
        token === "social" ? "publishable" : titleCase(token),
      ),
    ).slice(0, 5),
  };
}

function scorePhoto(
  photo: PhotoAsset,
  analysis: DraftAnalysis,
  prompt: string,
  variant: ToneVariant,
  seed: number,
): number {
  let score = 8;

  for (const concept of photo.concepts) {
    if (hasKeyword(prompt, concept)) {
      score += 4.2;
    }
  }

  if (analysis.focus === "Portrait-led" && photo.concepts.includes("portrait")) {
    score += 3.4;
  }
  if (analysis.focus === "Relationship-led" && photo.concepts.includes("friends")) {
    score += 3.2;
  }
  if (analysis.focus === "Coastal story" && photo.concepts.includes("coast")) {
    score += 3.4;
  }
  if (analysis.focus === "City everyday" && photo.concepts.includes("city")) {
    score += 3;
  }

  if (analysis.toneLabel === "Soft narrative") {
    if (
      photo.concepts.some((concept) =>
        ["soft", "quiet", "light", "memory"].includes(concept),
      )
    ) {
      score += 2.8;
    }
  }

  if (analysis.toneLabel === "Quiet restraint" && photo.concepts.includes("quiet")) {
    score += 2.6;
  }

  if (analysis.locationLabel === "Around Los Angeles" && photo.concepts.includes("losangeles")) {
    score += 1.8;
  }

  if (variant === "soft") {
    if (
      photo.concepts.some((concept) =>
        ["soft", "quiet", "light", "warm"].includes(concept),
      )
    ) {
      score += 2.4;
    }
    if (photo.concepts.includes("city")) {
      score -= 0.6;
    }
  }

  const jitter = (hashString(`${photo.id}-${seed}`) % 17) / 10;
  return score + jitter;
}

function selectDiversePhotos(sortedPhotos: PhotoAsset[]): PhotoAsset[] {
  const selected: PhotoAsset[] = [];
  const seenSlots = new Set<string>();

  for (const photo of sortedPhotos) {
    if (!seenSlots.has(photo.slot)) {
      selected.push(photo);
      seenSlots.add(photo.slot);
    }
    if (selected.length === 9) {
      return selected;
    }
  }

  for (const photo of sortedPhotos) {
    if (!selected.some((item) => item.id === photo.id)) {
      selected.push(photo);
    }
    if (selected.length === 9) {
      break;
    }
  }

  return selected;
}

function buildTitle(analysis: DraftAnalysis, variant: ToneVariant): string {
  if (analysis.focus === "Portrait-led") {
    return variant === "soft" ? "A softer portrait sequence" : "Recent moments, centered on one person";
  }
  if (analysis.focus === "Relationship-led") {
    return variant === "soft" ? "Quiet time with friends" : "Companionship, edited with care";
  }
  if (analysis.focus === "Coastal story") {
    return variant === "soft" ? "Where the coast slows everything down" : "Light from those coastal days";
  }
  if (analysis.focus === "City everyday") {
    return variant === "soft" ? "A quieter stretch of the city" : "Recent life, arranged with intent";
  }

  return variant === "soft" ? "Make the ordinary feel lighter" : "Recent life, arranged with intent";
}

function buildCaption(analysis: DraftAnalysis, variant: ToneVariant): string {
  if (analysis.focus === "Portrait-led") {
    return variant === "soft"
      ? "What stays with you is not the big event, but the quieter moments where light, posture, and mood all line up. Set together, they feel closer to the truth of that stretch of time."
      : "When recent moments around one person are edited into a sequence, even ordinary pauses and walks start to feel like a story.";
  }

  if (analysis.focus === "Relationship-led") {
    return variant === "soft"
      ? "Time with people you love rarely needs much explanation. A laugh, a pause, a little sunlight is often enough to make the memory stay."
      : "This set keeps the moments between friends that never needed staging in the first place. Lively enough to feel warm, calm enough to stay relaxed.";
  }

  if (analysis.focus === "Coastal story") {
    return variant === "soft"
      ? "Sea air, roadside light, late afternoon, and a slower state of mind come together here as the part of the trip most worth keeping."
      : "Those coastal days were not packed with plans, but the light and air carried enough atmosphere to become a set that already feels ready to post.";
  }

  if (analysis.toneLabel === "Soft narrative") {
    return "A slower kind of everyday life can still be worth keeping. Light, walks, coffee, and quieter evenings come together here as one mood worth returning to.";
  }

  if (analysis.toneLabel === "Quiet restraint") {
    return "Once the louder frames are removed, what remains feels closer to what was actually worth remembering. Quieter, and easier to stay with.";
  }

  return "Reordering recent photos into a set makes it clear that what lasts is rarely the event itself. It is usually the mood that makes you want to look again.";
}

function buildNotes(analysis: DraftAnalysis, selected: PhotoAsset[]): string[] {
  const narrative = selected.slice(0, 3).map((photo) => photo.title).join(" / ");
  const slotMix = dedupe(selected.map((photo) => photo.slot)).join(" + ");

  return [
    `The sequence opens as a ${analysis.focus.toLowerCase()} edit, led by ${narrative}.`,
    `The set stays ${analysis.toneLabel.toLowerCase()} without repeating the same framing over and over.`,
    `The current order covers ${slotMix} and reads more like one publishable set than a dump of search results.`,
  ];
}

export function createDraft(
  rawPrompt = INITIAL_PROMPT,
  variant: ToneVariant = "balanced",
  seed = 1,
): DraftResult {
  const prompt = rawPrompt.trim() || INITIAL_PROMPT;
  const normalizedPrompt = prompt.toLowerCase();
  const analysis = analyzePrompt(normalizedPrompt);

  const rankedPhotos = PHOTO_LIBRARY.map((photo) => ({
    ...photo,
    score: scorePhoto(photo, analysis, normalizedPrompt, variant, seed),
  })).sort((left, right) => (right.score ?? 0) - (left.score ?? 0));

  const selected = selectDiversePhotos(rankedPhotos).map((photo, index) => ({
    ...photo,
    score: Number(((photo.score ?? 0) - index * 0.18).toFixed(2)),
  }));

  return {
    id: `draft-${seed}`,
    prompt,
    title: buildTitle(analysis, variant),
    caption: buildCaption(analysis, variant),
    candidateCount: 108 + analysis.tokens.length * 7 + (variant === "soft" ? 11 : 0),
    selectedCount: selected.length,
    selected,
    analysis,
    notes: buildNotes(analysis, selected),
    parsedQuery: null,
  };
}
