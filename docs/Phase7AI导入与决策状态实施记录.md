# Phase 7 AI 导入与决策状态实施记录

> 状态：开发完成，待测试验收
> 分支：`codex/phase7-10-core`

## 范围

本阶段实现 Single Envelope/Bundle 统一导入、整包预览与一次确认、逻辑 Atomic Group 独立事务、Task Run 组计数，以及追加式 MarketReview、Decision、EntryPlan、Review 和 Watchlist Proposal。

## 已实现

- `AIResultBundle`、Atomic Group、依赖关系及 Canonical Hash 校验；
- 预览持久化与 `preview_revision + bundle_hash` 确认防漂移；
- 确认幂等键、组级独立提交、失败隔离及 `PARTIAL_COMPLETED` 计数回写；
- Context Pack ID/Hash、AgentTask、ResultType 与 Evidence 时点校验；
- Decision、Review、MarketReview、EntryPlan Version 追加写入；
- AI Watchlist 结果只落 Proposal，不冒充用户状态、成交或持仓；
- 数据库不可变 Trigger 和迁移回滚路径；
- `/api/v3/ai-results/imports/preview` 与 `/confirm` 写接口。

## 不变量

- AI 输出不能声明 `actual_trade`、`trade_executed`、`holding_confirmed`；
- 一个 Atomic Group 内失败则该组整体回滚，其他合法组继续；
- EntryPlan 旧版本不覆盖；Decision/Review/MarketReview 不更新原记录；
- PositionReview 的完整持久化留在 Phase 9；Trade/Holding 只由 Phase 8 人工确认账本产生。

## 验收状态

按当前要求，本阶段只完成开发，未执行单元、集成、迁移或性能测试，也未部署生产。进入主分支前仍需执行 Phase 7 门禁，尤其是 29/30、幂等重试、迁移 Up/Down 和不可变 Trigger 验收。
