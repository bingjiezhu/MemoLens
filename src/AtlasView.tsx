import { useCallback, useEffect, useMemo, useState } from "react";

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

function normalizeConcept(value: string): string {
  return value.trim().toLowerCase();
}

function nodePreviewUrl(apiBase: string, node: GalaxyNode, imageLibraryDir?: string | null): string | null {
  const asset = node.memory?.representative_assets[0] ?? node.memory?.best_assets[0] ?? null;
  return asset ? previewUrl(apiBase, asset, imageLibraryDir, 240) : null;
}

function previewUrl(apiBase: string, asset: AtlasAsset, imageLibraryDir?: string | null, width = 420): string {
  return buildPreviewImageUrl(apiBase, asset.relative_path, imageLibraryDir, width);
}

function photoStrip(assets: AtlasAsset[], apiBase: string, imageLibraryDir?: string | null) {
  return (
    <div className="memory-photo-strip">
      {assets.slice(0, 5).map((asset) => (
        <img key={asset.id} src={previewUrl(apiBase, asset, imageLibraryDir, 260)} alt={asset.title} />
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
      label: memory.label,
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

  // Force simulation: repulsion + link attraction + center gravity
  const iterations = 80;
  for (let iter = 0; iter < iterations; iter++) {
    const alpha = 1 - iter / iterations;
    // Repulsion between all node pairs
    for (let i = 0; i < nodes.length; i++) {
      for (let j = i + 1; j < nodes.length; j++) {
        const dx = nodes[j].x - nodes[i].x;
        const dy = nodes[j].y - nodes[i].y;
        const dist = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
        const repulsion = (alpha * 2800) / (dist * dist);
        const fx = (dx / dist) * repulsion;
        const fy = (dy / dist) * repulsion;
        nodes[i].x -= fx;
        nodes[i].y -= fy;
        nodes[j].x += fx;
        nodes[j].y += fy;
      }
    }
    // Attraction along links
    for (const link of links) {
      const s = nodeById.get(link.source);
      const t = nodeById.get(link.target);
      if (!s || !t) {
        continue;
      }
      const dx = t.x - s.x;
      const dy = t.y - s.y;
      const dist = Math.max(Math.sqrt(dx * dx + dy * dy), 1);
      const attraction = alpha * link.weight * (dist - 120) * 0.004;
      const fx = (dx / dist) * attraction;
      const fy = (dy / dist) * attraction;
      s.x += fx;
      s.y += fy;
      t.x -= fx;
      t.y -= fy;
    }
    // Center gravity
    for (const node of nodes) {
      node.x += (GALAXY_CENTER_X - node.x) * alpha * 0.008;
      node.y += (GALAXY_CENTER_Y - node.y) * alpha * 0.008;
    }
    // Boundary clamping
    for (const node of nodes) {
      node.x = clamp(node.x, 80, GALAXY_WIDTH - 80);
      node.y = clamp(node.y, 70, GALAXY_HEIGHT - 70);
    }
  }

  // Update link source/target node references after simulation
  for (const link of links) {
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
    links: links.slice(0, 80),
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
  const conceptAssets = conceptMemories
    .flatMap((memory) => memory.representative_assets)
    .filter((asset, index, list) => list.findIndex((item) => item.id === asset.id) === index)
    .slice(0, 6);

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
                        {node.label}
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
                  const imageUrl = nodePreviewUrl(apiBase, node, imageLibraryDir);
                  const imageSize = (node.radius - 5) * 2;
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
                      {imageUrl ? (
                        <foreignObject
                          x={-node.radius + 5}
                          y={-node.radius + 5}
                          width={imageSize}
                          height={imageSize}
                        >
                          <div className="memory-node-image">
                            <img src={imageUrl} alt={node.label} />
                          </div>
                        </foreignObject>
                      ) : (
                        <circle r={node.radius - 5} className="memory-fallback" />
                      )}
                      <circle r={node.radius - 5} className="memory-ring" />
                      <text className="memory-label" textAnchor="middle" y={node.radius + 20}>
                        {node.label}
                      </text>
                      <text className="memory-count" textAnchor="middle" y={node.radius + 38}>
                        {formatCount(node.count)} photos
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

          {selectedConceptNode ? (
            <div className="memory-insight-strip">
              <div>
                <p className="eyebrow">Keyword</p>
                <h3>{selectedConceptNode.label}</h3>
                <p>
                  Connected to {conceptMemories.length} memories and {formatCount(selectedConceptNode.count)} weighted photos.
                </p>
                <div className="highlight-row">
                  {conceptMemories.slice(0, 5).map((memory) => (
                    <span key={memory.id} className="highlight-chip">{memory.label}</span>
                  ))}
                </div>
              </div>
              <div className="memory-insight-photos">
                {conceptAssets.map((asset) => (
                  <button
                    key={asset.id}
                    type="button"
                    className={selectedAssetId === asset.id ? "active" : ""}
                    onClick={() => setSelectedAssetId(asset.id)}
                  >
                    <img src={previewUrl(apiBase, asset, imageLibraryDir, 360)} alt={asset.title} />
                  </button>
                ))}
              </div>
            </div>
          ) : selectedMemory ? (
            <div className="memory-insight-strip">
              <div>
                <p className="eyebrow">{selectedMemory.kind}</p>
                <h3>{selectedMemory.label}</h3>
                <p>
                  {selectedMemory.asset_count} photos · {selectedMemory.chapter_count} chapters · quality {formatScore(selectedMemory.score)}
                </p>
                <div className="highlight-row">
                  {selectedMemory.top_concepts.slice(0, 5).map((term) => (
                    <span key={term} className="highlight-chip">{term}</span>
                  ))}
                </div>
              </div>
              <div className="memory-insight-photos">
                {selectedMemory.representative_assets.slice(0, 6).map((asset) => (
                  <button
                    key={asset.id}
                    type="button"
                    className={selectedAssetId === asset.id ? "active" : ""}
                    onClick={() => setSelectedAssetId(asset.id)}
                  >
                    <img src={previewUrl(apiBase, asset, imageLibraryDir, 360)} alt={asset.title} />
                  </button>
                ))}
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
