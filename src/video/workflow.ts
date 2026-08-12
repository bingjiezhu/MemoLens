export const VIDEO_WORKFLOW_IDS = [
  "idea",
  "materials",
  "brief",
  "timeline",
  "preview",
  "save",
] as const;

export type VideoWorkflowId = (typeof VIDEO_WORKFLOW_IDS)[number];

export type VideoWorkflowStatus = "complete" | "current" | "locked";

export interface VideoWorkflowCompletion {
  idea: boolean;
  materials: boolean;
  brief: boolean;
  timeline: boolean;
  preview: boolean;
  save: boolean;
}
export interface VideoWorkflowStep {
  id: VideoWorkflowId;
  number: number;
  label: string;
  status: VideoWorkflowStatus;
  canOpen: boolean;
}

const VIDEO_WORKFLOW_LABELS: Record<VideoWorkflowId, string> = {
  idea: "Idea",
  materials: "Material",
  brief: "Brief",
  timeline: "Timeline",
  preview: "Preview",
  save: "Save",
};

/**
 * Derive the presentation model from persisted product facts and two explicit
 * local confirmations. The first unfinished step is the only writable step;
 * completed steps stay reviewable and later steps stay locked.
 */
export function deriveVideoWorkflow(
  completion: VideoWorkflowCompletion,
): { currentId: VideoWorkflowId; steps: VideoWorkflowStep[] } {
  const firstIncompleteIndex = VIDEO_WORKFLOW_IDS.findIndex((id) => !completion[id]);
  const currentIndex = firstIncompleteIndex === -1
    ? VIDEO_WORKFLOW_IDS.length - 1
    : firstIncompleteIndex;

  return {
    currentId: VIDEO_WORKFLOW_IDS[currentIndex],
    steps: VIDEO_WORKFLOW_IDS.map((id, index) => {
      const status: VideoWorkflowStatus = completion[id]
        ? "complete"
        : index === currentIndex
          ? "current"
          : "locked";
      return {
        id,
        number: index + 1,
        label: VIDEO_WORKFLOW_LABELS[id],
        status,
        canOpen: status !== "locked",
      };
    }),
  };
}
