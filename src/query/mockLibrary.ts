import type { PhotoAsset, PromptPreset } from "./types";

interface ArtworkPalette {
  sky: string;
  glow: string;
  horizon: string;
  ground: string;
  ink: string;
}

interface MockPhotoSeed {
  id: string;
  title: string;
  summary: string;
  location: string;
  takenAt: string;
  slot: string;
  concepts: string[];
  surfaceTint: string;
  palette: ArtworkPalette;
}

function buildArtworkDataUrl(
  title: string,
  slot: string,
  palette: ArtworkPalette,
): string {
  const svg = `
    <svg xmlns="http://www.w3.org/2000/svg" width="800" height="640" viewBox="0 0 800 640">
      <defs>
        <linearGradient id="bg" x1="0" x2="1" y1="0" y2="1">
          <stop offset="0%" stop-color="${palette.sky}" />
          <stop offset="54%" stop-color="${palette.glow}" />
          <stop offset="100%" stop-color="${palette.horizon}" />
        </linearGradient>
      </defs>
      <rect width="800" height="640" rx="42" fill="url(#bg)" />
      <circle cx="620" cy="154" r="94" fill="${palette.glow}" opacity="0.72" />
      <path d="M0 408 C134 346 226 320 332 338 C412 352 486 392 584 394 C666 396 728 364 800 314 V640 H0 Z" fill="${palette.horizon}" opacity="0.75" />
      <path d="M0 468 C140 444 236 406 340 426 C450 446 562 534 800 484 V640 H0 Z" fill="${palette.ground}" opacity="0.9" />
      <rect x="34" y="34" width="132" height="40" rx="20" fill="rgba(255,255,255,0.26)" />
      <text x="100" y="60" text-anchor="middle" font-family="Manrope, sans-serif" font-size="18" font-weight="700" fill="${palette.ink}">
        ${slot.toUpperCase()}
      </text>
      <rect x="34" y="534" width="732" height="72" rx="24" fill="rgba(255,255,255,0.3)" />
      <text x="64" y="582" font-family="Manrope, sans-serif" font-size="34" font-weight="700" fill="${palette.ink}">
        ${title}
      </text>
    </svg>
  `;

  return `data:image/svg+xml;charset=UTF-8,${encodeURIComponent(svg)}`;
}

