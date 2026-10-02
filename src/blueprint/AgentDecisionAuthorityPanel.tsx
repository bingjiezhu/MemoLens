import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  BLUEPRINT_DECISION_UNITS,
  type BlueprintDecisionUnitName,
  type DesktopAgentAuthorityOverview,
  type DesktopAuthorityActionResult,
} from "./authorityTypes";
import {
  authorityProjectIds,
  canonicalDecisionUnitSelection,
  decisionUnitsForOperation,
} from "./authorityModel";

interface AgentDecisionAuthorityPanelProps {
  enabled: boolean;
}

function safeUiError(error: unknown): string {
  if (error instanceof Error && error.message.trim()) {
    return error.message.replace(/^Error invoking remote method '[^']+':\s*/i, "").slice(0, 500);
  }
  return "Agent authority controls are temporarily unavailable.";
}

function formatExpiry(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "unknown" : parsed.toLocaleString();
}

export function AgentDecisionAuthorityPanel({
  enabled,
}: AgentDecisionAuthorityPanelProps) {
  const [overview, setOverview] = useState<DesktopAgentAuthorityOverview | null>(null);
  const [projectId, setProjectId] = useState("");
  const [projectIdInput, setProjectIdInput] = useState("");
  const [selectedUnits, setSelectedUnits] = useState<Set<BlueprintDecisionUnitName>>(new Set());
  const [isBusy, setIsBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const refreshEpoch = useRef(0);
  const desktop = window.memolensDesktop;

  const refresh = useCallback(async (requestedProjectId?: string | null) => {
    if (!enabled || !desktop) return;
    const normalizedProjectId = requestedProjectId?.trim() || null;
    const epoch = refreshEpoch.current + 1;
    refreshEpoch.current = epoch;
    if (normalizedProjectId) {
      setOverview((current) => (
        current?.projectAuthority
        && current.projectAuthority.projectId !== normalizedProjectId
          ? { ...current, projectAuthority: null }
          : current
      ));
    }
    try {
      const next = await desktop.listAgentAuthority(normalizedProjectId);
      if (refreshEpoch.current !== epoch) return null;
      setOverview(next);
      setError(null);
      return next;
    } catch (cause) {
      if (refreshEpoch.current !== epoch) return null;
      setError(safeUiError(cause));
      return null;
    }
  }, [desktop, enabled]);

  useEffect(() => {
    if (!enabled || !desktop) return undefined;
    void refresh(projectId);
    const intervalId = window.setInterval(() => {
      if (!document.hidden) void refresh(projectId);
    }, 5_000);
    return () => window.clearInterval(intervalId);
  }, [desktop, enabled, projectId, refresh]);

  const knownProjectIds = useMemo(() => authorityProjectIds(overview), [overview]);
  useEffect(() => {
    if (!projectId && knownProjectIds.length > 0) {
      setProjectId(knownProjectIds[0]);
      setProjectIdInput(knownProjectIds[0]);
    }
  }, [knownProjectIds, projectId]);

  const authority = overview?.projectAuthority ?? null;

  async function finishAction(
    operation: () => Promise<DesktopAuthorityActionResult>,
    refreshProjectId = projectId,
  ): Promise<void> {
    setIsBusy(true);
    setMessage(null);
    setError(null);
    try {
      const result = await operation();
      if (result.status === "failed") {
        setError(result.message);
      } else {
        setMessage(result.message);
      }
      if (result.projectAuthority) {
        setOverview((current) => current ? {
          ...current,
          projectAuthority: result.projectAuthority,
        } : current);
      }
      await refresh(refreshProjectId);
    } catch (cause) {
      setError(safeUiError(cause));
    } finally {
      setIsBusy(false);
    }
  }

  function toggleUnit(name: BlueprintDecisionUnitName): void {
    setSelectedUnits((current) => {
      const next = new Set(current);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }

  useEffect(() => {
    setSelectedUnits(new Set());
  }, [authority?.projectId, authority?.revision]);

  function requestDecisionReview(
    operation: "confirm" | "revoke",
    wholeBlueprint = false,
  ): void {
    if (!desktop || !authority) return;
    const units = wholeBlueprint
      ? [...BLUEPRINT_DECISION_UNITS]
      : decisionUnitsForOperation(authority, selectedUnits, operation);
    if (units.length === 0) {
      setError(
        operation === "confirm"
          ? "Select at least one unverified decision."
          : "Select at least one confirmed decision.",
      );
      return;
    }
    void finishAction(() => desktop.requestDecisionAuthorityReview({
      projectId: authority.projectId,
      operation,
      decisionUnits: canonicalDecisionUnitSelection(units),
    }));
  }

  if (!enabled || !desktop) {
    return (
      <section className="section-block authority-panel" aria-labelledby="agent-authority-title">
        <div className="section-heading compact-heading">
          <p className="eyebrow">Agent &amp; decisions</p>
          <h2 id="agent-authority-title">Native review is available in the desktop app.</h2>
        </div>
        <p className="inline-note">
          Browser mode can read project context, but it cannot pair an Agent or confirm creative decisions.
        </p>
      </section>
    );
  }

  return (
    <section className="section-block authority-panel" aria-labelledby="agent-authority-title">
      <div className="authority-panel-heading">
        <div className="section-heading compact-heading">
          <p className="eyebrow">Agent &amp; creative decisions</p>
          <h2 id="agent-authority-title">Let Agents propose and edit. Keep your decisions yours.</h2>
        </div>
        <button
          type="button"
          className="secondary-button compact-button"
          disabled={isBusy}
          onClick={() => void refresh(projectId)}
        >
          Refresh
        </button>
      </div>

      <p className="authority-boundary-note">
        Pairing grants only the listed short-lived, reversible project writes for one existing project.
        A Timeline edit still requires an explicit Save in the plugin editor. Pairing never confirms
        your stance, script, direction, render, export, or publication.
      </p>

      <div className="authority-columns">
        <section className="authority-column" aria-labelledby="pending-pairings-title">
          <div className="authority-column-title">
            <h3 id="pending-pairings-title">Pending pairing</h3>
            <span>{overview?.pairings.filter((item) => item.status === "pending").length ?? 0}</span>
          </div>
          {(overview?.pairings ?? []).filter((item) => item.status === "pending").map((pairing) => (
            <article className="authority-card" key={pairing.pairingId}>
              <strong>{pairing.claimedClientLabel}</strong>
              <small>Self-reported client · vendor identity not verified</small>
              <dl>
                <div><dt>Project</dt><dd>{pairing.projectId}</dd></div>
                <div><dt>Code</dt><dd>{pairing.shortCode}</dd></div>
                <div><dt>Scope</dt><dd>{pairing.requestedActions.join(", ")}</dd></div>
                <div><dt>Limit</dt><dd>{pairing.requestedMaxOperations} operations</dd></div>
              </dl>
              <button
                type="button"
                className="primary-button compact-button"
                disabled={isBusy}
                onClick={() => {
                  setProjectId(pairing.projectId);
                  setProjectIdInput(pairing.projectId);
                  void finishAction(
                    () => desktop.reviewAgentPairing(pairing.pairingId, pairing.projectId),
                    pairing.projectId,
                  );
                }}
              >
                Review in native window
              </button>
            </article>
          ))}
          {(overview?.pairings ?? []).filter((item) => item.status === "pending").length === 0 ? (
            <p className="authority-empty">No Agent is waiting for access.</p>
          ) : null}
        </section>

        <section className="authority-column" aria-labelledby="active-capabilities-title">
          <div className="authority-column-title">
            <h3 id="active-capabilities-title">Active Agent access</h3>
            <span>{overview?.capabilities.filter((item) => item.status === "active").length ?? 0}</span>
          </div>
          {(overview?.capabilities ?? []).filter((item) => item.status === "active").map((capability) => (
            <article className="authority-card" key={capability.capabilityId}>
              <strong>{capability.claimedClientLabel}</strong>
              <small>Self-reported client · local capability currently active</small>
              <dl>
                <div><dt>Project</dt><dd>{capability.projectId}</dd></div>
                <div><dt>Scope</dt><dd>{capability.actions.join(", ")}</dd></div>
                <div><dt>Remaining</dt><dd>{capability.remainingOperations} / {capability.maxOperations}</dd></div>
                <div><dt>Expires</dt><dd>{formatExpiry(capability.expiresAt)}</dd></div>
              </dl>
              <button
                type="button"
                className="secondary-button compact-button danger-button"
                disabled={isBusy}
                onClick={() => {
                  setProjectId(capability.projectId);
                  setProjectIdInput(capability.projectId);
                  void finishAction(
                    () => desktop.revokeAgentCapability(
                      capability.capabilityId,
                      capability.projectId,
                    ),
                    capability.projectId,
                  );
                }}
              >
                Revoke in native window
              </button>
            </article>
          ))}
          {(overview?.capabilities ?? []).filter((item) => item.status === "active").length === 0 ? (
            <p className="authority-empty">No Agent currently has proposal-write access.</p>
          ) : null}
        </section>
      </div>

      <section className="decision-authority" aria-labelledby="decision-authority-title">
        <div className="decision-authority-head">
          <div>
            <p className="eyebrow">User authority</p>
            <h3 id="decision-authority-title">Current Blueprint decisions</h3>
          </div>
          <label className="authority-project-field">
            <span>Project ID</span>
            <input
              type="text"
              value={projectIdInput}
              list="known-authority-projects"
              placeholder="Existing Blueprint project ID"
              onChange={(event) => setProjectIdInput(event.target.value)}
            />
            <datalist id="known-authority-projects">
              {knownProjectIds.map((id) => <option value={id} key={id} />)}
            </datalist>
          </label>
          <button
            type="button"
            className="secondary-button compact-button"
            disabled={isBusy || !projectIdInput.trim()}
            onClick={() => {
              const nextProjectId = projectIdInput.trim();
              setProjectId(nextProjectId);
              void refresh(nextProjectId);
            }}
          >
            Load exact head
          </button>
        </div>

        {authority ? (
          <>
            <div className="authority-projection-summary">
              <strong>{authority.projectId} · Revision {authority.revision}</strong>
              <span>{authority.confirmedDecisionUnitCount} / {authority.decisionUnitCount} confirmed</span>
              <small>
                This is an authority projection only—not compiler, render, export, or publish approval.
              </small>
            </div>
            <div className="decision-unit-grid">
              {authority.decisionUnits.map((unit) => (
                <label className={`decision-unit-card state-${unit.state}`} key={unit.name}>
                  <input
                    type="checkbox"
                    checked={selectedUnits.has(unit.name)}
                    onChange={() => toggleUnit(unit.name)}
                  />
                  <span>
                    <strong>{unit.label}</strong>
                    <small>{unit.state === "confirmed" ? "Confirmed by native user review" : "Unverified proposal"}</small>
                  </span>
                  <em>{unit.state === "confirmed" ? "Confirmed" : "Open"}</em>
                </label>
              ))}
            </div>
            <div className="authority-actions">
              <button
                type="button"
                className="primary-button compact-button"
                disabled={isBusy}
                onClick={() => requestDecisionReview("confirm")}
              >
                Confirm selected
              </button>
              <button
                type="button"
                className="secondary-button compact-button"
                disabled={isBusy}
                onClick={() => requestDecisionReview("confirm", true)}
              >
                Review all 8 decisions
              </button>
              <button
                type="button"
                className="secondary-button compact-button danger-button"
                disabled={isBusy}
                onClick={() => requestDecisionReview("revoke")}
              >
                Revoke selected confirmations
              </button>
            </div>
          </>
        ) : (
          <p className="authority-empty">
            Choose an existing Blueprint project to inspect its eight decision units. This panel is
            intentionally not a Timeline editor.
          </p>
        )}
      </section>

      {message ? <p className="inline-note" role="status" aria-live="polite">{message}</p> : null}
      {error ? <p className="inline-error" role="alert">{error}</p> : null}
    </section>
  );
}
