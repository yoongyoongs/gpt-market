# Phase 6 Acceptance

> Derived from Baseline 第 30 章。状态只有具备测试或真实证据后才能从 TODO 改为 PASS。

| ID | 验收项 | Verification | Status | Evidence |
|---|---|---|---|---|
| AC-P6-001 | 20–100 候选可构建紧凑 Comparison Pack，候选顺序、输入 Run、版本和 Hash 可重放 | Domain/Repository/PostgreSQL 测试 | TODO | - |
| AC-P6-002 | Comparison Pack 不加载逐股深度 Context/分钟 K，且不存在 Final Score | Contract/负向测试 | TODO | - |
| AC-P6-003 | N→TopK 后只为 TopK 构建 NORMAL/DEEP Context | Application 测试 | TODO | - |
| AC-P6-004 | FAST/NORMAL/DEEP 预算生效，必保事实/反方 Evidence/质量警告不被裁掉 | Builder 测试 | TODO | - |
| AC-P6-005 | Evidence Selection 保存选择原因、side、检索分、来源优先级和顺序 | Repository/API 测试 | TODO | - |
| AC-P6-006 | `known_at <= context_as_of` 防前视，UNKNOWN/conflict/stale/coverage 显式 | 时点与缺失测试 | TODO | - |
| AC-P6-007 | Task Profile 保存 schedule/timezone/trading_calendar/Context/Comparison/TopK/Schema | Domain/PostgreSQL 测试 | TODO | - |
| AC-P6-008 | Expected Run 不被表示成 AI 已执行，Task Run 状态与计数语义正确 | 状态机测试 | TODO | - |
| AC-P6-009 | Comparison、Context、Task READ JSON 契约稳定，V3 禁用时保持 503 | API 契约测试 | TODO | - |
| AC-P6-010 | 常用 READ P95 <200ms，Comparison/Context P95 <500ms | 真实 5,551 库服务器压测 | TODO | - |
| AC-P6-011 | Migration 可正反向执行，Pack/Task 事实 append-only 且幂等 | 隔离 PostgreSQL 17 | TODO | - |
| AC-P6-012 | V1/V2/MCP/行情/扫描/评分无行为变化，生产 V3 默认关闭 | 完整回归与配置检查 | TODO | - |

## Phase Gate

- [ ] 对照 Baseline、G-001–G-048、SCOPE/CONTRACTS；
- [ ] 所有 AC-P6 项 PASS 且有证据；
- [ ] Migration Upgrade/Downgrade、真实 PostgreSQL、性能和幂等通过；
- [ ] 无 Final Score、权限墙、前视、Prompt Injection 或 Legacy 侵入；
- [ ] 正式文档、STATUS、中文提交、远端分支和稳定标签收口。
