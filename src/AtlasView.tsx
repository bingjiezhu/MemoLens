import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { MouseEvent } from "react";

import {
  buildPreviewImageUrl,
  fetchAtlasWorkbench,
  rebuildAtlas,
} from "./query/api";
import type {
  AtlasAsset,
  AtlasLens,
  AtlasInspirationCard,
  AtlasMemory,
  AtlasStoryline,
  AtlasWorkbench,
} from "./query/types";

const GALAXY_WIDTH = 1000;
const GALAXY_HEIGHT = 620;
const GALAXY_CENTER_X = GALAXY_WIDTH / 2;
const GALAXY_CENTER_Y = GALAXY_HEIGHT / 2;
const GALAXY_REPULSION = 8000;
const GALAXY_LINK_DISTANCE = 220;
const GALAXY_LINK_STRENGTH = 0.001;
const GALAXY_CENTER_GRAVITY = 0.003;
const GALAXY_COLLISION_PADDING = 26;
const GALAXY_LAYOUT_LINKS = 45;
const GALAXY_RENDER_LINKS = 72;

interface AtlasViewProps {
  apiBase: string;
  imageLibraryDir?: string | null;
  dbPath?: string | null;
  canUseBackend: boolean;
  onInspirationChange?: (
    cards: AtlasInspirationCard[],
    storylines: AtlasStoryline[],
    suggestedQueries: string[],
  ) => void;
  basketAssetIds?: string[];
  onBasketToggle?: (asset: AtlasAsset) => void;
  onBasketAddMany?: (assets: AtlasAsset[]) => void;
}

type GalaxyNodeKind = "concept" | "memory";
type GalaxyLinkKind = "cooccurrence" | "memory-concept";

interface GalaxyNode {
  id: string;
  kind: GalaxyNodeKind;
  label: string;
  x: number;
  y: number;
  radius: number;
  count: number;
  color: string;
  concepts: string[];
  memory?: AtlasMemory;
}

interface GalaxyLink {
  id: string;
  kind: GalaxyLinkKind;
  source: string;
  target: string;
  sourceNode: GalaxyNode;
  targetNode: GalaxyNode;
  weight: number;
}

interface GalaxyData {
  nodes: GalaxyNode[];
  links: GalaxyLink[];
  conceptNodes: GalaxyNode[];
  memoryNodes: GalaxyNode[];
}

function formatScore(value: number): string {
  return `${Math.round(value * 100)}%`;
}

function formatCount(value: number): string {
  return value > 999 ? `${Math.round(value / 100) / 10}k` : String(value);
}

function colorForKey(value: string, alpha = 220): [number, number, number, number] {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash * 31 + value.charCodeAt(index)) | 0;
  }
  const colors: Array<[number, number, number]> = [
    [52, 83, 92],
    [126, 88, 67],
    [80, 103, 83],
    [109, 89, 132],
    [161, 119, 61],
    [63, 107, 131],
  ];
  const [r, g, b] = colors[Math.abs(hash) % colors.length];
  return [r, g, b, alpha];
}

