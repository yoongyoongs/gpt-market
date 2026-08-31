# Phase 11 策略影子与稳定化实施记录

> 状态：开发完成，待测试验收
> 分支：`codex/phase11-stabilization`

## 已实现

- Strategy Version 和 Guardrail Version 不可变版本链；
- AI/Human/System Strategy Proposal，AI Proposal 必须引用来源结果；
- Shadow/A-B Experiment 配置与追加式生命周期事件；
- 确定性 A/B Bucket，同一 Experiment/Subject 始终得到相同分组；
- Shadow Observation 保存新旧输出 Hash、完整 Payload、分歧、延迟和错误；
- Capacity Evaluation 从真实 Shadow Observation 计算 Sample、Error Rate、P95 和 Divergence，调用方不能自报这些指标；
- Guardrail 检查容量利用率、Provider Failure、样本量、错误率、延迟和分歧率；
- 发布状态使用乐观锁；发布事件追加且不可变；
- SHADOW、AB、V3 三级激活门禁；
- V3 激活要求完成 Experiment、通过 Capacity、完成 Point-in-Time Replay 且无阻塞 Regression Case；
- Activation 必须绑定被批准的 Strategy Proposal 且只允许 `HUMAN`；AI 不能启动实验、激活策略或回滚生产；
- Human/System 可快速回滚到 V2；
- 运行健康事件为 FAILED 且 Guardrail 要求时，追加系统回滚事件并将发布投影切回 V2；
- Release Dashboard 明确区分数据库期望状态与实际进程 Feature Flag。

## 数据库对象

- `strategy_versions`
- `strategy_proposals`
- `guardrail_versions`
- `strategy_experiments`
- `strategy_experiment_events`
- `shadow_observations`
- `capacity_evaluations`
- `release_states`
- `release_events`
- `operational_health_events`

除 `release_states` 当前投影外，其余 Phase 11 事实表均由数据库 Trigger 禁止 UPDATE/DELETE。

## 安全边界

- Proposal 不等于 Activation；
- Shadow 不路由用户流量；
- A/B 只按配置比例分流，不修改 V1/V2 事实或评分；
- 数据库 Release State 不直接修改进程环境变量、容器或反向代理；
- 本阶段没有自动部署服务器、没有启用生产 V3、没有接入券商；
- Named Tunnel、稳定域名和企业级鉴权仍按 Hardening 单独排期。

## 验收状态

按当前约定，本阶段未执行 Domain、Migration、PostgreSQL、Shadow、A/B、并发激活、自动回滚、容量或生产回归测试。当前只表示代码、迁移、API 和文档开发完成，不代表通过 Phase 11 Architecture Gate，也不代表 V3 已稳定上线。
