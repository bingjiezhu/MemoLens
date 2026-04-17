import type { BotReply, RetrievalResponse } from "./types.js";

const followUpPrompt = [
  "继续收窄可以直接回复：",
  "再来一组",
  "只保留风景",
  "发前两张原图",
].join("\n");

export function formatReply(result: RetrievalResponse, imagePaths: string[]): BotReply {
  if (result.status !== "completed") {
    return {
      text: [
        result.message ? `这次检索没有跑通：${result.message}` : "这次检索没有跑通。",
        "",
        "你可以换一句更具体的描述再试一次，例如：去年夏天海边日落。",
      ].join("\n"),
      imagePaths: [],
    };
  }

  if (result.data.length === 0) {
    return {
      text: [
        "这次没有找到合适的照片。",
        "",
        "你可以换个说法再试一次，例如：去年夏天海边日落、夜景城市天际线。",
      ].join("\n"),
      imagePaths: [],
    };
  }

  if (imagePaths.length === 0) {
    return {
      text: [
        "结果已经找到了，但本地原图没有解析成功，所以这次没法直接把图片发出来。",
        "",
        "请先确认 `IMAGE_LIBRARY_DIR` 指向本机真实图片目录，然后再试一次。",
      ].join("\n"),
      imagePaths: [],
    };
  }

  return {
    text: [
      `我先帮你筛出 ${imagePaths.length} 张最接近的照片。`,
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
        "上一轮结果已经发完了。",
        "",
        "你可以直接换个条件继续找，例如：只保留风景、要夜景。",
      ].join("\n"),
      imagePaths: [],
    };
  }

  return {
    text: [
      `我再补 ${imagePaths.length} 张给你。`,
      "",
      "如果还想继续收窄，也可以直接回复下面这些：",
      "再来一组",
      "只保留风景",
      "发前两张原图",
    ].join("\n"),
    imagePaths,
  };
}

export function formatNoSessionReply(): BotReply {
  return {
    text: [
      "我这里还没有上一轮结果。",
      "",
      "先发一句你想找的照片描述，例如：去年夏天海边日落。",
    ].join("\n"),
    imagePaths: [],
  };
}

function buildSummary(result: RetrievalResponse): string {
  const lines: string[] = [];

  if (result.title) {
    lines.push(`这组更接近“${result.title}”这类感觉。`);
  }

  if (result.caption) {
    lines.push(result.caption.trim());
  } else if (result.notes.length > 0) {
    lines.push(`关键词更偏 ${result.notes.slice(0, 3).join("、")}。`);
  }

  lines.push("我已经把图发给你。");
  return lines.join("");
}
