# V3 Baseline 文档审查意见

> 归档说明：这是形成 V3 Architecture Baseline 1.0 最后一轮一致性修补的原始审查输入，仅供追溯；当前开发入口以 `docs/README.md`、正式架构文档和工作状态文档为准。

当前这5份V3文档总体通过，不需要重新设计架构，也不要进入代码实施。

请基于现有：
- 《gpt-market V3 技术架构设计》
- 《gpt-market V3 数据库设计》
- 《gpt-market V3 系统功能架构》
- 《gpt-market V3 详细设计》
- 《gpt-market V3 需求规格说明》

做一次“文档一致性修补”，直接修改原文并保持它们继续以 V3 Architecture Baseline 1.0 为基线。

本轮只补以下5项：

1. 补清 PositionReviewResult 的正式持久化模型

当前详细设计中已经有 PositionReviewResult，且允许对真实持仓做 HOLD / REDUCE / TAKE_PROFIT / MOVE_STOP / EXIT / STOP_LOSS 等建议，但数据库设计中主要只有绑定 Decision 的 reviews。

必须支持这种情况：
用户自己先买入某股票，
系统存在真实 Portfolio，
但没有历史 AI Decision / EntryPlan，
后续 ChatGPT 仍然要能对该真实持仓进行持续 Review。

请明确设计 Position Review 的持久化方式。

优先建议：
新增独立 position_reviews 表/领域对象。

至少保存：
position_review_id
account_id
security_id
position_snapshot_id / portfolio_snapshot_id
decision_id nullable
entry_plan_id nullable
previous_position_review_id nullable
task_run_id
context_pack_id/hash
evidence_ids
agent identity
as_of
真实持仓数量/成本快照引用
thesis_status
supporting_evidence
contrary_evidence
changed_facts
new_risks
time_efficiency
recommended_action
reason
content_hash
created_at

要求：
- append-only；
- 不要求必须存在 Decision；
- 不能直接生成真实 SELL；
- 能支持 MANUAL_TRADE_WITHOUT_AI_ENTRY 的持仓后续管理；
- PositionReviewResult 的 Import 映射、数据库表、API/Context关系、测试要求全部同步更新。

2. 补 Portfolio 层的账户级 Corporate Action / Adjustment Ledger

当前市场事实层已有 corporate_actions，但 Portfolio Projection 又要求根据 Opening + Adjustment + Trade + Correction 重建。

市场层 corporate_actions 只是“公司发生了什么”，不等于“该公司行动实际怎样影响用户这个账户”。

请增加账户级不可变调整记录，例如：
portfolio_adjustments
或
corporate_action_adjustments

至少考虑：
account_id
security_id
corporate_action_id nullable
adjustment_type
effective_time
quantity_delta
cash_delta
cost_basis_delta
source
source_reference
known_at
confirmation_status
created_at

用于：
分红
送股
转增
拆并股
配股
其他会改变实际持仓数量、现金或成本基础的公司行动。

要求：
- 不覆盖 Trade Ledger；
- Adjustment 自身 append-only；
- Portfolio Projection 可以由 Opening Position + Portfolio Adjustment + Trade Ledger + Correction/Reconciliation 重建；
- 如果公司行动来源不可靠，显式 UNKNOWN / pending reconciliation；
- 更新数据库设计、详细设计、功能架构和相关需求编号/验收测试。

3. 补清 Task Run 部分成功状态

当前 AIResultBundle 支持多个 Atomic Group：
例如同一次盘后任务分析30只股票，
29组成功，1组失败。

现有 Task Run 状态只有：
EXPECTED
PENDING_IMPORT
COMPLETED
MISSED
CANCELLED

这个状态不足以精确表达部分完成。

请明确设计。

建议增加：
PARTIAL_COMPLETED

并在 task_runs 中增加或等价表达：
expected_group_count
successful_group_count
failed_group_count
pending_group_count

规则示例：
- 全部必需组成功 → COMPLETED
- 至少一组成功但仍有失败/待修正 → PARTIAL_COMPLETED
- 尚未导入任何结果 → PENDING_IMPORT
- 超过宽限期仍完全无结果 → MISSED

如果某些 Task 本身只有Single Result，应兼容原流程。

请同步更新：
数据库设计
详细设计
Task状态图
AI Result Import
验收标准
监控指标
测试用例。

4. 明确 EntryPlan 的版本关系和唯一真相

当前系统既有 Immutable Decision，又有独立 entry_plans。

后续 Review 很可能修改：
Stop
Target
Entry Window
Cancel Condition
Quantity Suggestion
Horizon

不能 UPDATE 原始 Decision，也不能覆盖原始 EntryPlan。

请明确：

原始 Decision 永久保存当时的 Entry Plan 快照/引用；
正式 EntryPlan 为独立版本化领域对象。

建议字段/关系：
entry_plan_id
decision_id
version
supersedes_entry_plan_id nullable
created_by_review_id nullable
status
effective_from
content_hash

Decision 中保存：
original_entry_plan_id
original_entry_plan_snapshot/hash

后续 Review 如果调整交易计划：
创建新的 EntryPlan Version，
旧版本永久保留，
当前有效版本通过 Projection / status / current_entry_plan_id 表达。

要求明确：
- 哪个对象是 canonical trade plan；
- Decision 只保存原始不可变快照；
- Execution Deviation 必须绑定用户成交当时对应的 EntryPlan Version；
- Performance 能分别评价原始计划与后续调整；
- 不能静默覆盖旧 Stop/Target。

同步修改数据库、详细设计、AI Import、Review、Trade、Performance相关章节。

5. 补 Requirements Traceability Matrix

《需求规格说明》已经有：
FR-MD
FR-DI
FR-EV
FR-CT
FR-AI
FR-DC
FR-TR
FR-PF
等编号。

请先检查当前仓库文档中是否已经存在正式 Requirements Traceability 文档。

如果已经存在：
请更新并保证与当前5份文档一致。

如果不存在：
新增一份：
《gpt-market V3 需求追踪矩阵》
或 requirements_traceability.md

至少包含：

Requirement ID
需求摘要
Priority
Phase
功能域/模块
主要数据库表
主要API/Job
主要测试
当前状态
Evidence/验收依据

例如：
FR-AI-006
→ Phase 7
→ AI Result Import
→ ai_result_atomic_groups
→ POST /api/v3/ai-results/import
→ atomic group partial failure test
→ TODO

要求：
- 每个P0/P1需求必须能追踪到实现Phase；
- 不能出现“需求存在但无模块/API/表/测试承接”；
- 不能把设计完成标成实现完成；
- UNKNOWN保持UNKNOWN；
- 后续每个Phase结束时可通过这张矩阵更新状态。

最后请做一次跨文档一致性检查，重点确认：
- 功能架构、数据库、详细设计、需求规格使用相同术语；
- PositionReview、PortfolioAdjustment、Task Partial、EntryPlan Version在所有相关文档中没有互相矛盾；
- V4边界不变；
- 不引入新的固定Final Score；
- 不修改“系统负责事实、AI负责判断、用户确认真实资金行为”的核心原则。

本轮输出：
1. 更新后的原文档；
2. 如需要，新增 Requirements Traceability Matrix；
3. 一份很短的“本轮修补摘要”，列出修改了哪些文件和章节。

不要修改任何业务代码。
不要创建Migration。
不要开始Phase 1。
不要部署。
完成后停止，等待Review。
