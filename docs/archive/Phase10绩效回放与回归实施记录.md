# Phase 10 绩效、回放与回归实施记录

> 状态：技术验收通过；生产 V3 仍关闭

## 已实现

- Selection、Initial Entry、User Execution、Add、Reduce、Final Exit、Risk Control 七类独立 Performance Attribution；
- Raw Return、Excess Return、MFE、MAE、Target/Stop 和扩展指标；
- Decision 原始 Plan、Review 后评估 Plan、真实成交绑定 Plan 三种引用分别保存；
- Trade Attribution 校验 Ledger 中冻结的 EntryPlan Binding，后续 Plan 不改写历史；
- Performance Summary 按 Ability、Market Regime、Strategy Version、时间窗口聚合；
- Point-in-Time Replay 保存明确 Revision/Evidence/Context Set；
- 缺失输入或任意 `known_at > replay_as_of` 时 Replay 状态为 `BLOCKED`，不降级为 Warning；
- Regression Case 保存输入要求、预期不变量和源 Replay；源 Replay 被阻塞时案例同步阻塞；
- 在 Phase 5 成熟 Observation/Recall Miss Evaluation 基础上增加版本化统计运行快照；
- Performance、Replay、Regression、Recall Miss API。

## 核心边界

- 七类能力不合并成统一最终分；
- 未成交机会只做 Selection 归因，用户执行必须绑定真实 Trade；
- 自主成交不能事后绑定新 AI Plan；
- Replay 只接受在回放时点已知的不可变输入集合；
- Strategy 激活、Shadow/A-B 和生产 Guardrail 属于 Phase 11，本阶段不自动启用策略。

## 验收状态

2026-09-01 完成七类能力契约、归因成熟时间、User Execution 必须绑定 Trade、Replay 缺失输入硬阻塞及 Regression Case 继承阻塞验收；Phase 5 Recall Miss 回归随全项目真实库套件通过。生产未部署。
