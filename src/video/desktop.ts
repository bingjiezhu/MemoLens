import type {
  DesktopArtifactSaveRequest,
  DesktopArtifactSaveResult,
} from "./types";

export function canSaveVideoArtifactOnDesktop(): boolean {
  return typeof window.memolensDesktop?.saveVideoArtifact === "function";
}

export async function saveVideoArtifactOnDesktop(
  request: DesktopArtifactSaveRequest,
): Promise<DesktopArtifactSaveResult | null> {
  const api = window.memolensDesktop;
  if (!api?.saveVideoArtifact) {
    return null;
  }
  return api.saveVideoArtifact(request);
}
