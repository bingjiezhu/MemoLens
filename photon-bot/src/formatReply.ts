import type { BotReply, RetrievalResponse } from "./types.js";

const followUpPrompt = [
  "You can refine by replying:",
  "More like this",
  "Keep only landscapes",
  "Send first two originals",
].join("\n");

export function formatReply(result: RetrievalResponse, imagePaths: string[]): BotReply {
  if (result.status !== "completed") {
    return {
      text: [
        result.message ? `This search did not complete: ${result.message}` : "This search did not complete.",
        "",
        "Try a more specific description, for example: beach sunset last summer.",
      ].join("\n"),
      imagePaths: [],
    };
  }

  if (result.data.length === 0) {
    return {
      text: [
        "No matching photos were found.",
        "",
        "Try another description, for example: beach sunset last summer or night city skyline.",
      ].join("\n"),
      imagePaths: [],
    };
  }

  if (imagePaths.length === 0) {
    return {
      text: [
        "Results were found, but the local original image paths could not be resolved.",
        "",
        "Confirm that `IMAGE_LIBRARY_DIR` points to the real local photo folder, then try again.",
      ].join("\n"),
      imagePaths: [],
    };
  }

  return {
    text: [
      `I found ${imagePaths.length} close matches.`,
      "",
      buildSummary(result),
      "",
      followUpPrompt,
    ].join("\n"),
    imagePaths,
  };
}

export function formatNextBatchReply(imagePaths: string[]): BotReply {
  if (imagePaths.length === 0) {
    return {
      text: [
        "The previous result set has already been fully sent.",
        "",
        "You can refine the search, for example: keep only landscapes or add night scenes.",
      ].join("\n"),
      imagePaths: [],
    };
  }

  return {
    text: [
      `Here are ${imagePaths.length} more photos.`,
      "",
      "You can keep refining with:",
      "More like this",
      "Keep only landscapes",
      "Send first two originals",
    ].join("\n"),
    imagePaths,
  };
}

export function formatNoSessionReply(): BotReply {
  return {
    text: [
      "There is no previous result set yet.",
      "",
      "Send a photo search description first, for example: beach sunset last summer.",
    ].join("\n"),
    imagePaths: [],
  };
}

function buildSummary(result: RetrievalResponse): string {
  const lines: string[] = [];

  if (result.title) {
    lines.push(`This set is closest to a ${result.title} direction. `);
  }

  if (result.caption) {
    lines.push(result.caption.trim());
  } else if (result.notes.length > 0) {
    lines.push(`The strongest keywords are ${result.notes.slice(0, 3).join(", ")}. `);
  }

  lines.push("Images sent.");
  return lines.join("");
}
