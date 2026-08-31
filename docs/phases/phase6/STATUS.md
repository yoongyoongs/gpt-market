# Phase 6 Status

> 本文件是 Phase 6 普通开发的主要状态入口，不复制完整设计。
>
> 最后更新：2026-08-31（Asia/Shanghai）

## Phase State

- **Status**：TODO
- **Stable Base**：`main@4e80755` / `v3-phase5-multi-recall-20260831`
- **Suggested Branch**：`codex/phase6-context-task`
- **Current Task**：NONE；等待用户确认后才启动 P6-01

## Tasks

| Task ID | Task Name | Status | Commit | Tests | Interface/Migration | Follow-up |
|---|---|---|---|---|---|---|
| P6-01 | 冻结 Phase 6 Domain/Repository Contract 与 0006 Migration 设计 | TODO | - | Contract/Migration | 新增 Phase 6，不改 Phase 3–5 Contract | 治理完成后首个 Task |
| P6-02 | CandidateComparisonPack Domain 与不可变持久化 | TODO | - | Domain/PostgreSQL | 0006 | 依赖 P6-01 |
| P6-03 | Comparison Builder 与 N→TopK READ | TODO | - | Application/API | 新 READ | 依赖 P6-02 |
| P6-04 | FAST/NORMAL/DEEP Context Pack 与 Evidence Selection | TODO | - | Builder/PostgreSQL | 0006 | 依赖 P6-01 |
| P6-05 | Task Profile、Expected Run 与 Task Run Registry | TODO | - | Domain/PostgreSQL | 0006 | 不实现 Import |
| P6-06 | ChatGPT READ JSON 接线与契约测试 | TODO | - | API/回归 | 新 READ | 依赖 P6-03–P6-05 |
| P6-07 | Phase 6 全市场性能验收与 Architecture Gate | TODO | - | 全量/真实库 | NONE | 最终 Gate |

## Current Task Scope

- **READ SCOPE**：治理文件、Baseline 的 Phase 6/Context/Task/READ 精确章节；
- **WRITE SCOPE**：`AGENTS.md`、治理文档、Phase Capsule、导航/状态文档和轻量治理测试；
- **Forbidden Changes**：所有业务代码、Migration、API、V1/V2、Provider、Scanner、评分和生产配置。

## Blockers / UNKNOWN

- CandidateComparison/Context/Task 的首个正式 Schema/Builder Version 尚未在 P6-01 冻结；
- 生产 V3 仍关闭，Phase 6 不以生产部署为开始条件；
- 当前服务器 SSH/公网在 Phase 5 收尾后曾再次出现 502，属于独立运维问题，不允许在治理 Task 中修改生产。

## Next Executable Action

等待用户确认。下一个独立 Task 使用 `TASK_TEMPLATE.md` 启动 **P6-01**，先冻结 Contract 与 Migration 设计，不自动进入 P6-02。