function rgbaForKey(value: string, alpha = 0.82): string {
  const [r, g, b] = colorForKey(value, Math.round(alpha * 255));
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function hashUnit(value: string): number {
  let hash = 0;
  for (let index = 0; index < value.length; index += 1) {
    hash = (hash * 33 + value.charCodeAt(index)) | 0;
  }
  return (Math.abs(hash) % 1000) / 1000;
}

const HAN_TEXT_PATTERN = /[\u3400-\u9fff]/u;

function hasHanText(value: string): boolean {
  return HAN_TEXT_PATTERN.test(value);
}

function normalizeConcept(value: string): string {
  const normalized = value.trim().toLowerCase();
  if (!normalized) {
    return "";
  }
  return hasHanText(normalized) ? "" : normalized;
}

function sanitizeDisplayText(value: string, fallback: string): string {
  let cleaned = value.replace(/\s+/g, " ").trim();
  return cleaned && !hasHanText(cleaned) ? cleaned : fallback;
}

function compactGalaxyLabel(value: string, maxLength = 18): string {
  const normalized = sanitizeDisplayText(value, "Memory");
  return normalized.length > maxLength ? `${normalized.slice(0, maxLength - 3)}...` : normalized;
}

function previewUrl(apiBase: string, asset: AtlasAsset, imageLibraryDir?: string | null, width = 420): string {
  return buildPreviewImageUrl(apiBase, asset.relative_path, imageLibraryDir, width);
}

function assetMatchesConcept(asset: AtlasAsset, concept: string): boolean {
  const normalizedConcept = normalizeConcept(concept);
  if (!normalizedConcept) {
    return false;
  }
  const searchable = [
    asset.filename,
    asset.title,
    asset.description,
    asset.combined_text,
    asset.cluster_label,
    asset.mode_cluster_label,
    ...asset.tags,
  ].join(" ").toLowerCase();
  return searchable.includes(normalizedConcept);
}

function photoStrip(assets: AtlasAsset[], apiBase: string, imageLibraryDir?: string | null) {
  return (
    <div className="memory-photo-strip">
      {assets.slice(0, 5).map((asset) => (
        <img key={asset.id} src={previewUrl(apiBase, asset, imageLibraryDir, 260)} alt={sanitizeDisplayText(asset.title, "Photo")} />
      ))}
    </div>
  );
}

function buildKeywordGalaxy(workbench: AtlasWorkbench | null): GalaxyData {
  if (!workbench) {
    return {
      nodes: [],
      links: [],
      conceptNodes: [],
      memoryNodes: [],
    };
  }

  const conceptCounts = new Map<string, number>();
  const conceptPairs = new Map<string, number>();
  const memories = workbench.memories.slice(0, 18);

  for (const term of workbench.library_summary.top_concepts.slice(0, 14)) {
    const concept = normalizeConcept(term);
    if (concept) {
      conceptCounts.set(concept, Math.max(conceptCounts.get(concept) ?? 0, 4));
    }
  }

  for (const memory of memories) {
    const concepts = Array.from(
      new Set(memory.top_concepts.map(normalizeConcept).filter(Boolean).slice(0, 6)),
    );
    for (const concept of concepts) {
      conceptCounts.set(concept, (conceptCounts.get(concept) ?? 0) + memory.asset_count);
    }
    for (let firstIndex = 0; firstIndex < concepts.length; firstIndex += 1) {
      for (let secondIndex = firstIndex + 1; secondIndex < concepts.length; secondIndex += 1) {
        const pair = [concepts[firstIndex], concepts[secondIndex]].sort().join("::");
        conceptPairs.set(pair, (conceptPairs.get(pair) ?? 0) + 1);
      }
    }
  }

  const conceptEntries = [...conceptCounts.entries()]
    .sort((left, right) => right[1] - left[1])
    .slice(0, 18);
  const conceptNodes: GalaxyNode[] = conceptEntries.map(([concept, count], index) => {
    const angle = (Math.PI * 2 * index) / Math.max(1, conceptEntries.length) - Math.PI / 2;
    const jitter = (hashUnit(concept) - 0.5) * 42;
    const ringX = 325 + (index % 3) * 24;
    const ringY = 210 + ((index + 1) % 3) * 26;
    return {
      id: `concept:${concept}`,
      kind: "concept",
      label: concept,
      x: GALAXY_CENTER_X + Math.cos(angle) * ringX + jitter,
      y: GALAXY_CENTER_Y + Math.sin(angle) * ringY + jitter * 0.45,
      radius: clamp(28 + Math.sqrt(count) * 2.4, 34, 74),
      count,
      color: rgbaForKey(concept, 0.8),
      concepts: [concept],
    };
  });
  const conceptNodeByLabel = new Map(conceptNodes.map((node) => [node.label, node]));

  const memoryNodes: GalaxyNode[] = memories.slice(0, 14).map((memory, index) => {
    const concepts = memory.top_concepts.map(normalizeConcept).filter(Boolean);
    const primaryConcept = concepts.find((concept) => conceptNodeByLabel.has(concept));
    const anchor = primaryConcept ? conceptNodeByLabel.get(primaryConcept) : null;
    const orbitAngle = Math.PI * 2 * hashUnit(`${memory.id}:${index}`);
    const orbitDistance = 70 + (index % 4) * 22;
    const fallbackAngle = (Math.PI * 2 * index) / Math.max(1, memories.length);
    const rawX = anchor
      ? anchor.x + Math.cos(orbitAngle) * orbitDistance
      : GALAXY_CENTER_X + Math.cos(fallbackAngle) * 185;
    const rawY = anchor
      ? anchor.y + Math.sin(orbitAngle) * orbitDistance
      : GALAXY_CENTER_Y + Math.sin(fallbackAngle) * 145;
    return {
      id: `memory:${memory.id}`,
      kind: "memory",
      label: sanitizeDisplayText(memory.label, "Memory"),
      x: clamp(rawX, 105, GALAXY_WIDTH - 105),
      y: clamp(rawY, 92, GALAXY_HEIGHT - 92),
      radius: clamp(36 + Math.sqrt(memory.asset_count) * 2.8, 44, 78),
      count: memory.asset_count,
      color: rgbaForKey(memory.id, 0.88),
      concepts,
      memory,
    };
  });

  const nodes = [...conceptNodes, ...memoryNodes];
  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const links: GalaxyLink[] = [];

  for (const [pair, count] of conceptPairs.entries()) {
    const [left, right] = pair.split("::");
    const source = conceptNodeByLabel.get(left);
    const target = conceptNodeByLabel.get(right);
    if (!source || !target) {
      continue;
    }
    links.push({
      id: `concept-link:${pair}`,
      kind: "cooccurrence",
      source: source.id,
      target: target.id,
      sourceNode: source,
      targetNode: target,
      weight: clamp(count / 5, 0.18, 1),
    });
  }

  for (const memoryNode of memoryNodes) {
    for (const concept of memoryNode.concepts.slice(0, 4)) {
      const conceptNode = conceptNodeByLabel.get(concept);
      if (!conceptNode) {
        continue;
      }
      const source = nodeById.get(memoryNode.id);
      if (!source) {
        continue;
      }
      links.push({
        id: `memory-link:${memoryNode.id}:${concept}`,
        kind: "memory-concept",
        source: memoryNode.id,
        target: conceptNode.id,
        sourceNode: source,
        targetNode: conceptNode,
        weight: clamp(memoryNode.count / 90, 0.22, 1),
      });
    }
  }

  const rankedLinks = [...links]
    .sort((left, right) => {
      const weightDelta = right.weight - left.weight;
      if (Math.abs(weightDelta) > 0.001) {
        return weightDelta;
      }
      const leftPriority = left.kind === "cooccurrence" ? 1 : 0;
      const rightPriority = right.kind === "cooccurrence" ? 1 : 0;
      return rightPriority - leftPriority;
    });
  const layoutLinks = rankedLinks.slice(0, GALAXY_LAYOUT_LINKS);
  const renderLinks = rankedLinks.slice(0, GALAXY_RENDER_LINKS);

  // Force simulation: repulsion + collision + light link attraction + center gravity.
  const iterations = 80;
  for (let iter = 0; iter < iterations; iter++) {
    const alpha = 1 - iter / iterations;
    // Repulsion between all node pairs
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const rawDx = nodes[j].x - nodes[i].x;
        const rawDy = nodes[j].y - nodes[i].y;
        const rawDist = Math.sqrt(rawDx * rawDx + rawDy * rawDy);
        const fallbackAngle = (i * 12.9898 + j * 78.233) % (Math.PI * 2);
        const dx = rawDist < 0.001 ? Math.cos(fallbackAngle) : rawDx;
        const dy = rawDist < 0.001 ? Math.sin(fallbackAngle) : rawDy;
        const dist = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
        const repulsion = (alpha * GALAXY_REPULSION) / (dist * dist);
        const fx = (dx / dist) * repulsion;
        const fy = (dy / dist) * repulsion;
        nodes[i].x -= fx;
        nodes[i].y -= fy;
        nodes[j].x += fx;
        nodes[j].y += fy;

        const minDistance = nodes[i].radius + nodes[j].radius + GALAXY_COLLISION_PADDING;
        if (dist < minDistance) {
          const push = (minDistance - dist) * 0.5 * alpha;
          const pushX = (dx / dist) * push;
          const pushY = (dy / dist) * push;
          nodes[i].x -= pushX;
          nodes[i].y -= pushY;
          nodes[j].x += pushX;
          nodes[j].y += pushY;
        }
      }
    }
    // Attraction along links
    for (const link of layoutLinks) {
      const s = nodeById.get(link.source);
      const t = nodeById.get(link.target);
      if (!s || !t) {
        continue;
      }
      const dx = t.x - s.x;
      const dy = t.y - s.y;
      const dist = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
      const attraction = alpha * link.weight * (dist - GALAXY_LINK_DISTANCE) * GALAXY_LINK_STRENGTH;
      const fx = (dx / dist) * attraction;
      const fy = (dy / dist) * attraction;
      s.x += fx;
      s.y += fy;
      t.x -= fx;
      t.y -= fy;
    }
    // Center gravity
    for (const node of nodes) {
      node.x += (GALAXY_CENTER_X - node.x) * alpha * GALAXY_CENTER_GRAVITY;
      node.y += (GALAXY_CENTER_Y - node.y) * alpha * GALAXY_CENTER_GRAVITY;
    }
    // Boundary clamping
    for (const node of nodes) {
      node.x = clamp(node.x, 80, GALAXY_WIDTH - 80);
      node.y = clamp(node.y, 70, GALAXY_HEIGHT - 70);
    }
  }

  // Update link source/target node references after simulation
  for (const link of renderLinks) {
    const s = nodeById.get(link.source);
    const t = nodeById.get(link.target);
    if (s) {
      link.sourceNode = s;
    }
    if (t) {
      link.targetNode = t;
    }
  }

  return {
    nodes,
    links: renderLinks,
    conceptNodes,
    memoryNodes,
  };
}

