# Phase 9 行动、入场与持仓复盘实施记录

> 状态：开发完成，待测试验收

## 已实现

- Raw Opportunity、Action Candidate、Entry Assessment 三层独立持久化；
- Action 保存 expected horizon、time efficiency、正反事实、条件和解释；
- Entry 独立保存 Trigger/Cancel 事实与 Readiness，不把 Trigger 冒充 BUY；
- Domain 明确拒绝 `final_total_score`、`action_total_score`、`final_rank_score`、`opportunity_score`；
- `PositionReviewResult` 接入 Bundle/Atomic Group 导入；
- PositionReview 绑定 Account、Security、Context Hash、Agent Identity、Evidence 和导入时 Position Projection Hash；
- PositionReview 的 Decision/EntryPlan 可空，支持手工持仓；
- PositionReview 只追加建议，不调用 Trade Service；
- Decision Pipeline、PositionReview History 和完整 Position Context READ API。

## 核心边界

- Raw 是召回事实，Action 是 AI 行动判断，Entry 是入场状态，ActualTrade 只能来自 Phase 8 人工确认；
- `REDUCE/TAKE_PROFIT/EXIT/STOP_LOSS` 均为建议，不自动生成 SELL；
- Portfolio Context 返回真实 Projection、Trade、Decision、EntryPlan 和 Review 链，但不提供自动成交能力；
- Action/Entry 只有分项事实和解释，不建立统一 Final Score 权限墙。

## 验收状态

按当前要求未执行 Domain、数据库、Bundle Import、手工持仓 Review、API 或回归测试，未部署生产。以上状态仅表示代码开发完成。
