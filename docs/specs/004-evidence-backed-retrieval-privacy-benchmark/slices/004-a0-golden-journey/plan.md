# Implementation Plan: ML-004-A0

## Approach

复用现有 unittest 与 Node contract tests，不新增第二套 demo runner，不把 UI 文案当验证 oracle。

## Verification Layers

1. `test_media_import_service.py`：manifest 冻结、单事务 apply、提交后 job dispatch。
2. `test_video_media.py`：identity/head、路径、权限、恢复、synthetic import-to-preview 主链。
3. `video_api.test.mjs`：renderer API contract 与错误语义。
4. `video_workflow.test.mjs`：可见工作流只声明当前真正开放的步骤。

## Evidence Rule

只有 `verify.sh` 的同一次完整退出码为 0 才可记录 A0 local pass。单个测试、已有日志或文档中的旧数字不能替代本次运行。

## Rollback

本切片只增加文档与验证入口，无 schema、媒体、runtime 或产品状态迁移。
