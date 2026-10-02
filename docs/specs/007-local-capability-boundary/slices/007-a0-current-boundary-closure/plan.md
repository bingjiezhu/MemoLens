# Implementation Plan: ML-007-A0

## Strategy

以现有真实 call-site 测试冻结防线，而不是先建设通用 policy engine。

```text
Electron trust/runtime tests
        +
Flask route/root/render tests
        +
Agent authority + plugin read-only tests
        ↓
current-boundary local gate
```

## Covered Oracles

- `test_backend_regressions.py`：runtime swap、origin/token、copy route 不发送原图。
- `test_video_hardening.py`、`test_strict_http_json.py`：media/root/render/JSON 边界。
- `test_b1_backend_protocol.py`：desktop/main/Agent authority 不可替代。
- Electron Node tests：health proof、managed process、native authority、artifact integrity。
- plugin Media Wiki tests：safe-default 无网络、无原媒体读取和只读 SQLite。

## Deferred

统一 capability manifest、provider grant ledger、全 surface census 和 ML-008 最终 identity contract 另行实施；A0 不虚构这些能力。