const PHOTO_SEEDS: MockPhotoSeed[] = [
  {
    id: "photo-window-pause",
    title: "Window Pause",
    summary: "Soft window light makes the scene feel like an ordinary day worth keeping.",
    location: "Los Angeles · Silver Lake",
    takenAt: "2026-02-14",
    slot: "cover",
    concepts: ["soft", "daily", "portrait", "window", "quiet", "losangeles"],
    surfaceTint: "#d8cdbd",
    palette: {
      sky: "#f0dfcf",
      glow: "#f7efe6",
      horizon: "#cdbda8",
      ground: "#9f8f77",
      ink: "#2a221c",
    },
  },
  {
    id: "photo-coffee-counter",
    title: "Coffee Pause",
    summary: "A cup, a tabletop edge, and a little negative space make a clean pause inside the set.",
    location: "Los Angeles · Echo Park",
    takenAt: "2026-02-17",
    slot: "detail",
    concepts: ["coffee", "soft", "detail", "daily", "quiet", "losangeles"],
    surfaceTint: "#ddd2c3",
    palette: {
      sky: "#e4d5c6",
      glow: "#f7eee4",
      horizon: "#d0bead",
      ground: "#8f7a66",
      ink: "#2b231d",
    },
  },
  {
    id: "photo-neighborhood-walk",
    title: "Neighborhood Walk",
    summary: "Tree shadows and a walking silhouette keep the frame light while still moving the story forward.",
    location: "Los Angeles · Los Feliz",
    takenAt: "2026-02-19",
    slot: "walk",
    concepts: ["walk", "daily", "city", "quiet", "light", "losangeles"],
    surfaceTint: "#d9d2c7",
    palette: {
      sky: "#dce4dc",
      glow: "#eff4ee",
      horizon: "#c5d1c3",
      ground: "#7b8b79",
      ink: "#253126",
    },
  },
  {
    id: "photo-evening-tram",
    title: "Evening Rail",
    summary: "A little blue-hour calm and a city line help the set breathe.",
    location: "Los Angeles · Downtown",
    takenAt: "2026-01-31",
    slot: "city",
    concepts: ["city", "evening", "quiet", "blue", "travel", "losangeles"],
    surfaceTint: "#c9d0d7",
    palette: {
      sky: "#cad6e6",
      glow: "#e8edf4",
      horizon: "#a8b5c8",
      ground: "#66758f",
      ink: "#1d2733",
    },
  },
  {
    id: "photo-film-portrait",
    title: "Soft Portrait",
    summary: "A close portrait with a hint of film texture, the kind of frame that tends to stay in memory.",
    location: "Los Angeles · Koreatown",
    takenAt: "2026-02-11",
    slot: "portrait",
    concepts: ["portrait", "soft", "memory", "daily", "person", "losangeles"],
    surfaceTint: "#d9c8c3",
    palette: {
      sky: "#e5d1cf",
      glow: "#f2e7e6",
      horizon: "#d6bbb9",
      ground: "#926d6c",
      ink: "#331f22",
    },
  },
  {
    id: "photo-stair-light",
    title: "Light on Stairs",
    summary: "Simple geometry and falling light make the whole set feel cleaner.",
    location: "Los Angeles · Pasadena",
    takenAt: "2026-02-08",
    slot: "light",
    concepts: ["light", "detail", "quiet", "architecture", "daily", "losangeles"],
    surfaceTint: "#d7d7ce",
    palette: {
      sky: "#ede8d9",
      glow: "#faf7ee",
      horizon: "#d3ccbc",
      ground: "#8c8678",
      ink: "#2d2a22",
    },
  },
  {
    id: "photo-house-plants",
    title: "Morning Plants",
    summary: "Plants and morning light keep the result grounded in everyday life without feeling repetitive.",
    location: "Los Angeles · Highland Park",
    takenAt: "2026-02-05",
    slot: "quiet",
    concepts: ["home", "soft", "quiet", "daily", "light", "losangeles"],
    surfaceTint: "#c7d4d0",
    palette: {
      sky: "#dce8e1",
      glow: "#eef5f0",
      horizon: "#c4d5cc",
      ground: "#6e887f",
      ink: "#203028",
    },
  },
  {
    id: "photo-diner-candid",
    title: "Late Lunch",
    summary: "A candid human moment keeps the set from becoming all scenery.",
    location: "Los Angeles · Glendale",
    takenAt: "2026-02-03",
    slot: "candid",
    concepts: ["daily", "friends", "person", "warm", "city", "losangeles"],
    surfaceTint: "#d9d2c7",
    palette: {
      sky: "#f0d8c5",
      glow: "#f7eadc",
      horizon: "#d0b89d",
      ground: "#936c4b",
      ink: "#342417",
    },
  },
  {
    id: "photo-coast-drive",
    title: "Coastline Drive",
    summary: "If the prompt leans relaxed or reflective, this frame helps open up the story.",
    location: "Malibu · Pacific Coast Highway",
    takenAt: "2026-01-24",
    slot: "cover",
    concepts: ["coast", "travel", "memory", "soft", "blue", "sunset"],
    surfaceTint: "#c6d5ca",
    palette: {
      sky: "#d7e2e7",
      glow: "#edf4f7",
      horizon: "#b7d0d6",
      ground: "#66848e",
      ink: "#1e2d33",
    },
  },
  {
    id: "photo-sea-breeze",
    title: "Sea Breeze",
    summary: "A quieter coastal detail that works well later in the set to slow the mood down.",
    location: "Santa Monica · Ocean Front",
    takenAt: "2026-01-22",
    slot: "detail",
    concepts: ["coast", "quiet", "detail", "light", "memory", "travel"],
    surfaceTint: "#d6dfdf",
    palette: {
      sky: "#d9e7e6",
      glow: "#eef6f5",
      horizon: "#bfd8d6",
      ground: "#779391",
      ink: "#203436",
    },
  },
  {
    id: "photo-golden-hour-road",
    title: "Golden Hour Road",
    summary: "It suggests distance without feeling overloaded, which makes it useful as a transition frame.",
    location: "Orange County · Laguna",
    takenAt: "2026-01-18",
    slot: "walk",
    concepts: ["travel", "sunset", "warm", "light", "road", "memory"],
    surfaceTint: "#d8c7b6",
    palette: {
      sky: "#ead5be",
      glow: "#f8eddc",
      horizon: "#d9b999",
      ground: "#9a704d",
      ink: "#362417",
    },
  },
  {
    id: "photo-bookstore",
    title: "Bookstore Quiet",
    summary: "A still-life leaning frame with spatial calm helps the result feel more intentionally edited.",
    location: "Los Angeles · Arts District",
    takenAt: "2026-02-12",
    slot: "quiet",
    concepts: ["quiet", "daily", "detail", "warm", "city", "losangeles"],
    surfaceTint: "#cfbfae",
    palette: {
      sky: "#e1cfbf",
      glow: "#f4ebdf",
      horizon: "#c8b39f",
      ground: "#866e59",
      ink: "#2d231d",
    },
  },
  {
    id: "photo-rooftop-night",
    title: "Roofline Blue",
    summary: "The night stays restrained, leaving only the city outline and a little wind.",
    location: "Los Angeles · Hollywood",
    takenAt: "2026-02-02",
    slot: "city",
    concepts: ["city", "quiet", "blue", "night", "memory", "losangeles"],
    surfaceTint: "#c3cbd7",
    palette: {
      sky: "#c3cfdf",
      glow: "#dde6f0",
      horizon: "#a5b0c9",
      ground: "#59647f",
      ink: "#1b2235",
    },
  },
  {
    id: "photo-friends-picnic",
    title: "Picnic Hour",
    summary: "There are people here, but not too much noise, which suits a prompt asking for warmth without chaos.",
    location: "Los Angeles · Griffith Park",
    takenAt: "2026-02-21",
    slot: "candid",
    concepts: ["friends", "warm", "daily", "soft", "nature", "losangeles"],
    surfaceTint: "#d2d5c2",
    palette: {
      sky: "#dce4cd",
      glow: "#edf3df",
      horizon: "#c4cfaa",
      ground: "#75815e",
      ink: "#28301f",
    },
  },
  {
    id: "photo-sunshade",
    title: "Sunshade Detail",
    summary: "The contrast stays gentle, which helps the whole edit feel a little more refined.",
    location: "Los Angeles · Venice",
    takenAt: "2026-01-28",
    slot: "detail",
    concepts: ["detail", "light", "soft", "architecture", "coast", "losangeles"],
    surfaceTint: "#ddd0c1",
    palette: {
      sky: "#ead9c7",
      glow: "#f7eee2",
      horizon: "#d1bea8",
      ground: "#8e7d6a",
      ink: "#30261f",
    },
  },
  {
    id: "photo-courtyard",
    title: "Courtyard Noon",
    summary: "Space, tree shade, and neutral tones make it a strong middle frame that bridges what comes before and after.",
    location: "Los Angeles · Pasadena",
    takenAt: "2026-02-07",
    slot: "light",
    concepts: ["architecture", "light", "quiet", "daily", "city", "losangeles"],
    surfaceTint: "#d6d0c4",
    palette: {
      sky: "#e8dfd0",
      glow: "#f5efe5",
      horizon: "#d8ccbb",
      ground: "#8d7f6d",
      ink: "#302921",
    },
  },
];

export const PHOTO_LIBRARY: PhotoAsset[] = PHOTO_SEEDS.map((photo) => ({
  id: photo.id,
  title: photo.title,
  summary: photo.summary,
  location: photo.location,
  takenAt: photo.takenAt,
  slot: photo.slot,
  concepts: photo.concepts,
  surfaceTint: photo.surfaceTint,
  imageUrl: buildArtworkDataUrl(photo.title, photo.slot, photo.palette),
}));

export const PROMPT_PRESETS: PromptPreset[] = [
  { label: "Post-ready", query: "suitable for posting" },
  { label: "Softer", query: "make the tone softer" },
  { label: "Everyday", query: "with a grounded everyday mood" },
  { label: "One person", query: "centered on one person" },
  { label: "Recent", query: "from the last six months" },
  { label: "Calmer", query: "not too busy" },
];

export const INITIAL_PROMPT =
  "Pick 9 photos from my recent library that feel gentle, grounded, and suitable for posting, then give me a caption.";