function AtlasView({
  apiBase,
  imageLibraryDir,
  dbPath,
  canUseBackend,
  onInspirationChange,
  basketAssetIds = [],
  onBasketToggle,
  onBasketAddMany,
}: AtlasViewProps) {
  const [lens, setLens] = useState<AtlasLens>("explore");
  const [workbench, setWorkbench] = useState<AtlasWorkbench | null>(null);
  const [selectedMemoryId, setSelectedMemoryId] = useState<string | null>(null);
  const [selectedAssetId, setSelectedAssetId] = useState<string | null>(null);
  const [selectedGalaxyNodeId, setSelectedGalaxyNodeId] = useState<string | null>(null);
  const [hoveredGalaxyNodeId, setHoveredGalaxyNodeId] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isRebuilding, setIsRebuilding] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const filmstripRef = useRef<HTMLDivElement | null>(null);
  const lastFilmstripIndexRef = useRef<number | null>(null);

  const loadWorkbench = useCallback(async () => {
    if (!canUseBackend) {
      setMessage("Local backend is offline.");
      return;
    }
    setIsLoading(true);
    setMessage(null);
    try {
      const nextWorkbench = await fetchAtlasWorkbench({
        apiBase,
        dbPath,
        lens,
        showDuplicates: true,
        limit: 1200,
      });
      setWorkbench(nextWorkbench);
      setSelectedMemoryId((current) =>
        current && nextWorkbench.memories.some((memory) => memory.id === current)
          ? current
          : nextWorkbench.featured_memory?.id ?? nextWorkbench.memories[0]?.id ?? null,
      );
      setSelectedAssetId((current) =>
        current && nextWorkbench.overview.assets.some((asset) => asset.id === current)
          ? current
          : nextWorkbench.featured_memory?.representative_asset_ids[0] ?? nextWorkbench.overview.assets[0]?.id ?? null,
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Memory Workbench failed to load.");
    } finally {
      setIsLoading(false);
    }
  }, [apiBase, canUseBackend, dbPath, lens]);

  useEffect(() => {
    void loadWorkbench();
  }, [loadWorkbench]);

  useEffect(() => {
    if (!workbench) {
      return;
    }
    onInspirationChange?.(
      workbench.inspiration_cards,
      workbench.storylines,
      workbench.suggested_queries,
    );
  }, [onInspirationChange, workbench]);

  const selectedMemory =
    workbench?.memories.find((memory) => memory.id === selectedMemoryId) ??
    workbench?.featured_memory ??
    null;
  const librarySummary = workbench?.library_summary ?? null;
  const indexHealth = workbench?.index_health ?? workbench?.overview.index_health ?? null;
  const placeLensCount = workbench?.lenses.find((item) => item.id === "map")?.count ?? 0;
  const assetsById = useMemo(() => {
    const next = new Map<string, AtlasAsset>();
    for (const asset of workbench?.overview.assets ?? []) {
      next.set(asset.id, asset);
    }
    for (const memory of workbench?.memories ?? []) {
      for (const asset of [...memory.representative_assets, ...memory.best_assets]) {
        next.set(asset.id, asset);
      }
    }
    return next;
  }, [workbench]);
  const basketIdSet = useMemo(() => new Set(basketAssetIds), [basketAssetIds]);
  const galaxy = useMemo(() => buildKeywordGalaxy(workbench), [workbench]);
  const activeGalaxyNodeId = hoveredGalaxyNodeId ?? selectedGalaxyNodeId ?? (selectedMemoryId ? `memory:${selectedMemoryId}` : null);
  const selectedGalaxyNode =
    galaxy.nodes.find((node) => node.id === (selectedGalaxyNodeId ?? activeGalaxyNodeId)) ?? null;
  const selectedConceptNode = selectedGalaxyNode?.kind === "concept" ? selectedGalaxyNode : null;
  const conceptMemories = selectedConceptNode
    ? galaxy.memoryNodes
        .filter((node) => node.concepts.includes(selectedConceptNode.label))
        .map((node) => node.memory)
        .filter((memory): memory is AtlasMemory => Boolean(memory))
    : [];
  const focusedConceptAssets = selectedConceptNode
    ? (workbench?.overview.assets ?? [])
        .filter((asset) => assetMatchesConcept(asset, selectedConceptNode.label))
        .sort((left, right) => right.quality_score - left.quality_score)
    : [];
  const selectedMemoryAssets = selectedMemory
    ? selectedMemory.asset_ids
        .map((assetId) => assetsById.get(assetId))
        .filter((asset): asset is AtlasAsset => Boolean(asset))
    : [];
  const focusAssets = selectedConceptNode
    ? focusedConceptAssets
    : selectedMemoryAssets.length > 0
      ? selectedMemoryAssets
      : selectedMemory?.representative_assets ?? [];

  async function handleRebuild(): Promise<void> {
    setIsRebuilding(true);
    setMessage(null);
    try {
      await rebuildAtlas({ apiBase, dbPath });
      await loadWorkbench();
      setMessage("Memory Workbench rebuilt locally.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Rebuild failed.");
    } finally {
      setIsRebuilding(false);
    }
  }

  function selectMemory(memory: AtlasMemory): void {
    setSelectedMemoryId(memory.id);
    setSelectedAssetId(memory.representative_asset_ids[0] ?? memory.best_assets[0]?.id ?? null);
    setSelectedGalaxyNodeId(`memory:${memory.id}`);
  }

  function selectGalaxyNode(node: GalaxyNode): void {
    setSelectedGalaxyNodeId(node.id);
    if (node.kind === "memory" && node.memory) {
      selectMemory(node.memory);
    }
  }

  function nodeIsActive(nodeId: string): boolean {
    if (!activeGalaxyNodeId) {
      return true;
    }
    if (nodeId === activeGalaxyNodeId) {
      return true;
    }
    return galaxy.links.some(
      (link) =>
        (link.source === activeGalaxyNodeId && link.target === nodeId) ||
        (link.target === activeGalaxyNodeId && link.source === nodeId),
    );
  }

  function linkIsActive(link: GalaxyLink): boolean {
    if (!activeGalaxyNodeId) {
      return true;
    }
    return link.source === activeGalaxyNodeId || link.target === activeGalaxyNodeId;
  }

  function scrollFilmstrip(direction: -1 | 1): void {
    filmstripRef.current?.scrollBy({
      left: direction * 440,
      behavior: "smooth",
    });
  }

  function handleFilmstripAssetClick(
    asset: AtlasAsset,
    index: number,
    event: MouseEvent<HTMLButtonElement>,
  ): void {
    setSelectedAssetId(asset.id);
    if (event.shiftKey && lastFilmstripIndexRef.current !== null) {
      const start = Math.min(lastFilmstripIndexRef.current, index);
      const end = Math.max(lastFilmstripIndexRef.current, index);
      onBasketAddMany?.(focusAssets.slice(start, end + 1));
    } else {
      onBasketToggle?.(asset);
    }
    lastFilmstripIndexRef.current = index;
  }

  return (
    <section className="section-block workbench-section" id="atlas">
      <div className="workbench-overview-head">
        <div>
          <p className="eyebrow">Memory Workbench</p>
          <h2>Library map</h2>
          {librarySummary ? (
            <p>{librarySummary.summary}</p>
          ) : (
            <p>Index a library, rebuild Atlas, then MemoLens will organize themes, stories, places, and cleanup cues.</p>
          )}
        </div>

        <div className="workbench-overview-actions">
          <span className="meta-pill">
            {isLoading
              ? "Loading map"
              : indexHealth?.needs_rebuild
                ? "Atlas cache needs rebuild"
                : `${workbench?.overview.visible_count ?? 0} mapped photos`}
          </span>
          <button className="secondary-button" type="button" onClick={handleRebuild} disabled={!canUseBackend || isRebuilding}>
            {isRebuilding ? "Rebuilding" : "Rebuild Atlas"}
          </button>
        </div>
      </div>

      <div className="workbench-grid">
        <aside className="memory-rail">
          {librarySummary ? (
            <div className="rail-section library-snapshot">
              <strong>Library Map</strong>
              <div className="snapshot-grid">
                <span>{formatCount(librarySummary.asset_count)} photos</span>
                <span>{formatCount(librarySummary.memory_count)} memories</span>
                <span>{formatScore(librarySummary.quality_avg)} quality</span>
                <span>{formatCount(librarySummary.people_risk_count)} people risk</span>
              </div>
              <div className="highlight-row">
                {librarySummary.top_concepts.slice(0, 5).map((term) => (
                  <span key={term} className="highlight-chip">
                    {term}
                  </span>
                ))}
              </div>
            </div>
          ) : null}

          <div className="rail-section">
            <strong>Lenses</strong>
            <div className="lens-list">
              {(workbench?.lenses ?? []).map((item) => (
                <button
                  key={item.id}
                  type="button"
                  className={lens === item.id ? "active" : ""}
                  onClick={() => setLens(item.id)}
                >
                  <span>{item.label}</span>
                  <em>{formatCount(item.count)}</em>
                  <small>{item.summary}</small>
                </button>
              ))}
            </div>
          </div>

          <div className="rail-section">
            <strong>Memories</strong>
            <div className="memory-list">
              {(workbench?.memories ?? []).slice(0, 14).map((memory) => (
                <button
                  key={memory.id}
                  type="button"
                  className={selectedMemoryId === memory.id ? "active" : ""}
                  onClick={() => selectMemory(memory)}
                >
                  {photoStrip(memory.representative_assets, apiBase, imageLibraryDir)}
                  <span>{memory.label}</span>
                  <small>{memory.asset_count} photos · {memory.time_label ?? "any time"}</small>
                </button>
              ))}
            </div>
          </div>

          <div className="rail-section">
            <strong>Storylines</strong>
            <div className="inspiration-list">
              {(workbench?.storylines ?? []).slice(0, 5).map((storyline) => (
                <button
                  key={storyline.id}
                  type="button"
                  onClick={() => {
                    const memory = workbench?.memories.find((item) => storyline.memory_ids.includes(item.id));
                    if (memory) {
                      selectMemory(memory);
                    }
                  }}
                >
                  <span>{storyline.title}</span>
                  <small>{storyline.summary}</small>
                </button>
              ))}
            </div>
          </div>
        </aside>

        <main className="memory-canvas">
          <div className="keyword-galaxy">
            <svg
              className="keyword-galaxy-svg"
              viewBox={`0 0 ${GALAXY_WIDTH} ${GALAXY_HEIGHT}`}
              role="img"
              aria-label="Keyword galaxy showing concepts, memories, and semantic links in the local photo library"
            >
              <defs>
                <filter id="galaxyGlow" x="-60%" y="-60%" width="220%" height="220%">
                  <feGaussianBlur stdDeviation="7" result="blur" />
                  <feMerge>
                    <feMergeNode in="blur" />
                    <feMergeNode in="SourceGraphic" />
                  </feMerge>
                </filter>
              </defs>

              <g className="galaxy-links">
                {galaxy.links.map((link) => {
                  const active = linkIsActive(link);
                  return (
                    <line
                      key={link.id}
                      className={`galaxy-link galaxy-link-${link.kind}${active ? " active" : " muted"}`}
                      x1={link.sourceNode.x}
                      y1={link.sourceNode.y}
                      x2={link.targetNode.x}
                      y2={link.targetNode.y}
                      strokeWidth={link.kind === "cooccurrence" ? 1.2 + link.weight * 5 : 0.9 + link.weight * 3}
                    />
                  );
                })}
              </g>

              <g className="galaxy-concepts">
                {galaxy.conceptNodes.map((node) => {
                  const active = nodeIsActive(node.id);
                  return (
                    <g
                      key={node.id}
                      className={`galaxy-node concept-node${active ? " active" : " muted"}`}
                      role="button"
                      tabIndex={0}
                      transform={`translate(${node.x} ${node.y})`}
                      onClick={() => selectGalaxyNode(node)}
                      onMouseEnter={() => setHoveredGalaxyNodeId(node.id)}
                      onMouseLeave={() => setHoveredGalaxyNodeId(null)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          selectGalaxyNode(node);
                        }
                      }}
                    >
                      <title>{`${node.label} · ${formatCount(node.count)} photos`}</title>
                      <circle r={node.radius + 14} className="concept-glow" style={{ fill: node.color }} />
                      <circle r={node.radius} className="concept-core" style={{ fill: node.color }} />
                      <text className="concept-label" textAnchor="middle" dominantBaseline="middle">
                        {compactGalaxyLabel(node.label, 12)}
                      </text>
                      <text className="concept-count" textAnchor="middle" y={node.radius + 20}>
                        {formatCount(node.count)}
                      </text>
                    </g>
                  );
                })}
              </g>

              <g className="galaxy-memories">
                {galaxy.memoryNodes.map((node) => {
                  const active = nodeIsActive(node.id);
                  return (
                    <g
                      key={node.id}
                      className={`galaxy-node memory-node${active ? " active" : " muted"}${selectedMemoryId === node.memory?.id ? " selected" : ""}`}
                      role="button"
                      tabIndex={0}
                      transform={`translate(${node.x} ${node.y})`}
                      onClick={() => selectGalaxyNode(node)}
                      onMouseEnter={() => setHoveredGalaxyNodeId(node.id)}
                      onMouseLeave={() => setHoveredGalaxyNodeId(null)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          selectGalaxyNode(node);
                        }
                      }}
                    >
                      <title>{`${node.label} · ${formatCount(node.count)} photos · ${node.concepts.slice(0, 3).join(", ")}`}</title>
                      <circle r={node.radius + 10} className="memory-glow" style={{ fill: node.color }} />
                      <circle r={node.radius} className="memory-core" style={{ fill: node.color }} />
                      <circle r={node.radius} className="memory-ring" />
                      <text className="memory-label" textAnchor="middle" dominantBaseline="middle">
                        {compactGalaxyLabel(node.label)}
                      </text>
                      <text className="memory-count" textAnchor="middle" y={node.radius + 20}>
                        {formatCount(node.count)}
                      </text>
                    </g>
                  );
                })}
              </g>
            </svg>

            {selectedGalaxyNode ? (
              <div className="galaxy-focus-card">
                <p className="eyebrow">{selectedGalaxyNode.kind === "concept" ? "Keyword" : "Memory"}</p>
                <strong>{selectedGalaxyNode.label}</strong>
                <span>
                  {selectedGalaxyNode.kind === "concept"
                    ? `${conceptMemories.length} related memories · ${formatCount(selectedGalaxyNode.count)} weighted photos`
                    : `${formatCount(selectedGalaxyNode.count)} photos · ${selectedGalaxyNode.concepts.slice(0, 3).join(", ")}`}
                </span>
              </div>
            ) : null}

            <div className="atlas-map-status">
              <span>{galaxy.conceptNodes.length} keywords</span>
              <span>{galaxy.memoryNodes.length} memory nodes</span>
              <span>{galaxy.links.length} links</span>
            </div>
            {lens === "map" && placeLensCount === 0 ? (
              <div className="memory-map-empty">
                <strong>No GPS data yet</strong>
                <span>MemoLens can still organize by inferred themes and story chapters.</span>
              </div>
            ) : null}
          </div>

          {selectedConceptNode || selectedMemory ? (
            <div className="memory-insight-strip">
              <div>
                <p className="eyebrow">{selectedConceptNode ? "Keyword" : selectedMemory?.kind}</p>
                <h3>{selectedConceptNode?.label ?? selectedMemory?.label}</h3>
                <p>
                  {selectedConceptNode
                    ? `${focusAssets.length} matching photos · ${conceptMemories.length} connected memories`
                    : `${selectedMemory?.asset_count ?? focusAssets.length} photos · ${selectedMemory?.chapter_count ?? 0} chapters · quality ${formatScore(selectedMemory?.score ?? 0)}`}
                </p>
                <div className="highlight-row">
                  {(selectedConceptNode
                    ? conceptMemories.slice(0, 5).map((memory) => sanitizeDisplayText(memory.label, "Memory"))
                    : (selectedMemory?.top_concepts ?? []).map(normalizeConcept).filter(Boolean).slice(0, 5)
                  ).map((term) => (
                    <span key={term} className="highlight-chip">{term}</span>
                  ))}
                </div>
              </div>
              <div className="memory-filmstrip-panel">
                <div className="filmstrip-toolbar">
                  <span>
                    {focusAssets.length} photos · {basketAssetIds.filter((assetId) => focusAssets.some((asset) => asset.id === assetId)).length} selected here
                  </span>
                  <div className="filmstrip-actions">
                    <button type="button" className="icon-button" onClick={() => scrollFilmstrip(-1)} aria-label="Scroll filmstrip left">
                      ‹
                    </button>
                    <button type="button" className="icon-button" onClick={() => scrollFilmstrip(1)} aria-label="Scroll filmstrip right">
                      ›
                    </button>
                    <button
                      type="button"
                      className="secondary-button compact-button"
                      onClick={() => onBasketAddMany?.(focusAssets)}
                      disabled={focusAssets.length === 0}
                    >
                      Add all
                    </button>
                  </div>
                </div>
                <div className="memory-insight-filmstrip" ref={filmstripRef}>
                  {focusAssets.map((asset, index) => {
                    const isInBasket = basketIdSet.has(asset.id);
                    return (
                      <button
                        key={asset.id}
                        type="button"
                        className={`filmstrip-photo${selectedAssetId === asset.id ? " active" : ""}${isInBasket ? " selected" : ""}`}
                        onClick={(event) => handleFilmstripAssetClick(asset, index, event)}
                      >
                        <img
                          src={previewUrl(apiBase, asset, imageLibraryDir, 320)}
                          alt={sanitizeDisplayText(asset.title, "Photo")}
                          loading="lazy"
                          decoding="async"
                        />
                        <span className="filmstrip-check" aria-hidden="true">
                          {isInBasket ? "✓" : ""}
                        </span>
                        <span className="filmstrip-caption">
                          <strong>{sanitizeDisplayText(asset.title, "Photo")}</strong>
                          <small>{sanitizeDisplayText(asset.place_name ?? asset.country ?? asset.taken_at?.slice(0, 10) ?? "Local library", "Local library")}</small>
                        </span>
                      </button>
                    );
                  })}
                </div>
                {focusAssets.length === 0 ? (
                  <div className="empty-card">
                    <strong>No matching photos in this map view</strong>
                    <span>Rebuild Atlas or switch lenses to refresh the local evidence.</span>
                  </div>
                ) : null}
              </div>
            </div>
          ) : null}

        </main>
      </div>

      {message ? <p className="inline-note">{message}</p> : null}
    </section>
  );
}

export default AtlasView;
