# 最小必要上下文规范

> 适用于 Codex、Claude Code、Cursor、人工开发者及其他 Agent。
>
> 目标是减少重复读取，同时保证实现不偏离 V3 Architecture Baseline。

## 1. 默认读取顺序

普通 Task 只先读取 Level 0：

1. 根 `AGENTS.md`；
2. 当前路径向上生效的局部 `AGENTS.md`；
3. `docs/ARCHITECTURE_GUARDRAILS.md`；
4. `docs/工作状态.md` 指向的当前 Phase Capsule：`SCOPE.md`、`CONTRACTS.md`、`ACCEPTANCE.md`、`STATUS.md`；
5. 当前 Task；
6. Task 直接相关源代码和测试。

不得默认全文读取 Baseline、需求、功能架构、技术架构、数据库设计、详细设计、全部历史 STATUS 或 `docs/archive/`。

## 2. 分层扩大上下文

| Level | 允许读取 | 触发条件 |
|---|---|---|
| 0 | 导航、Guardrails、Phase Capsule、Task、直接代码/测试 | 所有普通 Task |
| 1 | 当前模块直接使用的 DTO、Domain Object、Protocol、Repository/API/Migration/配置契约 | Level 0 缺少接口事实 |
| 2 | 直接依赖的具体实现 | Contract 不能解释实际行为、测试失败或兼容问题 |
| 3 | Baseline 及大型正式设计的精确相关章节 | 架构冲突、跨 Phase 语义、Contract/Schema 变更或 Phase Gate |

每次扩大范围都应有明确问题。不得递归探索无关模块，也不得为了节省 Token 猜字段、接口、数据库结构或历史语义。

## 3. Task 启动门禁

开始前必须：

1. 检查 `git status --short --branch`、最近提交和远端；
2. 从 Phase `STATUS.md` 选择一个 TODO Task；
3. 使用 `TASK_TEMPLATE.md` 明确 Goal、Implements、READ/WRITE Scope、Dependencies、Acceptance、Tests 和 Forbidden Changes；
4. 核对 Guardrail ID、冻结 Contract 和回滚点；
5. 将 Task 标记为 `IN_PROGRESS` 后再修改代码。

模糊请求如“继续 Phase 6”必须先收敛为一个有 Task ID 的目标，不能一次包办整个 Phase。

## 4. Task 执行与停止

标准顺序：读取最小上下文 → 简短计划 → 实现当前 Task → 相关测试 → 最小回归 → 更新 Phase STATUS → 检查 Secret/无关改动 → 中文提交 → 推送 → 核对远端 → 停止。

不阻塞当前 Task 的其他问题只记为 `FOLLOW_UP`。不得顺手修改其他模块，不得自动进入下一 Task。

若 Token、时间或外部条件即将中断，停止扩大范围；在任务分支提交可恢复 WIP，并在 STATUS 写明失败、命令、关键文件和下一条动作。WIP 不得合并 `main` 或部署。

## 5. Contract 与设计冲突

Phase `CONTRACTS.md` 中的跨模块接口默认冻结。无法满足正式需求时先报告：

```text
DESIGN_CHANGE_REQUIRED
当前设计 / 问题 / 建议修改 / 影响模块与 Phase
API / Database / Migration / V1-V2 Compatibility 影响
是否需要修改 Baseline：YES | NO
```

下层文档与 Baseline 冲突时报告 `DESIGN_CONFLICT`，不得静默选择或自行重写架构。

## 6. Contract Check 与 Phase Gate

每完成 3–5 个关联 Task，执行轻量 Contract Check：接口漂移、输入输出、重复模型、Phase 边界、Guardrails 和冻结 Contract。

Phase 宣称完成前必须执行单独 Phase Architecture Conformance Review，允许读取 Level 3，并逐项核对 Baseline、Guardrails、SCOPE、CONTRACTS、ACCEPTANCE、STATUS、代码、Migration、API、测试和前后 Phase 接口。只有无未决 Blocker 且 Acceptance 全部有证据，才能标记 `DONE/ACCEPTED`。

## 7. 文档更新范围

普通 Task 主要更新代码、测试、Phase STATUS 和必要局部文档。只有真实 Requirement、Architecture、Contract、Schema 或 Phase Boundary 变化才同步大型 Source of Truth。Phase Gate 统一检查全局文档是否需要更新。

正式 Markdown 必须生成同名 HTML；Markdown 是唯一内容源。历史材料默认归档且不进入普通 Task 上下文。

## 8. 跨机器恢复标准

新电脑只需 clone 仓库，即可从根 `AGENTS.md` 找到 Guardrails、Context Policy、当前 Phase Capsule、下一 Task、冻结接口和验收方式。任何必须依赖旧聊天、某台电脑或未提交文件才能恢复的项目知识都视为治理缺陷。
