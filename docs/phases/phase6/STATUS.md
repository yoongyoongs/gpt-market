# Phase 6 Status

> 本文件是 Phase 6 普通开发的主要状态入口，不复制完整设计。
>
> 最后更新：2026-08-31（Asia/Shanghai）

## Phase State

- **Status**：IN_PROGRESS
- **Stable Base**：`main@9506983` / `v3-phase5-multi-recall-20260831`
- **Working Branch**：`codex/phase6-context-task`
- **Current Task**：NONE；P6-01 已完成，等待用户确认后才启动 P6-02

## Tasks

| Task ID | Task Name | Status | Commit | Tests | Interface/Migration | Follow-up |
|---|---|---|---|---|---|---|
| P6-01 | 冻结 Phase 6 Domain/Repository Contract 与 0006 Migration 设计 | DONE | `044da58` | 20 个聚焦测试；全量 197 passed / 23 skipped | 新增 Phase 6 Domain/Repository Contract；冻结 0006 设计，未创建 Migration | 无 |
| P6-02 | CandidateComparisonPack Domain 与不可变持久化 | TODO | - | Domain/PostgreSQL | 0006 | 依赖 P6-01 |
| P6-03 | Comparison Builder 与 N→TopK READ | TODO | - | Application/API | 新 READ | 依赖 P6-02 |
| P6-04 | FAST/NORMAL/DEEP Context Pack 与 Evidence Selection | TODO | - | Builder/PostgreSQL | 0006 | 依赖 P6-01 |
| P6-05 | Task Profile、Expected Run 与 Task Run Registry | TODO | - | Domain/PostgreSQL | 0006 | 不实现 Import |
| P6-06 | ChatGPT READ JSON 接线与契约测试 | TODO | - | API/回归 | 新 READ | 依赖 P6-03–P6-05 |
| P6-07 | Phase 6 全市场性能验收与 Architecture Gate | TODO | - | 全量/真实库 | NONE | 最终 Gate |

## P6-01 Completion Record

- **Goal**：冻结 CandidateComparisonPack、ContextPack、Task Profile/Expected Run/Task Run 的首版 Domain 与 Repository Protocol，并形成可执行的 0006 Migration 设计；
- **Implements**：Baseline §10–11、§24.1–24.4、Phase 6 AC-P6-001/002/005/006/007/008/011 的契约前置，G-006–G-014、G-025–G-030、G-042–G-045；
- **READ SCOPE**：治理文件、Phase 6 Capsule、Baseline 精确章节、现有 Phase 1/3/4/5 Domain、Repository Protocol、Model 和 Migration；
- **WRITE SCOPE**：Phase 6 新 Domain/Repository Contract、0006 Migration 设计、契约/设计静态测试、Phase 6 Capsule/状态；
- **Forbidden Changes**：Migration 实施、Repository/Builder/API 实现、Phase 3–5 冻结 Contract、V1/V2、Provider、Scanner、评分和生产配置；
- **Rollback Point**：`main@9506983`。
- **Commit**：`044da58 Phase6：冻结Context与Task领域契约`；
- **Result**：冻结 `candidate-comparison.v1`、`context-pack.v1`、Task Profile/Expected Run/Task Run Domain 与三个 Repository Protocol；明确 404/409/422/503 语义和 `0006_context_task_foundation` 表、ALTER、约束、索引、不可变 Trigger、Upgrade/Downgrade 顺序；
- **Tests**：`tests/v3/test_phase6_contracts.py` 等聚焦 20 passed；完整本地回归 197 passed、23 skipped、2 个既有 Warning；`compileall` 与 48 组 Markdown/HTML 同步检查通过；
- **Tooling Note**：当前虚拟环境未安装 Ruff，未宣称 Ruff 通过；
- **Interface Change**：仅新增 Phase 6 Contract，Phase 3–5 冻结接口未修改；
- **Migration Change**：仅设计，未创建/执行 0006 Migration；
- **Production/Server**：不需要服务器，未部署，V3 生产开关不变。

## Blockers / UNKNOWN

- Pack Schema Version 已冻结；实际 Builder Version 将在各 Builder 实现 Task 发布，不能冒充 Schema Version；
- 生产 V3 仍关闭，Phase 6 不以生产部署为开始条件；
- 当前服务器 SSH/公网在 Phase 5 收尾后曾再次出现 502，属于独立运维问题，不允许在治理 Task 中修改生产。

## Next Executable Action

等待用户确认。下一个独立 Task 是 **P6-02 CandidateComparisonPack Domain 与不可变持久化**；一次实现已冻结的完整 0006 DDL（后续 Task 不回改 Migration），但 Repository 行为只实现 Comparison Pack/Member，并在提交推送后停止，不自动进入 P6-03。
