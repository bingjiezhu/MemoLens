# Implementation Plan: ML-015-B2B4A

## Status

- 当前：`IMPLEMENTED / LOCALLY VALIDATED / PARENT B2B4 PROMOTION BLOCKED`
- 以下 sequence 已在 2026-08-29 的受控本地证据链中完成；B2B4 T053/T054 的真实 Codex↔DeepSeek model/host 旅程仍属父切片阻塞项。

## Sequence

1. 冻结 OpenCut、OpenChatCut、ChatCut Agent Plugin 的 commit 与许可边界；只采用通用交互模式。
2. 保持 canonical snapshot 的 replacement projection 为唯一 candidate list；补齐 current clip 与 candidate 的 verified thumbnail adapter。
3. 增加 cookie-bound/same-origin/no-store/bounded candidate JPEG route，并在 manager 与 paired backend 做两层 eligibility 验证。
4. 以 current-vs-eligible cards 替换 ID-only dropdown；卡片按钮只 Stage 现有 `replace_clip` command。
5. 将新路由加入 production inventory、negative oracle 和双向 discovery。
6. 运行 unit、inventory、真实浏览器、完整 plugin/cache 与整仓 gate；只按实际证据更新状态。

## Stop conditions

- 若 candidate 不能从同一 Beat 的 verified proof 唯一解析，显示 unavailable，不猜素材。
- 若 browser 需要 source path/token 才能显示候选，停止该路径，不扩大浏览器 authority。
- 若操作需要 `clip_id + assignment_id` 之外的 caller facts，先改 Core contract，不让 UI 成为 truth source。
- 若只能显示身份而不能取得 verified JPEG，保留文字 fallback；不得伪造 preview。
